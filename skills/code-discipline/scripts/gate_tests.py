"""Tests gate: the project's own test command must run, pass, and run everything.

The gate never trusts an exit code alone. It reads the runner's summary (see
``gate_tests_parsers``) or, for JVM builds that print none, fresh JUnit XML
reports, and fails when tests fail, are skipped or filtered out, are too few,
or when the count dropped since the last full run without the user's consent.
Output with no recognizable summary blocks the gate instead of passing it.

``infer_test_command`` (from ``gate_tests_command``) finds the conventional
test command from what the repository declares; it never installs anything.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import gate_tests_parsers as parsers
import gate_tests_parsers_other as other_parsers
import source_files
from gate_tests_command import (
    find_executable,
    infer_test_command,
    package_manager,
    project_python,
)
from gate_tests_parsers import TestExecution, clean_output, measured_execution
from gate_types import (
    BLOCKED,
    FAIL,
    PASS,
    CommandRun,
    GateItem,
    GateOutcome,
    Status,
    plural,
)

__all__ = [
    "DEFAULT_JUNIT_PATTERNS",
    "KEY",
    "TITLE",
    "TestExecution",
    "combine_executions",
    "evaluate_tests",
    "find_executable",
    "fresh_junit_reports",
    "infer_test_command",
    "measure_test_run",
    "package_manager",
    "parse_test_execution",
    "project_python",
    "read_junit_reports",
]

KEY = "tests"
TITLE = "Tests"

# Findings are about the test run as a whole, not one file.
RUN_LOCATION = "test run"
OUTPUT_TAIL_LINES = 40
MISSING_EXECUTABLE_EXIT = 127

DEFAULT_JUNIT_PATTERNS = (
    "**/build/test-results/**/*.xml",
    "**/target/surefire-reports/*.xml",
    "**/target/failsafe-reports/*.xml",
)

# JUnit reports live in build output folders, which file discovery skips.
# Configured globs may also name a skipped folder, such as jest-junit's
# ``coverage/junit.xml``; see ``_pruned_dirs``.
_JUNIT_SKIPPED_DIRS = source_files.DEFAULT_EXCLUDED_DIRS - {"build", "target"}

# --- Go -----------------------------------------------------------------------


def _go_test_event(line: str) -> tuple[str, str, str] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        # go test -json interleaves plain build output; those lines are not events.
        return None
    if not isinstance(value, dict):
        return None
    action = value.get("Action")
    test = value.get("Test")
    if action not in {"pass", "fail", "skip"} or not isinstance(test, str):
        return None
    return str(value.get("Package", "")), test, action


def parse_go_json(text: str) -> TestExecution | None:
    """``go test -json``: the last terminal event of each (package, test)."""
    outcomes: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        event = _go_test_event(line)
        if event is not None:
            package, test, action = event
            outcomes[(package, test)] = action
    if not outcomes:
        return None
    counts = Counter(outcomes.values())
    return measured_execution(
        "go-json",
        executed=len(outcomes),
        passed=counts["pass"],
        failed=counts["fail"],
        skipped=counts["skip"],
    )


_GO_PACKAGE = re.compile(r"^(ok|FAIL)\s+\S+\s+(?:\d[\d.]*m?s|\(cached\))", re.MULTILINE)


def parse_go_packages(text: str) -> TestExecution | None:
    """Plain ``go test`` prints one line per package, not per test, so the
    counts are packages and the execution stays unmeasured."""
    verdicts = Counter(_GO_PACKAGE.findall(text))
    if not verdicts:
        return None
    return TestExecution(
        executed=verdicts["ok"] + verdicts["FAIL"],
        passed=verdicts["ok"],
        failed=verdicts["FAIL"],
        parser="go-packages",
    )


# --- Parsing console output ---------------------------------------------------

Parser = Callable[[str], "TestExecution | None"]

# One entry per runner family. Within a family the first parser that matches
# wins, because the later ones read a less complete form of the same runner's
# output. Across families the counts add up: one command may run several
# runners (a monorepo, ``unittest && pytest``), and every one is judged.
PARSER_FAMILIES: tuple[tuple[Parser, ...], ...] = (
    (parse_go_json, parse_go_packages),
    (parsers.parse_cargo,),
    (parsers.parse_unittest,),
    (parsers.parse_vitest,),
    (parsers.parse_jest,),
    (parsers.parse_pytest,),
    (other_parsers.parse_phpunit,),
    (other_parsers.parse_maven,),
    (other_parsers.parse_dotnet,),
    (other_parsers.parse_swift,),
    (other_parsers.parse_rspec,),
    (other_parsers.parse_ruby_xunit,),
    (other_parsers.parse_exunit,),
    (parsers.parse_mocha,),
    (other_parsers.parse_dart,),
    (other_parsers.parse_ctest,),
    (other_parsers.parse_meson,),
    (parsers.parse_tap_summary, parsers.parse_tap_lines),
)


def _family_execution(family: Sequence[Parser], text: str) -> TestExecution | None:
    for parser in family:
        execution = parser(text)
        if execution is not None:
            return execution
    return None


def _total(executions: Sequence[TestExecution], field: str) -> int:
    return sum(int(getattr(execution, field)) for execution in executions)


def combine_executions(executions: Sequence[TestExecution]) -> TestExecution:
    """One measured execution adding up several runners' counts; the parser
    names are joined with ``+`` so the report shows every runner."""
    if len(executions) == 1:
        return executions[0]
    return TestExecution(
        executed=_total(executions, "executed"),
        passed=_total(executions, "passed"),
        failed=_total(executions, "failed"),
        skipped=_total(executions, "skipped"),
        xfailed=_total(executions, "xfailed"),
        retried=_total(executions, "retried"),
        parser="+".join(execution.parser for execution in executions),
        measured=True,
        deselected=_total(executions, "deselected"),
    )


def parse_test_execution(output: str) -> TestExecution:
    """Every runner summary in the output, added up across runners.

    Unknown output never implies that tests ran. A runner whose summary
    cannot be measured (plain ``go test``) is reported only when no other
    runner was measured; the exit code still catches its failures.
    """
    text = clean_output(output)
    found = [
        execution
        for family in PARSER_FAMILIES
        if (execution := _family_execution(family, text)) is not None
    ]
    measured = [execution for execution in found if execution.measured]
    if measured:
        return combine_executions(measured)
    return found[0] if found else TestExecution(parser=parsers.UNKNOWN_PARSER)


# --- Measuring a run ----------------------------------------------------------


def _attribute_count(element: ElementTree.Element, name: str) -> int:
    value = element.get(name, "0") or "0"
    try:
        return int(float(value))
    except ValueError:
        return 0


def _junit_suites(path: Path) -> list[ElementTree.Element]:
    """The innermost ``<testsuite>`` elements of one report, so nested
    suites and ``<testsuites>`` totals are not counted twice. A file that is
    not readable XML contributes nothing."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return []
    leaves = [
        suite for suite in root.iter("testsuite") if suite.find("testsuite") is None
    ]
    if not leaves and root.tag == "testsuites" and root.get("tests"):
        return [root]
    return leaves


