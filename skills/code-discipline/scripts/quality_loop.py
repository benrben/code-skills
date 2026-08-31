#!/usr/bin/env python3
"""Run one machine-readable repository quality-gate round.

Agents invoke this command repeatedly. Exit 0 means every required gate passed,
exit 1 means actionable failures remain, and exit 2 means setup or configuration
prevented a complete measurement.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
import shlex
import signal
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Sequence, TextIO

VERSION = "3.1.0"
DEFAULT_GATE_SCRIPT = Path(__file__).resolve().with_name("repo_quality_gate.py")


class ConcurrentRunError(RuntimeError):
    """Raised when another quality loop already owns a repository."""


def raise_keyboard_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def install_interrupt_handlers() -> None:
    signal.signal(signal.SIGINT, raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, raise_keyboard_interrupt)


def load_gate(path: Path) -> ModuleType:
    """Load the bundled gate without requiring package installation."""
    spec = importlib.util.spec_from_file_location("repo_quality_gate_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load quality-gate engine: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve_from_root(value: str | None, root: Path, default: Path) -> Path:
    if value is None:
        return default
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def default_artifact_dir(root: Path) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return cache / "repo-quality-loop" / f"{root.name}-{digest}"


def repository_lock_path(root: Path) -> Path:
    """Return one stable lock path regardless of report destination."""
    return default_artifact_dir(root.resolve()) / ".run.lock"


def try_lock(handle: TextIO) -> bool:
    if os.name == "nt":
        import msvcrt

        windows_msvcrt: Any = msvcrt
        locking = windows_msvcrt.locking
        nonblocking_lock = windows_msvcrt.LK_NBLCK

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        try:
            locking(handle.fileno(), nonblocking_lock, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        windows_msvcrt: Any = msvcrt
        handle.seek(0)
        windows_msvcrt.locking(handle.fileno(), windows_msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_owner(handle: TextIO) -> str:
    handle.seek(0)
    raw = handle.read().strip("\0\n ")
    if not raw:
        return "owner details unavailable"
    try:
        owner = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    pid = owner.get("pid", "unknown")
    started_at = owner.get("started_at", "unknown time")
    return f"PID {pid}, started {started_at}"


@contextmanager
def repository_run_lock(root: Path) -> Iterator[None]:
    """Allow only one quality loop to use a repository's shared tools at a time."""
    path = repository_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if not try_lock(handle):
            raise ConcurrentRunError(
                f"quality loop already running for {root} ({lock_owner(handle)})"
            )
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                },
                handle,
            )
            handle.flush()
            yield
        finally:
            unlock(handle)


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def append_rerun_configuration(
    command: list[str],
    root: Path,
    config_path: Path,
    explicit_config: bool,
    thresholds_path: Path,
    explicit_thresholds: bool,
    artifact_dir: Path,
    explicit_artifacts: bool,
    html_path: Path,
    explicit_html: bool,
    gate_script: Path,
    explicit_gate_script: bool,
) -> None:
    if explicit_config:
        command.extend(["--config", display_path(config_path, root)])
    if explicit_thresholds:
        command.extend(["--thresholds", display_path(thresholds_path, root)])
    if explicit_html:
        command.extend(["--html", display_path(html_path, root)])
    elif explicit_artifacts:
        command.extend(["--artifact-dir", display_path(artifact_dir, root)])
    if explicit_gate_script:
        command.extend(["--gate-script", str(gate_script)])


def append_rerun_execution(
    command: list[str],
    scope_arguments: Sequence[str],
    no_install: bool,
    fast: bool,
    mutation_workers: str | None,
) -> None:
    command.extend(scope_arguments)
    if no_install:
        command.append("--no-install")
    if mutation_workers:
        command.extend(["--mutation-workers", mutation_workers])
    if fast:
        command.append("--fast")


def build_rerun_command(
    root: Path,
    config_path: Path,
    explicit_config: bool,
    thresholds_path: Path,
    explicit_thresholds: bool,
    artifact_dir: Path,
    explicit_artifacts: bool,
    html_path: Path,
    explicit_html: bool,
    gate_script: Path,
    explicit_gate_script: bool,
    scope_arguments: Sequence[str],
    no_install: bool,
    fast: bool,
    mutation_workers: str | None,
) -> str:
    command = [sys.executable, str(Path(__file__).resolve()), "--root", "."]
    append_rerun_configuration(
        command,
        root,
        config_path,
        explicit_config,
        thresholds_path,
        explicit_thresholds,
        artifact_dir,
        explicit_artifacts,
        html_path,
        explicit_html,
        gate_script,
        explicit_gate_script,
    )
    append_rerun_execution(command, scope_arguments, no_install, fast, mutation_workers)
    return shlex.join(command)


