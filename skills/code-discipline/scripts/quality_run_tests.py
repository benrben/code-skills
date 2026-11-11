"""One test run that feeds the tests, coverage, and complexity gates.

The project's test command runs once, wrapped by the coverage recipe for its
runtime. The tests gate judges that run; the coverage gate reads the report
it wrote; the complexity gate uses that coverage for CRAP scores. The run
must not change the project's dependencies: manifests, lockfiles, and the
markers package managers write on restore are fingerprinted around it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shlex
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import gate_coverage_readers
import gate_coverage_recipes
import gate_tests
import gate_tests_native
from gate_coverage_readers import CoverageData
from gate_tests_command import infer_test_command, native_toolchain_problem
from gate_types import FAIL, CommandRun, GateItem, GateOutcome, Runner
from quality_commands import command_list, project_execution_env
from quality_config import DEFAULT_TEST_TIMEOUT
from quality_tools import ToolSet
from source_files import normalize_path

DEPENDENCY_STATE_FILES = frozenset(
    {
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile.lock",
        "Package.resolved",
        "Pipfile.lock",
        "composer.lock",
        "go.mod",
        "go.sum",
        "mix.lock",
        "package-lock.json",
        "package.json",
        "packages.lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pubspec.lock",
        "pylock.toml",
        "pyproject.toml",
        "uv.lock",
        "yarn.lock",
    }
)
DEPENDENCY_DIRECTORY_MARKERS = {
    ".dart_tool": ("package_config.json",),
    ".venv": ("pyvenv.cfg",),
    "node_modules": (".package-lock.json", ".modules.yaml", ".yarn-state.yml"),
    "obj": ("project.assets.json",),
    "venv": ("pyvenv.cfg",),
    "vendor": ("autoload.php",),
}
REQUIREMENTS_FILE = re.compile(r"requirements(?:[-_.][^/]*)?\.txt", re.IGNORECASE)
PREREQUISITE_PATTERNS = {
    "project dependencies are missing": (
        "modulenotfounderror",
        "cannot find module",
        "could not resolve dependencies",
        "dependency resolution failed",
        "no matching package named",
        "package restore failed",
        "assets file",
    ),
    "the required runtime or test command is missing": (
        "command not found",
        "no such file or directory",
        "is not recognized as an internal or external command",
    ),
    "the dependency cache is incomplete and network restore is disabled": (
        "offline mode",
        "offline and the package",
        "not available in offline mode",
        "failed to download",
    ),
}
STEP_TIMEOUT = 600
BUILD_FAILED = "the project did not build, so coverage cannot be measured"

Command = tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TestRunResult:
    """The tests gate's outcome plus what the coverage gate needs."""

    outcome: GateOutcome
    data: CoverageData | None = None
    report_fresh: bool = False
    problem: str = ""
    prerequisite: str = ""
    measures: frozenset[str] | None = None


@dataclasses.dataclass(frozen=True)
class TestRunRequest:
    root: Path
    config: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    tools: ToolSet
    runner: Runner
    out_dir: Path
    known_paths: Sequence[str]
    previous_executed: int | None = None
    allow_test_removal: bool = False


def project_test_command(root: Path, config: Mapping[str, Any]) -> list[str] | None:
    """The configured test command, else the repository's conventional one."""
    section = config.get("test") or {}
    return command_list(section.get("command")) or infer_test_command(root)


def run_tests(request: TestRunRequest) -> TestRunResult:
    """Run the test command once under the coverage recipe."""
    command = project_test_command(request.root, request.config)
    if command is None:
        return _no_test_command(request.root)
    plan = gate_coverage_recipes.plan_coverage(
        request.root,
        command,
        request.out_dir,
        request.tools.coverage_tools,
        configured=request.config.get("coverage") or None,
        environ={**os.environ, **project_execution_env()},
    )
    if isinstance(plan, str):
        return _run_plain(request, command, plan)
    return _run_with_recipe(request, plan)


