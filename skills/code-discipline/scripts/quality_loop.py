#!/usr/bin/env python3
"""Run one round of the repository quality loop: ten generic gates, one report."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

import diff_scope
import project_setup
import quality_report
import quality_run
from diff_scope import ChangeSet
from gate_types import BLOCKED
from quality_commands import runner_for
from quality_config import (
    CONFIG_NAME,
    QUALITY_DIRECTORY,
    REPORT_NAME,
    STATE_NAME,
    THRESHOLDS_NAME,
    VERSION,
    load_config,
    load_thresholds,
)
from quality_model import (
    FAST,
    FULL,
    PARTIAL,
    Analysis,
    GateRun,
    analysis_state,
    read_state,
    write_json_atomic,
)
from quality_tools import bootstrap_tools

CHECK_FLAGS = quality_run.CHECK_FLAGS
RunAnalysis = Callable[..., Analysis]


class ConcurrentRunError(RuntimeError):
    """Raised when another quality loop already owns a repository."""


@dataclasses.dataclass(frozen=True)
class RunFiles:
    config: Path
    thresholds: Path | None
    artifact_dir: Path
    html: Path
    state: Path


# --- Command line -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--config", help=f"configuration (default: ROOT/{CONFIG_NAME})")
    parser.add_argument(
        "--thresholds",
        help=f"thresholds (default: ROOT/{THRESHOLDS_NAME}, then the bundled ones)",
    )
    project_setup.add_setup_arguments(parser)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--artifact-dir",
        help=f"report directory (default: the repository's {QUALITY_DIRECTORY}/)",
    )
    output.add_argument(
        "--html", help="HTML report path; the JSON state is written beside it"
    )
    parser.add_argument(
        "--gate-script", help="alternate engine module with run_analysis (development)"
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--commit",
        nargs="?",
        const="HEAD",
        metavar="REF",
        help="judge the lines one commit changed (default REF: HEAD)",
    )
    scope.add_argument(
        "--local-changes",
        action="store_true",
        help="judge the lines in staged, unstaged, and untracked changes",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="defer the vulnerability lookup; never certifies",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="install nothing; analysis tools come from the current interpreter",
    )
    parser.add_argument(
        "--allow-policy-change",
        action="store_true",
        help="the user approved this change to .quality policy files",
    )
    parser.add_argument(
        "--allow-test-removal",
        action="store_true",
        help="the user approved running fewer tests than the last full run",
    )
    checks = parser.add_argument_group(
        "select checks", "combinable; omit all of them for the full report"
    )
    for flag, _, description in CHECK_FLAGS:
        checks.add_argument(f"--{flag}", action="store_true", help=description)
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def selection_names(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(
        flag
        for flag, _, _ in CHECK_FLAGS
        if getattr(args, flag.replace("-", "_"), False)
    )


def resolve_from_root(value: str, root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def resolve_run_files(args: argparse.Namespace, root: Path) -> RunFiles:
    config = resolve_from_root(args.config or CONFIG_NAME, root)
    thresholds = resolve_from_root(args.thresholds or THRESHOLDS_NAME, root)
    if args.html is not None:
        html = resolve_from_root(args.html, root)
        artifact_dir = html.parent
    else:
        artifact_dir = resolve_from_root(args.artifact_dir or QUALITY_DIRECTORY, root)
        html = artifact_dir / REPORT_NAME
    if args.thresholds is None and not thresholds.exists():
        return RunFiles(config, None, artifact_dir, html, artifact_dir / STATE_NAME)
    return RunFiles(config, thresholds, artifact_dir, html, artifact_dir / STATE_NAME)


# --- Rerun commands -----------------------------------------------------------


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def rerun_command(
    args: argparse.Namespace, root: Path, files: RunFiles, *, full: bool = False
) -> str:
    """The command that repeats this run; ``full`` drops the scope, --fast, and
    the check flags (the run that certifies after a commit)."""
    command = [sys.executable, str(Path(__file__).resolve()), "--root", "."]
    command += _location_arguments(args, root, files)
    if not full:
        command += _scope_arguments(args)
    if args.no_install:
        command.append("--no-install")
    if args.allow_policy_change:
        command.append("--allow-policy-change")
    if args.allow_test_removal:
        command.append("--allow-test-removal")
    if not full:
        command += ["--fast"] if args.fast else []
        command += [f"--{flag}" for flag in selection_names(args)]
    return shlex.join(command)


def _location_arguments(
    args: argparse.Namespace, root: Path, files: RunFiles
) -> list[str]:
    arguments: list[str] = []
    if args.config:
        arguments += ["--config", display_path(files.config, root)]
    if args.thresholds and files.thresholds is not None:
        arguments += ["--thresholds", display_path(files.thresholds, root)]
    if args.html:
        arguments += ["--html", display_path(files.html, root)]
    elif args.artifact_dir:
        arguments += ["--artifact-dir", display_path(files.artifact_dir, root)]
    if args.gate_script:
        arguments += ["--gate-script", str(Path(args.gate_script).resolve())]
    return arguments


def _scope_arguments(args: argparse.Namespace) -> list[str]:
    if args.commit:
        return ["--commit", args.commit]
    if args.local_changes:
        return ["--local-changes"]
    return []


# --- One run ------------------------------------------------------------------


def change_set(args: argparse.Namespace, root: Path) -> ChangeSet:
    if args.commit:
        return diff_scope.commit_changes(root, args.commit)
    if args.local_changes:
        return diff_scope.local_changes(root)
    return diff_scope.repository_changes()


def scope_kind(args: argparse.Namespace) -> str:
    if args.commit:
        return diff_scope.COMMIT
    if args.local_changes:
        return diff_scope.LOCAL_CHANGES
    return diff_scope.REPOSITORY


def run_mode(args: argparse.Namespace) -> str:
    if selection_names(args):
        return PARTIAL
    return FAST if args.fast else FULL


def load_engine(gate_script: str | None) -> RunAnalysis:
    """``run_analysis`` from ``gate_script``, or the bundled engine."""
    if not gate_script:
        return quality_run.run_analysis
    path = Path(gate_script).resolve()
    spec = importlib.util.spec_from_file_location("quality_engine_override", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load quality engine: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine: Any = getattr(module, "run_analysis", None)
    if not callable(engine):
        raise ValueError(f"quality engine has no run_analysis: {path}")
    checked: RunAnalysis = engine
    return checked


def tool_cache_dir(config: dict[str, Any], root: Path) -> Path | None:
    value = (config.get("tools") or {}).get("cache_dir")
    return resolve_from_root(str(value), root) if value else None


def analyze(
    args: argparse.Namespace,
    root: Path,
    files: RunFiles,
    previous_state: dict[str, Any] | None,
) -> Analysis:
    """Run the gates. Raises OSError/ValueError/KeyError/TypeError for
    configuration and runner errors."""
    if args.config and not files.config.is_file():
        raise ValueError(f"configuration not found: {files.config}")
    changes = change_set(args, root)
    thresholds = load_thresholds(files.thresholds)
    config = load_config(files.config if files.config.is_file() else None)
    auto_install = bool(config["tools"].get("auto_install")) and not args.no_install
    engine = load_engine(args.gate_script)
    tools = bootstrap_tools(
        root, auto_install=auto_install, cache_dir=tool_cache_dir(config, root)
    )
    names = selection_names(args)
    analysis = engine(
        root,
        config,
        thresholds,
        changes=changes,
        selection=quality_run.selected_gates(names),
        fast=args.fast,
        allow_policy_change=args.allow_policy_change,
        allow_test_removal=args.allow_test_removal,
        previous_state=previous_state,
        tools=tools,
        runner=runner_for(root),
    )
    analysis.reference = args.commit or None
    if args.commit:
        analysis.scope = quality_run.scope_description(changes.kind, args.commit)
    return analysis


def error_analysis(args: argparse.Namespace, root: Path, message: str) -> Analysis:
    runner = GateRun(
        "runner",
        "Gate runner",
        BLOCKED,
        f"The quality-gate runner stopped: {message}",
        seconds=None,
    )
    return Analysis(
        root=str(root),
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        scope=quality_run.scope_description(scope_kind(args), args.commit),
        mode=run_mode(args),
        gates=[runner],
        version=VERSION,
        reference=args.commit or None,
        notes=["The run stopped before the gates could be evaluated."],
    )


def exit_code(analysis: Analysis) -> int:
    return 0 if analysis.status in ("pass", "selected_pass") else 1


def write_artifacts(
    analysis: Analysis, files: RunFiles, error: str | None
) -> dict[str, Any]:
    """Write the HTML report and the state file; return the state."""
    files.artifact_dir.mkdir(parents=True, exist_ok=True)
    state = analysis_state(analysis, str(files.html))
    state["state_path"] = str(files.state)
    if error:
        state["status"] = "error"
        state["error"] = error
    files.html.write_text(quality_report.render_html(state), encoding="utf-8")
    write_json_atomic(files.state, state)
    return state


def previous_state_for(path: Path) -> dict[str, Any] | None:
    state = read_state(path)
    return state if state and state.get("schema_version") == 2 else None


def run_locked(args: argparse.Namespace, root: Path) -> int:
    files = resolve_run_files(args, root)
    previous = previous_state_for(files.state)
    error: str | None = None
    try:
        analysis = analyze(args, root, files, previous)
        code = exit_code(analysis)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as raised:
        error = str(raised) or type(raised).__name__
        analysis = error_analysis(args, root, error)
        code = 2
    analysis.rerun_command = rerun_command(args, root, files)
    analysis.full_rerun_command = rerun_command(args, root, files, full=True)
    state = write_artifacts(analysis, files, error)
    quality_report.print_report(state, previous_state=previous, stream=sys.stdout)
    return code


# --- Run lock and interrupts ----------------------------------------------------


def raise_keyboard_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def install_interrupt_handlers() -> None:
    signal.signal(signal.SIGINT, raise_keyboard_interrupt)
    signal.signal(signal.SIGTERM, raise_keyboard_interrupt)


def default_artifact_dir(root: Path) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return cache / "repo-quality-loop" / f"{root.name}-{digest}"


def repository_lock_path(root: Path) -> Path:
    """One stable lock path per repository, whatever the report destination."""
    return default_artifact_dir(root.resolve()) / ".run.lock"


def try_lock(handle: TextIO) -> bool:
    if os.name == "nt":
        return _try_windows_lock(handle)
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _try_windows_lock(handle: TextIO) -> bool:
    import msvcrt

    windows: Any = msvcrt
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)
    try:
        windows.locking(handle.fileno(), windows.LK_NBLCK, 1)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EDEADLK}:
            return False
        raise
    return True


def unlock(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        windows: Any = msvcrt
        handle.seek(0)
        windows.locking(handle.fileno(), windows.LK_UNLCK, 1)
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
    if not isinstance(owner, dict):
        return raw
    return f"PID {owner.get('pid', 'unknown')}, started {owner.get('started_at', 'unknown time')}"


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
            owner = {
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            }
            json.dump(owner, handle)
            handle.flush()
            yield
        finally:
            unlock(handle)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: repository root does not exist: {root}", file=sys.stderr)
        return 2
    if args.init:
        return project_setup.run_init(root, args)
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
