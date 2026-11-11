"""Run the ten generic gates once and collect an ``Analysis``.

Gates run in report order. Shared measurements are made once and only when a
gate that runs needs them: the test command (tests, coverage, complexity)
and lizard's function list (coverage, complexity, dead code).
"""

from __future__ import annotations

import dataclasses
import functools
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import gate_complexity
import gate_complexity_lizard
import gate_coverage
import gate_dead_code
import gate_dead_code_references
import gate_duplication
import gate_file_size
import gate_project_checks
import gate_project_checks_inference
import gate_secrets
import gate_shortcuts
import gate_vulnerabilities
import source_files
from diff_scope import COMMIT, LOCAL_CHANGES, REPOSITORY, ChangeSet
from gate_complexity_lizard import AnalysisError
from gate_types import (
    BLOCKED,
    DEFERRED,
    FAIL,
    OFF,
    PASS,
    SKIPPED,
    FunctionInfo,
    GateOutcome,
    Runner,
)
from quality_config import CONFIG_NAME, DEFAULT_CHECK_TIMEOUT, VERSION, gate_choices
from quality_model import FAST, FULL, GATE_ORDER, PARTIAL, Analysis, GateRun
from quality_run_tests import TestRunRequest, TestRunResult, run_tests
from quality_tools import ToolSet

# (flag, gate key, help). Several flags may select one gate.
CHECK_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("shortcuts", "shortcuts", "suppressions, hidden files, silent handlers"),
    ("scope", "shortcuts", "alias of --shortcuts"),
    ("silent-errors", "shortcuts", "alias of --shortcuts"),
    ("secrets", "secrets", "credentials in text files"),
    ("loc", "file_loc", "file size"),
    ("checks", "project_checks", "the project's lint, type, format and build checks"),
    ("lint", "project_checks", "alias of --checks"),
    ("types", "project_checks", "alias of --checks"),
    ("tests", "tests", "the test suite"),
    ("test-count", "tests", "alias of --tests"),
    ("coverage", "coverage", "line and branch coverage (runs the tests)"),
    ("branches", "coverage", "alias of --coverage"),
    ("failure-paths", "coverage", "alias of --coverage"),
    ("complexity", "complexity", "cyclomatic, cognitive, nesting, CRAP (runs tests)"),
    ("crap", "complexity", "alias of --complexity"),
    ("cognitive", "complexity", "alias of --complexity"),
    ("duplication", "duplication", "copied code"),
    ("dead-code", "dead_code", "unused functions"),
    ("vulnerabilities", "vulnerabilities", "known advisories for locked dependencies"),
)
FLAG_GATES = {flag: key for flag, key, _ in CHECK_FLAGS}


def selected_gates(flags: Sequence[str]) -> frozenset[str] | None:
    """The gate keys the check flags select; None (every gate) when no flag
    is given. Coverage and complexity need the test run, so they add tests."""
    if not flags:
        return None
    keys = {FLAG_GATES[flag] for flag in flags}
    if keys & {"coverage", "complexity"}:
        keys.add("tests")
    return frozenset(keys)


@dataclasses.dataclass
class _Run:
    """One run's inputs and the measurements gates share."""

    root: Path
    config: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    changes: ChangeSet
    tools: ToolSet
    runner: Runner
    out_dir: Path
    allow_policy_change: bool
    allow_test_removal: bool
    previous_executed: int | None

    @property
    def source_config(self) -> dict[str, Any]:
        return dict(self.config.get("source") or {})

    @functools.cached_property
    def every_source(self) -> list[Path]:
        return source_files.discover_source_files(self.root, self.source_config)

    @functools.cached_property
    def scoped_source(self) -> list[Path]:
        return source_files.discover_source_files(
            self.root, self.source_config, self.changes
        )

    @functools.cached_property
    def lizard(self) -> tuple[list[FunctionInfo], list[str], list[AnalysisError]]:
        return gate_complexity_lizard.analyze_functions(
            self.tools.tool_python, self.root, self.every_source
        )

    @functools.cached_property
    def tests(self) -> TestRunResult:
        known = [
            source_files.normalize_path(path, self.root) for path in self.every_source
        ]
        return run_tests(
            TestRunRequest(
                self.root,
                self.config,
                self.thresholds,
                self.tools,
                self.runner,
                self.out_dir,
                known,
                self.previous_executed,
                self.allow_test_removal,
            )
        )