def _no_test_command(root: Path) -> TestRunResult:
    toolchain = native_toolchain_problem(root)
    summary = "No test command was found."
    if toolchain:
        summary = f"No test command was found: {toolchain}."
    outcome = GateOutcome(gate_tests.KEY, gate_tests.TITLE, "blocked", summary)
    return TestRunResult(
        outcome, problem="no test command was found, so coverage cannot be measured"
    )


def _run_plain(
    request: TestRunRequest,
    command: Sequence[str],
    problem: str,
    build: tuple[Command, ...] = (),
) -> TestRunResult:
    steps, command = _with_build(request, command, build)
    failure = _build_failure(request, steps, {})
    if failure is not None:
        return TestRunResult(failure, problem=problem)
    outcome, _ = _judged_run(request, command, {})
    return TestRunResult(outcome, problem=problem)


def _with_build(
    request: TestRunRequest, command: Sequence[str], steps: tuple[Command, ...]
) -> tuple[tuple[Command, ...], Sequence[str]]:
    """The build steps and the test command: the recipe's own steps, else
    those of a plain CMake or Meson build (``gate_tests_native``)."""
    if steps:
        return steps, command
    build = gate_tests_native.native_build(
        request.root, command, request.out_dir, coverage=False
    )
    return (build.steps, build.command) if build else ((), command)


def _build_failure(
    request: TestRunRequest, steps: Sequence[Sequence[str]], env: Mapping[str, str]
) -> GateOutcome | None:
    """The tests gate's outcome for the first build step that fails, or None
    when every step succeeded."""
    for step in steps:
        run = request.runner(step, _test_timeout(request), env)
        if not run.succeeded:
            outcome = gate_tests.evaluate_tests(
                run,
                gate_tests.parse_test_execution(run.output),
                request.thresholds,
                blocked_reason=blocked_prerequisite(run.output),
            )
            if outcome.status == FAIL and not run.timed_out:
                outcome.summary = (
                    f"The build step `{shlex.join(step)}` exited with code "
                    f"{run.returncode}."
                )
            return outcome
    return None


def _test_timeout(request: TestRunRequest) -> int:
    section = request.config.get("test") or {}
    return int(section.get("timeout_seconds") or DEFAULT_TEST_TIMEOUT)


def _run_with_recipe(
    request: TestRunRequest, recipe: gate_coverage_recipes.Recipe
) -> TestRunResult:
    gate_coverage_recipes.prepare_recipe(recipe)
    setup_problem = _run_steps(request.runner, recipe.setup, recipe.env)
    if setup_problem:
        problem = f"coverage setup failed: {setup_problem}"
        return _run_plain(request, recipe.command, problem, recipe.build)
    steps, command = _with_build(request, recipe.command, recipe.build)
    failure = _build_failure(request, steps, recipe.env)
    if failure is not None:
        return TestRunResult(failure, problem=BUILD_FAILED, measures=recipe.measures)
    outcome, began = _judged_run(request, command, recipe.env)
    finalize_problem = _run_steps(request.runner, recipe.finalize, recipe.env)
    data, fresh, problem = _read_report(request, recipe, began)
    if finalize_problem:
        step = f"coverage report step failed: {finalize_problem}"
        problem = f"{step}; {problem}" if problem else step
    return TestRunResult(
        outcome,
        data,
        fresh,
        problem,
        recipe.prerequisite,
        recipe.measures,
    )


def _judged_run(
    request: TestRunRequest, command: Sequence[str], env: Mapping[str, str]
) -> tuple[GateOutcome, float]:
    section = request.config.get("test") or {}
    before = project_dependency_state(request.root)
    began = time.time()
    run = request.runner(command, _test_timeout(request), env)
    changed = changed_dependency_state(before, project_dependency_state(request.root))
    execution = gate_tests.measure_test_run(
        request.root, run, began, gate_tests.junit_patterns(section)
    )
    outcome = gate_tests.evaluate_tests(
        run,
        execution,
        request.thresholds,
        previous_executed=request.previous_executed,
        allow_test_removal=request.allow_test_removal,
        blocked_reason=None if run.succeeded else blocked_prerequisite(run.output),
    )
    return with_dependency_changes(outcome, changed), began