def read_junit_reports(paths: Iterable[Path]) -> TestExecution | None:
    """Sum ``tests``/``failures``/``errors``/``skipped`` across JUnit XML
    reports (Gradle, Maven Surefire, or configured globs); None when no file
    holds a test suite."""
    suites = [suite for path in paths for suite in _junit_suites(path)]
    if not suites:
        return None
    executed = sum(_attribute_count(suite, "tests") for suite in suites)
    failed = sum(
        _attribute_count(suite, "failures") + _attribute_count(suite, "errors")
        for suite in suites
    )
    skipped = sum(
        _attribute_count(suite, "skipped") + _attribute_count(suite, "disabled")
        for suite in suites
    )
    return measured_execution(
        "junit", executed=executed, failed=failed, skipped=skipped
    )


def fresh_junit_reports(
    root: Path, since: float, patterns: Sequence[str] = DEFAULT_JUNIT_PATTERNS
) -> list[Path]:
    """JUnit XML files matching ``patterns`` written at or after ``since``
    (a ``time.time()`` value), so reports from earlier runs never count."""
    found: list[Path] = []
    pruned = _pruned_dirs(patterns)
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in pruned)
        for filename in sorted(files):
            path = Path(current) / filename
            relative = path.relative_to(root).as_posix()
            if source_files.matches_any(relative, patterns) and _modified_since(
                path, since
            ):
                found.append(path)
    return found