def function_measurement(function: Any) -> dict[str, Any]:
    return {
        "path": function.path,
        "name": function.name,
        "line": function.start_line,
        "covered_lines": function.covered_lines,
        "total_lines": function.total_lines,
        "coverage_percent": round(function.coverage_percent, 2),
        "complexity": function.complexity,
        "craap_score": round(function.craap_score, 2),
        "passed": function.passed,
    }


def file_measurement(file: Any) -> dict[str, Any]:
    return {
        "path": file.path,
        "lines": file.lines,
        "limit": file.limit,
        "passed": file.passed,
    }


def mutation_failure(mutation: Any) -> dict[str, Any]:
    return {
        "id": mutation.mutant_id,
        "path": mutation.path,
        "line": mutation.line,
        "column": mutation.column,
        "change": f"{mutation.original} -> {mutation.replacement}",
        "status": mutation.status or ("Survived" if mutation.survived else "Killed"),
        "static": bool(getattr(mutation, "static", False)),
    }


def dependency_failure(violation: Any) -> dict[str, Any]:
    return {
        "source": violation.source,
        "line": violation.line,
        "source_module": violation.source_module,
        "target": violation.target,
        "target_module": violation.target_module,
        "rule": violation.rule,
    }


def state_status(analysis: Any, error: str | None) -> str:
    if error:
        return "error"
    if analysis.passed:
        return "pass"
    return "ready_for_full" if analysis.ready_for_full else "fail"


def command_state(command: Any) -> dict[str, Any]:
    return {
        "command": command.command,
        "returncode": command.returncode,
        "timed_out": command.timed_out,
        "duration_seconds": round(command.duration_seconds, 3),
    }


def gate_status(result: Any) -> str:
    if result.deferred:
        return "deferred"
    if not result.applicable:
        return "not_applicable"
    return "pass" if result.passed else "fail"


def gate_state(result: Any) -> dict[str, Any]:
    return {
        "key": result.key,
        "status": gate_status(result),
        "summary": result.summary,
        "details": result.details[:100],
        "commands": [command_state(item) for item in result.command_results],
    }


def quality_gate_for(analysis: Any) -> Any:
    return next((result for result in analysis.gates if result.key == "quality"), None)


def metrics_state(analysis: Any, quality_gate: Any) -> dict[str, Any]:
    return {
        "certified": bool(analysis.functions and quality_gate and quality_gate.passed),
        "functions": [function_measurement(item) for item in analysis.functions],
        "files": [file_measurement(item) for item in analysis.files],
    }


def count_state(
    analysis: Any,
    failing_functions: Sequence[Any],
    survivors: Sequence[Any],
    violations: Sequence[Any],
) -> dict[str, int]:
    outcomes = [gate_status(item) for item in analysis.gates]
    return {
        "checks_total": len(outcomes),
        "checks_executed": len(outcomes) - outcomes.count("deferred"),
        "checks_deferred": outcomes.count("deferred"),
        "checks_applicable": outcomes.count("pass") + outcomes.count("fail"),
        "checks_passing": outcomes.count("pass"),
        "functions_total": len(analysis.functions),
        "functions_failing": len(failing_functions),
        "files_total": len(analysis.files),
        "files_failing_loc": sum(not item.passed for item in analysis.files),
        "mutants_total": len(analysis.mutations),
        "mutants_surviving": len(survivors),
        "mutants_static": sum(
            bool(getattr(item, "static", False)) for item in analysis.mutations
        ),
        "dependency_violations": len(violations),
    }


def failed_check_state(gates: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "title": item.title,
            "summary": item.summary,
            "details": item.details[:100],
        }
        for item in gates
        if item.applicable and not item.deferred and not item.passed
    ]


def failed_file_state(files: Sequence[Any]) -> list[dict[str, Any]]:
    return [file_measurement(item) for item in files if not item.passed][:200]


def failed_tool_state(failed_setup: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "command": item.command,
            "returncode": item.returncode,
            "output": item.stdout[-4000:],
        }
        for item in failed_setup
    ]


def failure_state(
    analysis: Any,
    failing_functions: Sequence[Any],
    survivors: Sequence[Any],
    violations: Sequence[Any],
    failed_setup: Sequence[Any],
) -> dict[str, Any]:
    return {
        "checks": failed_check_state(analysis.gates),
        "functions": [function_measurement(item) for item in failing_functions[:200]],
        "files": failed_file_state(analysis.files),
        "surviving_mutants": [mutation_failure(item) for item in survivors[:200]],
        "dependencies": [dependency_failure(item) for item in violations[:200]],
        "tool_setup": failed_tool_state(failed_setup),
    }