def _run_steps(
    runner: Runner, steps: Sequence[Sequence[str]], env: Mapping[str, str]
) -> str:
    """Run setup or finalize commands in order; the first failure, or ""."""
    for step in steps:
        run = runner(step, STEP_TIMEOUT, env)
        if not run.succeeded:
            return _failure_text(run)
    return ""


def _read_report(
    request: TestRunRequest, recipe: gate_coverage_recipes.Recipe, began: float
) -> tuple[CoverageData | None, bool, str]:
    fresh = gate_coverage_readers.report_is_fresh(recipe.report, began, recipe.format)
    try:
        data = gate_coverage_readers.read_coverage_report(
            recipe.report,
            recipe.format,
            request.root,
            request.known_paths,
            started_at=began,
        )
    except gate_coverage_readers.CoverageReportError as error:
        return None, fresh, str(error)
    return data, fresh, ""


def _failure_text(run: CommandRun) -> str:
    lines = [line for line in run.output.strip().splitlines() if line.strip()]
    last = lines[-1] if lines else f"exit {run.returncode}"
    return f"{_step_name(run.command)}: {last}"


def _step_name(command: Sequence[str]) -> str:
    """The program a step ran: the script, when an interpreter ran one."""
    names = [Path(part).name for part in command[:2]]
    if len(names) == 2 and names[0].startswith(("python", "node", "ruby")):
        return names[1]
    return names[0] if names else "step"


def with_dependency_changes(
    outcome: GateOutcome, changed: Sequence[str]
) -> GateOutcome:
    """Fail the tests gate when the test run changed dependency state."""
    if not changed:
        return outcome
    item = GateItem(
        gate_tests.RUN_LOCATION,
        None,
        "",
        "dependency files changed by the test run",
        ", ".join(changed),
        "none",
    )
    return dataclasses.replace(
        outcome,
        status=FAIL,
        summary="The test command changed the project's dependencies: "
        + ", ".join(changed),
        details=[*outcome.details, item.describe()],
        items=[*outcome.items, item],
    )


def blocked_prerequisite(output: str) -> str | None:
    """Why a failed run could not test anything: a missing dependency,
    runtime, or offline cache. None when the output names no such cause."""
    lowered = output.lower()
    for reason, needles in PREREQUISITE_PATTERNS.items():
        if any(needle in lowered for needle in needles):
            return reason
    return None


def project_dependency_state(root: Path) -> dict[str, str]:
    """Digests of manifests, lockfiles, and restore markers, without walking
    into large dependency trees."""
    state: dict[str, str] = {}
    for directory, names, files in os.walk(root):
        path = Path(directory)
        markers = DEPENDENCY_DIRECTORY_MARKERS.get(path.name, ())
        for filename in files:
            if _is_dependency_file(filename, markers):
                _record(state, root, path / filename)
        _record_markers(state, root, path, names)
        names[:] = [
            name
            for name in names
            if name not in DEPENDENCY_DIRECTORY_MARKERS and name != ".git"
        ]
    return state


def _is_dependency_file(filename: str, markers: Sequence[str]) -> bool:
    return (
        filename in DEPENDENCY_STATE_FILES
        or filename in markers
        or REQUIREMENTS_FILE.fullmatch(filename) is not None
    )


def _record_markers(
    state: dict[str, str], root: Path, path: Path, names: Sequence[str]
) -> None:
    for name in set(DEPENDENCY_DIRECTORY_MARKERS) & set(names):
        for marker in DEPENDENCY_DIRECTORY_MARKERS[name]:
            _record(state, root, path / name / marker)


def _record(state: dict[str, str], root: Path, path: Path) -> None:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        # Missing or unreadable markers simply do not take part.
        return
    state[normalize_path(path, root)] = digest


def changed_dependency_state(
    before: Mapping[str, str], after: Mapping[str, str]
) -> list[str]:
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
