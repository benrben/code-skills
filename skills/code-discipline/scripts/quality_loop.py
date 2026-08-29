#!/usr/bin/env python3
"""Run one machine-readable repository quality-gate round.

Agents invoke this command repeatedly. Exit 0 means every required gate passed,
exit 1 means actionable failures remain, and exit 2 means setup or configuration
prevented a complete measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from types import ModuleType
from typing import Any, Sequence


VERSION = "2.2.0"
DEFAULT_GATE_SCRIPT = Path(__file__).resolve().parents[3] / "repo_quality_gate.py"


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


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def build_rerun_command(
    root: Path,
    config_path: Path,
    explicit_config: bool,
    artifact_dir: Path,
    explicit_artifacts: bool,
    html_path: Path,
    explicit_html: bool,
    gate_script: Path,
    explicit_gate_script: bool,
    no_install: bool,
    fast: bool,
    mutation_workers: str | None,
) -> str:
    command = [sys.executable, str(Path(__file__).resolve()), "--root", "."]
    if explicit_config:
        command.extend(["--config", display_path(config_path, root)])
    if explicit_html:
        command.extend(["--html", display_path(html_path, root)])
    elif explicit_artifacts:
        command.extend(["--artifact-dir", display_path(artifact_dir, root)])
    if explicit_gate_script:
        command.extend(["--gate-script", str(gate_script)])
    if no_install:
        command.append("--no-install")
    if mutation_workers:
        command.extend(["--mutation-workers", mutation_workers])
    if fast:
        command.append("--fast")
    return shlex.join(command)


def function_failure(function: Any) -> dict[str, Any]:
    return {
        "path": function.path,
        "name": function.name,
        "line": function.start_line,
        "coverage_percent": round(function.coverage_percent, 2),
        "complexity": function.complexity,
        "craap_score": round(function.craap_score, 2),
    }


def mutation_failure(mutation: Any) -> dict[str, Any]:
    return {
        "id": mutation.mutant_id,
        "path": mutation.path,
        "line": mutation.line,
        "column": mutation.column,
        "change": f"{mutation.original} -> {mutation.replacement}",
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


def analysis_state(
    gate: ModuleType,
    analysis: Any,
    html_path: Path,
    state_path: Path,
    exit_code: int,
    error: str | None = None,
) -> dict[str, Any]:
    failing_functions = sorted(
        (item for item in analysis.functions if not item.passed),
        key=lambda item: (-item.craap_score, item.coverage_percent, item.path),
    )
    survivors = [item for item in analysis.mutations if item.survived]
    violations = analysis.dependency_violations
    failed_setup = [item for item in analysis.tool_setup if item.returncode != 0]
    status = (
        "error"
        if error
        else (
            "pass"
            if analysis.passed
            else ("ready_for_full" if analysis.ready_for_full else "fail")
        )
    )
    return {
        "schema_version": 1,
        "status": status,
        "mode": analysis.mode,
        "certified": analysis.passed,
        "ready_for_full": analysis.ready_for_full,
        "exit_code": exit_code,
        "repository": analysis.root,
        "generated_at": analysis.generated_at,
        "artifacts": {"html": str(html_path), "state": str(state_path)},
        "rerun_command": analysis.rerun_command,
        "full_rerun_command": gate.without_fast_flag(analysis.rerun_command or ""),
        "gates": [
            {
                "key": result.key,
                "status": (
                    "deferred"
                    if result.deferred
                    else (
                        "not_applicable"
                        if not result.applicable
                        else ("pass" if result.passed else "fail")
                    )
                ),
                "summary": result.summary,
                "details": result.details[:100],
                "commands": [
                    {
                        "command": item.command,
                        "returncode": item.returncode,
                        "timed_out": item.timed_out,
                        "duration_seconds": round(item.duration_seconds, 3),
                    }
                    for item in result.command_results
                ],
            }
            for result in analysis.gates
        ],
        "counts": {
            "checks_total": len(analysis.gates),
            "checks_executed": sum(not item.deferred for item in analysis.gates),
            "checks_deferred": sum(item.deferred for item in analysis.gates),
            "checks_applicable": sum(
                item.applicable and not item.deferred for item in analysis.gates
            ),
            "checks_passing": sum(
                item.applicable and not item.deferred and item.passed
                for item in analysis.gates
            ),
            "functions_total": len(analysis.functions),
            "functions_failing": len(failing_functions),
            "mutants_total": len(analysis.mutations),
            "mutants_surviving": len(survivors),
            "dependency_violations": len(violations),
        },
        "failures": {
            "checks": [
                {
                    "key": item.key,
                    "title": item.title,
                    "summary": item.summary,
                    "details": item.details[:100],
                }
                for item in analysis.gates
                if item.applicable and not item.deferred and not item.passed
            ],
            "functions": [function_failure(item) for item in failing_functions[:200]],
            "surviving_mutants": [mutation_failure(item) for item in survivors[:200]],
            "dependencies": [dependency_failure(item) for item in violations[:200]],
            "tool_setup": [
                {
                    "command": item.command,
                    "returncode": item.returncode,
                    "output": item.stdout[-4000:],
                }
                for item in failed_setup
            ],
        },
        "fix_prompt": None if analysis.passed else gate.master_fix_prompt(analysis),
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
    gate: ModuleType, root: Path, message: str, command: str, fast: bool
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
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--config", help="quality-gate JSON configuration")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--artifact-dir",
        help="report directory (default: a repository-specific user cache)",
    )
    output.add_argument(
        "--html",
        help="explicit HTML report path; JSON state is written beside it",
    )
    parser.add_argument(
        "--gate-script",
        help="alternate repo_quality_gate.py engine (for development)",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        help="diagnostic cap; capped runs can never pass",
    )
    parser.add_argument(
        "--mutation-workers",
        metavar="N|auto",
        help="run mutants in N isolated repository snapshots; auto uses up to 4 workers",
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: repository root does not exist: {root}", file=sys.stderr)
        return 2

    explicit_config = args.config is not None
    config_path = resolve_from_root(args.config, root, root / ".quality-gate.json")
    explicit_artifacts = args.artifact_dir is not None
    explicit_html = args.html is not None
    if explicit_html:
        html_path = resolve_from_root(
            args.html, root, root / "quality-gate-report.html"
        )
        artifact_dir = html_path.parent
    else:
        artifact_dir = resolve_from_root(
            args.artifact_dir, root, default_artifact_dir(root)
        )
        html_path = artifact_dir / "quality-gate-report.html"
    explicit_gate_script = args.gate_script is not None
    gate_script = resolve_from_root(args.gate_script, root, DEFAULT_GATE_SCRIPT)
    state_path = artifact_dir / "quality-gate-state.json"

    try:
        gate = load_gate(gate_script)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    command = build_rerun_command(
        root,
        config_path,
        explicit_config,
        artifact_dir,
        explicit_artifacts,
        html_path,
        explicit_html,
        gate_script,
        explicit_gate_script,
        args.no_install,
        args.fast,
        args.mutation_workers,
    )
    run_error: str | None = None
    try:
        config, notes = gate.load_config(config_path if config_path.exists() else None)
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
        )
        analysis.rerun_command = command
        exit_code = 0 if analysis.passed else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        run_error = str(error)
        analysis = error_report(gate, root, run_error, command, args.fast)
        exit_code = 2

    artifact_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(gate.html_report(analysis), encoding="utf-8")
    state = analysis_state(gate, analysis, html_path, state_path, exit_code, run_error)
    write_json_atomic(state_path, state)

    for result in analysis.gates:
        print(f"[{gate.gate_outcome(result)}] {result.title}: {result.summary}")
    print(f"QUALITY_LOOP={state['status'].upper()}")
    print(f"STATE={state_path}")
    print(f"HTML={html_path}")
    if args.print_prompt and state["fix_prompt"]:
        print("\n" + state["fix_prompt"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