def failing_function_items(analysis: Any) -> list[Any]:
    return sorted(
        (item for item in analysis.functions if not item.passed),
        key=lambda item: (-item.craap_score, item.coverage_percent, item.path),
    )


def surviving_mutation_items(analysis: Any) -> list[Any]:
    return [item for item in analysis.mutations if item.survived]


def failed_setup_items(analysis: Any) -> list[Any]:
    return [item for item in analysis.tool_setup if item.returncode != 0]


def repository_certified(analysis: Any) -> bool:
    return bool(analysis.passed and not analysis.scope.incremental)


def state_fix_prompt(gate: ModuleType, analysis: Any) -> str | None:
    return None if analysis.passed else gate.master_fix_prompt(analysis)


def analysis_state(
    gate: ModuleType,
    analysis: Any,
    html_path: Path,
    state_path: Path,
    exit_code: int,
    error: str | None = None,
) -> dict[str, Any]:
    failing_functions = failing_function_items(analysis)
    survivors = surviving_mutation_items(analysis)
    violations = analysis.dependency_violations
    failed_setup = failed_setup_items(analysis)
    quality_gate = quality_gate_for(analysis)
    return {
        "schema_version": 1,
        "status": state_status(analysis, error),
        "mode": analysis.mode,
        "certified": repository_certified(analysis),
        "scope_certified": analysis.passed,
        "ready_for_full": analysis.ready_for_full,
        "exit_code": exit_code,
        "repository": analysis.root,
        "generated_at": analysis.generated_at,
        "artifacts": {"html": str(html_path), "state": str(state_path)},
        "rerun_command": analysis.rerun_command,
        "full_rerun_command": gate.without_fast_flag(analysis.rerun_command or ""),
        "scope": {
            "kind": analysis.scope.kind,
            "reference": analysis.scope.reference,
            "changed_files": list(analysis.scope.paths),
        },
        "metrics": metrics_state(analysis, quality_gate),
        "thresholds": analysis.thresholds,
        "gates": [gate_state(result) for result in analysis.gates],
        "counts": count_state(analysis, failing_functions, survivors, violations),
        "failures": failure_state(
            analysis, failing_functions, survivors, violations, failed_setup
        ),
        "fix_prompt": state_fix_prompt(gate, analysis),
        "error": error,
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def error_report(
    gate: ModuleType,
    root: Path,
    message: str,
    command: str,
    fast: bool,
    scope: Any,
) -> Any:
    return gate.AnalysisReport(
        root=str(root),
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        languages=gate.detect_languages(root),
        gates=[
            gate.GateResult(
                "runner",
                "Gate runner",
                False,
                f"The quality-gate runner stopped: {message}",
                prompts=[
                    (
                        "Repair gate configuration",
                        "Repair this configuration or adapter error without disabling "
                        f"a required gate:\n\n{message}",
                    )
                ],
            )
        ],
        functions=[],
        mutations=[],
        dependency_violations=[],
        tool_setup=[],
        notes=["The run stopped before all gates could be evaluated."],
        rerun_command=command,
        mode="fast" if fast else "full",
        scope=scope,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--config", help="quality-gate JSON configuration")
    parser.add_argument("--thresholds", help="quality-threshold JSON configuration")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--artifact-dir",
        help="report directory (default: repository root)",
    )
    output.add_argument(
        "--html",
        help="override the automatic HTML report path; JSON state is written beside it",
    )
    parser.add_argument(
        "--gate-script",
        help="alternate repo_quality_gate.py engine (for development)",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--commit",
        nargs="?",
        const="HEAD",
        metavar="REF",
        help="gate production files changed by one commit (default REF: HEAD)",
    )
    scope.add_argument(
        "--local-changes",
        action="store_true",
        help="gate production files in staged, unstaged, and untracked changes",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        help="diagnostic cap; capped runs can never pass",
    )
    parser.add_argument(
        "--mutation-workers",
        metavar="N|auto",
        help="run native mutation workers inside one isolated snapshot; native auto follows Stryker's CPU default, while the portable fallback uses up to 4 isolated workers",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run one diagnostic pass and defer flaky-test repetitions and mutation testing; never certifies",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="forbid automatic installation of missing analysis tools",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="print the complete repair prompt after a failing run",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def resolve_artifacts(args: argparse.Namespace, root: Path) -> tuple[Path, Path, Path]:
    if args.html is not None:
        html_path = resolve_from_root(
            args.html, root, root / "quality-gate-report.html"
        )
        artifact_dir = html_path.parent
    else:
        artifact_dir = resolve_from_root(args.artifact_dir, root, root)
        html_path = artifact_dir / "quality-gate-report.html"
    return artifact_dir, html_path, artifact_dir / "quality-gate-state.json"


def scope_cli_arguments(args: argparse.Namespace) -> list[str]:
    if args.commit:
        return ["--commit", args.commit]
    if args.local_changes:
        return ["--local-changes"]
    return []


def load_gate_safely(gate_script: Path) -> ModuleType | None:
    try:
        return load_gate(gate_script)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return None


def requested_scope(args: argparse.Namespace, gate: ModuleType) -> Any:
    if args.commit:
        return gate.GateScope("commit", reference=args.commit)
    if args.local_changes:
        return gate.GateScope("local_changes")
    return gate.repository_scope()


def selected_scope(args: argparse.Namespace, gate: ModuleType, root: Path) -> Any:
    if args.commit:
        return gate.commit_scope(root, args.commit)
    if args.local_changes:
        return gate.local_changes_scope(root)
    return gate.repository_scope()


def execute_analysis(
    args: argparse.Namespace,
    gate: ModuleType,
    root: Path,
    config_path: Path,
    thresholds_path: Path,
    html_path: Path,
    command: str,
) -> tuple[Any, int, str | None]:
    scope = requested_scope(args, gate)
    try:
        scope = selected_scope(args, gate, root)
        thresholds, threshold_notes = gate.load_thresholds(thresholds_path)
        config, notes = gate.load_config(
            config_path if config_path.exists() else None, thresholds
        )
        notes = [*threshold_notes, *notes]
        if args.no_install:
            config["tools"]["auto_install"] = False
        analysis = gate.run(
            root,
            config,
            html_path,
            args.max_mutants,
            notes,
            fast=args.fast,
            cli_mutation_workers=args.mutation_workers,
            scope=scope,
            thresholds=thresholds,
        )
        analysis.rerun_command = command
        return analysis, 0 if analysis.passed else 1, None
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = str(error)
        analysis = error_report(gate, root, message, command, args.fast, scope)
        return analysis, 2, message


def print_run_summary(
    gate: ModuleType,
    analysis: Any,
    state: dict[str, Any],
    state_path: Path,
    html_path: Path,
    print_prompt: bool,
) -> None:
    for result in analysis.gates:
        print(f"[{gate.gate_outcome(result)}] {result.title}: {result.summary}")
    print(f"QUALITY_LOOP={state['status'].upper()}")
    print(f"STATE={state_path}")
    print(f"HTML={html_path}")
    if print_prompt and state["fix_prompt"]:
        print("\n" + state["fix_prompt"])


def run_locked(args: argparse.Namespace, root: Path) -> int:
    explicit_config = args.config is not None
    config_path = resolve_from_root(args.config, root, root / ".quality-gate.json")
    explicit_thresholds = args.thresholds is not None
    thresholds_path = resolve_from_root(
        args.thresholds, root, root / ".quality-thresholds.json"
    )
    explicit_artifacts = args.artifact_dir is not None
    explicit_html = args.html is not None
    artifact_dir, html_path, state_path = resolve_artifacts(args, root)
    explicit_gate_script = args.gate_script is not None
    gate_script = resolve_from_root(args.gate_script, root, DEFAULT_GATE_SCRIPT)
    scope_arguments = scope_cli_arguments(args)
    gate = load_gate_safely(gate_script)
    if gate is None:
        return 2

    if not explicit_thresholds and not thresholds_path.exists():
        thresholds_path = gate.bundled_thresholds_path()

    command = build_rerun_command(
        root,
        config_path,
        explicit_config,
        thresholds_path,
        explicit_thresholds,
        artifact_dir,
        explicit_artifacts,
        html_path,
        explicit_html,
        gate_script,
        explicit_gate_script,
        scope_arguments,
        args.no_install,
        args.fast,
        args.mutation_workers,
    )
    analysis, exit_code, run_error = execute_analysis(
        args,
        gate,
        root,
        config_path,
        thresholds_path,
        html_path,
        command,
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(gate.html_report(analysis), encoding="utf-8")
    state = analysis_state(gate, analysis, html_path, state_path, exit_code, run_error)
    write_json_atomic(state_path, state)
    print_run_summary(
        gate,
        analysis,
        state,
        state_path,
        html_path,
        args.print_prompt,
    )
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: repository root does not exist: {root}", file=sys.stderr)
        return 2
    install_interrupt_handlers()
    try:
        with repository_run_lock(root):
            return run_locked(args, root)
    except ConcurrentRunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted: quality loop stopped cleanly", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