def _pruned_dirs(patterns: Sequence[str]) -> frozenset[str]:
    """Dependency and cache folders the search skips, except those a pattern
    names literally (``coverage/junit.xml``, ``out/test-results/*.xml``)."""
    named = {segment for pattern in patterns for segment in pattern.split("/")}
    return _JUNIT_SKIPPED_DIRS - named


def _modified_since(path: Path, since: float) -> bool:
    try:
        return path.stat().st_mtime >= since
    except OSError:
        return False


def measure_test_run(
    root: Path,
    run: CommandRun,
    started_at: float,
    junit_patterns: Sequence[str] = (),
) -> TestExecution:
    """Counts from the console summary, else from JUnit reports the run wrote.

    ``junit_patterns`` are configured globs searched in addition to the
    Gradle and Maven defaults.
    """
    execution = parse_test_execution(run.output)
    if execution.measured:
        return execution
    patterns = (*DEFAULT_JUNIT_PATTERNS, *junit_patterns)
    junit = read_junit_reports(fresh_junit_reports(root, started_at, patterns))
    return junit if junit is not None else execution


# --- Evaluation ---------------------------------------------------------------


def _section(thresholds: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept the ``tests`` section or the whole thresholds document."""
    section = thresholds.get(KEY)
    return section if isinstance(section, Mapping) else thresholds


def _limit(section: Mapping[str, Any], name: str, default: int) -> int:
    value = section.get(name, default)
    return value if isinstance(value, int) else default


def _item(metric: str, value: str, limit: str) -> GateItem:
    return GateItem(RUN_LOCATION, None, "", metric, value, limit)


def _skip_value(execution: TestExecution) -> str:
    parts = [f"{execution.skipped} skipped"]
    if execution.xfailed:
        parts.append(f"including {execution.xfailed} expected failures")
    parts.append(f"{execution.deselected} deselected or filtered out")
    return f"{execution.skipped + execution.deselected} ({', '.join(parts)})"


def threshold_items(
    execution: TestExecution,
    section: Mapping[str, Any],
    previous_executed: int | None,
    allow_test_removal: bool,
) -> list[GateItem]:
    """Every threshold the measured run broke."""
    items: list[GateItem] = []
    max_failures = _limit(section, "max_failures", 0)
    if execution.failed > max_failures:
        items.append(
            _item(
                "failed tests",
                str(execution.failed),
                str(max_failures),
            )
        )
    max_skipped = _limit(section, "max_skipped", 0)
    if execution.skipped + execution.deselected > max_skipped:
        items.append(
            _item(
                "skipped/deselected tests",
                _skip_value(execution),
                str(max_skipped),
            )
        )
    min_executed = _limit(section, "min_executed", 1)
    if execution.executed < min_executed:
        items.append(
            _item(
                "executed tests",
                str(execution.executed),
                f">= {min_executed}",
            )
        )
    dropped = previous_executed is not None and execution.executed < previous_executed
    if dropped and not allow_test_removal:
        items.append(
            _item(
                "test count",
                f"dropped from {previous_executed} to {execution.executed}",
                f">= {previous_executed}",
            )
        )
    return items


def _measurements(
    run: CommandRun, execution: TestExecution, previous_executed: int | None
) -> dict[str, Any]:
    return {
        "executed": execution.executed,
        "passed": execution.passed,
        "failed": execution.failed,
        "skipped": execution.skipped,
        "deselected": execution.deselected,
        "xfailed": execution.xfailed,
        "retried": execution.retried,
        "duration_seconds": round(run.duration_seconds, 3),
        "parser": execution.parser,
        "measured": execution.measured,
        "returncode": run.returncode,
        "previous_executed": previous_executed,
    }


def _counts_text(execution: TestExecution) -> str:
    return (
        f"{plural(execution.executed, 'test')} ran ({execution.parser}): "
        f"{execution.passed} passed, {execution.failed} failed, "
        f"{execution.skipped} skipped, {execution.deselected} deselected"
    )


def output_tail(output: str, limit: int = OUTPUT_TAIL_LINES) -> str:
    """The last non-empty lines of the run, where runners put failures."""
    lines = [line for line in clean_output(output).splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _run_problem(
    run: CommandRun, execution: TestExecution, blocked_reason: str | None
) -> tuple[Status, str] | None:
    """(status, summary) when the process itself did not finish cleanly."""
    command = shlex.join(run.command)
    if run.timed_out:
        return FAIL, (
            f"The test command `{command}` timed out after {run.duration_seconds:.0f}s."
        )
    if run.returncode == 0:
        return None
    # Tests that ran and failed are the agent's to fix, even when the output
    # also mentions something that looks like a missing prerequisite.
    tests_ran = execution.measured and (execution.executed or execution.failed)
    if blocked_reason and not tests_ran:
        return BLOCKED, f"The test command could not run: {blocked_reason}."
    if run.returncode == MISSING_EXECUTABLE_EXIT and not execution.measured:
        return BLOCKED, f"The test command `{command}` could not start."
    summary = f"The test command `{command}` exited with code {run.returncode}."
    if blocked_reason:
        summary += (
            f" The output may also point to a missing prerequisite: {blocked_reason}."
        )
    return FAIL, summary


def _unmeasured_summary(execution: TestExecution) -> str:
    summary = (
        "The test command exited successfully, but the test runner output had "
        "no recognizable summary."
    )
    if execution.parser == "go-packages":
        summary += " Plain `go test` output lists packages, not tests."
    return summary


def _judged_items(
    run: CommandRun,
    execution: TestExecution,
    problem: tuple[Status, str] | None,
    section: Mapping[str, Any],
    previous_executed: int | None,
    allow_test_removal: bool,
) -> list[GateItem]:
    """Threshold findings, only for counts that describe a complete run: a
    blocked or timed-out run's partial counts would mislead."""
    blocked = problem is not None and problem[0] == BLOCKED
    if not execution.measured or blocked or run.timed_out:
        return []
    return threshold_items(execution, section, previous_executed, allow_test_removal)


def _verdict(
    problem: tuple[Status, str] | None,
    items: Sequence[GateItem],
    execution: TestExecution,
) -> tuple[Status, str]:
    """The run's own problem wins; otherwise broken thresholds fail it."""
    if problem is not None:
        return problem
    if not items:
        return PASS, f"{_counts_text(execution)}."
    broken = "; ".join(
        f"{item.metric} {item.value} (limit {item.limit})" for item in items
    )
    return FAIL, f"The test run broke {plural(len(items), 'threshold')}: {broken}."


def _details(
    run: CommandRun,
    execution: TestExecution,
    items: Sequence[GateItem],
    status: Status,
) -> list[str]:
    """Counts first, then findings, then the output tail when not passing."""
    details = [_counts_text(execution)] if execution.measured else []
    details += [item.describe() for item in items]
    if status != PASS and run.output.strip():
        details.append("Last lines of test output:\n" + output_tail(run.output))
    return details


def evaluate_tests(
    run: CommandRun,
    execution: TestExecution,
    thresholds: Mapping[str, Any],
    *,
    previous_executed: int | None = None,
    allow_test_removal: bool = False,
    blocked_reason: str | None = None,
) -> GateOutcome:
    """Judge one finished test run.

    ``previous_executed`` is the executed count of the last full run; a drop
    fails unless the user approved removing tests. ``blocked_reason`` names a
    missing prerequisite the caller recognized in the output (for example
    missing dependencies); a failed run then blocks instead of failing, but
    only when no test ran: measured test results always fail.
    """
    problem = _run_problem(run, execution, blocked_reason)
    if problem is None and not execution.measured:
        problem = BLOCKED, _unmeasured_summary(execution)
    items = _judged_items(
        run,
        execution,
        problem,
        _section(thresholds),
        previous_executed,
        allow_test_removal,
    )
    status, summary = _verdict(problem, items, execution)
    details = _details(run, execution, items, status)
    measurements = _measurements(run, execution, previous_executed)
    return GateOutcome(
        KEY,
        TITLE,
        status,
        summary,
        details,
        items,
        measurements,
    )


def junit_patterns(config: Mapping[str, Any]) -> list[str]:
    """Configured ``test.junit`` globs (a string or a list) from the gate
    config's ``test`` section."""
    value = config.get("junit")
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(pattern) for pattern in value]
    return []