def run_analysis(
    root: Path,
    config: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    *,
    changes: ChangeSet,
    selection: frozenset[str] | None,
    fast: bool,
    allow_policy_change: bool,
    allow_test_removal: bool,
    previous_state: Mapping[str, Any] | None,
    tools: ToolSet,
    runner: Runner,
) -> Analysis:
    """Run every enabled and selected gate in report order.

    Disabled gates are OFF, unselected gates SKIPPED, and ``fast`` defers only
    the network-bound vulnerabilities gate."""
    enabled, notes = gate_choices(config)
    with tempfile.TemporaryDirectory(prefix="quality-run-") as out_dir:
        run = _Run(
            root,
            config,
            thresholds,
            changes,
            tools,
            runner,
            Path(out_dir),
            allow_policy_change,
            allow_test_removal,
            previous_test_count(previous_state, changes.kind),
        )
        gates = [_gate(run, key, enabled[key], selection, fast) for key in GATE_ORDER]
    analysis = Analysis(
        root=str(root),
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        scope=scope_description(changes.kind),
        mode=PARTIAL if selection is not None else FAST if fast else FULL,
        gates=gates,
        version=VERSION,
        notes=[*tools.setup_notes, *notes],
    )
    analysis.baselines = updated_baselines(previous_state, analysis, changes.kind)
    return analysis


def _gate(
    run: _Run, key: str, enabled: bool, selection: frozenset[str] | None, fast: bool
) -> GateRun:
    if not enabled:
        return GateRun.not_run(
            key, OFF, f"Off: disabled for this project in {CONFIG_NAME} (gates.{key})."
        )
    if selection is not None and key not in selection:
        return GateRun.not_run(key, SKIPPED, "Skipped: not selected by this run.")
    if fast and key == "vulnerabilities":
        return GateRun.not_run(
            key, DEFERRED, "Deferred: --fast skips the advisory lookup."
        )
    started = time.monotonic()
    outcome = GATE_EVALUATORS[key](run)
    return GateRun.from_outcome(outcome, round(time.monotonic() - started, 3))


# --- Gate evaluators ----------------------------------------------------------


def _shortcuts(run: _Run) -> GateOutcome:
    return gate_shortcuts.evaluate_shortcuts(
        run.root,
        run.changes,
        run.source_config,
        allow_policy_change=run.allow_policy_change,
    )


def _secrets(run: _Run) -> GateOutcome:
    return gate_secrets.evaluate_secrets(run.root, run.changes)


def _file_loc(run: _Run) -> GateOutcome:
    limit = int(run.thresholds["file_loc"]["max_lines"])
    return gate_file_size.evaluate_file_size(
        run.root, run.scoped_source, run.changes, limit
    )


def _project_checks(run: _Run) -> GateOutcome:
    section = gate_project_checks_inference.project_checks_section(run.config)
    specs = gate_project_checks_inference.project_check_specs(run.root, section)
    timeout = int(section.get("timeout_seconds") or DEFAULT_CHECK_TIMEOUT)
    return gate_project_checks.run_project_checks(run.root, specs, run.runner, timeout)


def _tests(run: _Run) -> GateOutcome:
    return run.tests.outcome


def _coverage(run: _Run) -> GateOutcome:
    tests = run.tests
    return gate_coverage.evaluate_coverage(
        run.root,
        run.scoped_source,
        run.changes,
        tests.data,
        run.lizard[0],
        run.thresholds,
        report_fresh=tests.report_fresh,
        problem=tests.problem,
        prerequisite=tests.prerequisite,
        measures=tests.measures,
    )


def _complexity(run: _Run) -> GateOutcome:
    tests = run.tests
    lookup = None
    if tests.data is not None and tests.report_fresh:
        lookup = gate_coverage.coverage_lookup(tests.data, tests.measures)
    functions, unsupported, errors = run.lizard
    return gate_complexity.evaluate_complexity(
        functions,
        run.changes,
        lookup,
        run.thresholds,
        unsupported=unsupported,
        errors=errors,
    )


def _duplication(run: _Run) -> GateOutcome:
    return gate_duplication.run_duplication(
        run.tools.tool_python, run.root, run.every_source, run.changes, run.thresholds
    )


def _dead_code(run: _Run) -> GateOutcome:
    functions, _, errors = run.lizard
    failures = [str(error) for error in errors if error.path is None]
    if failures and not functions:
        summary = f"Lizard could not list the functions: {failures[0]}"
        return GateOutcome("dead_code", "Dead code", BLOCKED, summary.splitlines()[0])
    references = gate_dead_code_references.discover_reference_files(run.root)
    return gate_dead_code.evaluate_dead_code(
        run.root, functions, run.changes, references
    )


def _vulnerabilities(run: _Run) -> GateOutcome:
    return gate_vulnerabilities.evaluate_vulnerabilities(run.root, run.changes)


GATE_EVALUATORS: dict[str, Callable[[_Run], GateOutcome]] = {
    "shortcuts": _shortcuts,
    "secrets": _secrets,
    "file_loc": _file_loc,
    "project_checks": _project_checks,
    "tests": _tests,
    "coverage": _coverage,
    "complexity": _complexity,
    "duplication": _duplication,
    "dead_code": _dead_code,
    "vulnerabilities": _vulnerabilities,
}


# --- Test-count baseline ------------------------------------------------------


def previous_test_count(
    previous_state: Mapping[str, Any] | None, scope_kind: str
) -> int | None:
    """The executed test count of the last full run in the same scope kind."""
    if not previous_state or previous_state.get("schema_version") != 2:
        return None
    baselines = previous_state.get("baselines")
    if not isinstance(baselines, Mapping):
        return None
    count = baselines.get("tests_executed")
    if baselines.get("tests_scope") != scope_kind or not isinstance(count, int):
        return None
    return count


def updated_baselines(
    previous_state: Mapping[str, Any] | None, analysis: Analysis, scope_kind: str
) -> dict[str, Any]:
    """Carry the previous baselines forward; a full run whose tests gate passed
    or failed on measured counts records its executed count."""
    baselines: dict[str, Any] = {}
    if previous_state and previous_state.get("schema_version") == 2:
        baselines = dict(previous_state.get("baselines") or {})
    executed = _measured_test_count(analysis)
    if analysis.mode == FULL and executed is not None:
        baselines["tests_executed"] = executed
        baselines["tests_scope"] = scope_kind
    return baselines


def _measured_test_count(analysis: Analysis) -> int | None:
    """The count to remember. A failed run never lowers it: a dropped count
    must fail again until the user approves the removal."""
    for gate in analysis.gates:
        if gate.key != "tests" or gate.status not in (PASS, FAIL):
            continue
        executed = gate.measurements.get("executed")
        if not gate.measurements.get("measured") or not isinstance(executed, int):
            return None
        previous = gate.measurements.get("previous_executed")
        if gate.status == FAIL and isinstance(previous, int):
            return max(executed, previous)
        return executed
    return None


SCOPE_DESCRIPTIONS = {
    REPOSITORY: "whole repository",
    LOCAL_CHANGES: "local changes",
    COMMIT: "commit",
}


def scope_description(kind: str, reference: str | None = None) -> str:
    """``whole repository``, ``local changes`` or ``commit <ref>``."""
    description = SCOPE_DESCRIPTIONS.get(kind, kind)
    return f"{description} {reference}" if reference else description
