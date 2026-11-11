"""Test-runner summary parsers for PHP, JVM, .NET, Swift, Ruby, Elixir, Dart,
and C/C++ (CTest, Meson). The JavaScript, Python, Rust, and TAP parsers live
in ``gate_tests_parsers``; both share its counting helpers."""

from __future__ import annotations

import re
from collections import Counter

from gate_tests_parsers import (
    TestExecution,
    column_parser,
    column_sums,
    measured_execution,
    parts_execution,
    summed_rows,
    word_counts,
)

_PHPUNIT_OK = re.compile(r"^OK \((\d+) tests?, \d+ assertions?\)", re.MULTILINE)
_PHPUNIT_TOTALS = re.compile(r"^Tests: (\d+), Assertions: \d+(.*)$", re.MULTILINE)
_PHPUNIT_PAIR = re.compile(r"([A-Za-z]+): (\d+)")


def parse_phpunit(text: str) -> TestExecution | None:
    """PHPUnit: ``OK (5 tests, 9 assertions)`` or ``Tests: 5, Assertions: 9,
    Failures: 1, Skipped: 1.``"""
    executed = sum(int(amount) for amount in _PHPUNIT_OK.findall(text))
    counts: Counter[str] = Counter()
    found = executed > 0
    for match in _PHPUNIT_TOTALS.finditer(text):
        found = True
        executed += int(match.group(1))
        for name, amount in _PHPUNIT_PAIR.findall(match.group(2)):
            counts[name.lower()] += int(amount)
    if not found:
        return None
    return measured_execution(
        "phpunit",
        executed=executed,
        failed=counts["failures"] + counts["errors"],
        skipped=counts["skipped"] + counts["incomplete"],
    )


_SUREFIRE = re.compile(
    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(.*)$",
    re.MULTILINE,
)


def parse_maven(text: str) -> TestExecution | None:
    """Maven Surefire/Failsafe: the ``Results:`` aggregate of each module.

    Per-class lines carry ``Time elapsed``; aggregates do not. Summing the
    aggregates counts every module of a multi-module build once. Output with
    only per-class lines (an aborted build) falls back to summing those.
    """
    rows = _SUREFIRE.findall(text)
    if not rows:
        return None
    aggregates = [row for row in rows if "Time elapsed" not in row[4]]
    chosen = [row[:4] for row in aggregates or rows]
    executed, failures, errors, skipped = column_sums(chosen)
    return measured_execution(
        "maven", executed=executed, failed=failures + errors, skipped=skipped
    )


_DOTNET = re.compile(
    r"Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+),\s*Total:\s*(\d+)"
)


def _dotnet_fields(sums: list[int]) -> dict[str, int]:
    failed, passed, skipped, total = sums
    return {"executed": total, "failed": failed, "passed": passed, "skipped": skipped}


# ``dotnet test``: one ``Failed: N, Passed: N, Skipped: N, Total: N`` line
# per test project, summed.
parse_dotnet = column_parser("dotnet", _DOTNET, _dotnet_fields)


_XCTEST = re.compile(
    r"Executed (\d+) tests?, with (?:(\d+) tests? skipped and )?(\d+) failures?"
)
_SWIFT_TESTING = re.compile(
    r"Test run with (\d+) tests?(?: in \d+ suites?)? (passed|failed) after "
    r"[\d.]+ seconds?(?: with (\d+) issues?)?"
)


def _xctest_counts(text: str) -> tuple[int, int, int] | None:
    """The last ``Executed`` line is the outermost suite, the whole run."""
    rows = _XCTEST.findall(text)
    if not rows:
        return None
    executed, skipped, failed = rows[-1]
    return int(executed), int(failed), int(skipped or 0)


def _swift_testing_counts(text: str) -> tuple[int, int, int] | None:
    """swift-testing reports issues, not failed tests; a failed run counts
    its issues (at least one, at most every test) as failures."""
    rows = _SWIFT_TESTING.findall(text)
    if not rows:
        return None
    total, verdict, issues = rows[-1]
    executed = int(total)
    failed = 0 if verdict == "passed" else min(executed, max(1, int(issues or 0)))
    return executed, failed, 0


def parse_swift(text: str) -> TestExecution | None:
    """``swift test``: XCTest and swift-testing summaries, added together."""
    parts = [
        counts
        for counts in (_xctest_counts(text), _swift_testing_counts(text))
        if counts is not None
    ]
    if not parts:
        return None
    return measured_execution(
        "swift",
        executed=sum(part[0] for part in parts),
        failed=sum(part[1] for part in parts),
        skipped=sum(part[2] for part in parts),
    )


_RSPEC = re.compile(
    r"^(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?"
    r"(?:, (\d+) errors? occurred outside of examples)?",
    re.MULTILINE,
)


def _rspec_fields(sums: list[int]) -> dict[str, int]:
    examples, failures, pending, outside = sums
    return {"executed": examples, "failed": failures + outside, "skipped": pending}


# RSpec: ``12 examples, 1 failure, 2 pending``.
parse_rspec = column_parser("rspec", _RSPEC, _rspec_fields)


# minitest says ``runs`` (``tests`` with minitest-reporters) and ``skips``;
# test-unit says ``tests`` and reports ``pendings`` and ``omissions``.
_RUBY_XUNIT = re.compile(
    r"^(\d+) (?:runs?|tests?), \d+ assertions?, (\d+) failures?, (\d+) errors?"
    r"(?:, (\d+) skips?)?(?:, (\d+) pendings?, (\d+) omissions?)?",
    re.MULTILINE,
)


def parse_ruby_xunit(text: str) -> TestExecution | None:
    """Minitest ``3 runs, 5 assertions, 0 failures, 0 errors, 1 skips`` and
    test-unit ``3 tests, 1 assertions, 0 failures, 0 errors, 1 pendings,
    1 omissions``, summed over every test task. Pending and omitted tests
    prove nothing, so they count as skipped."""
    rows = _RUBY_XUNIT.findall(text)
    if not rows:
        return None
    executed, failures, errors, skips, pendings, omissions = column_sums(rows)
    test_unit = any(row[4] for row in rows)
    return measured_execution(
        "test-unit" if test_unit else "minitest",
        executed=executed,
        failed=failures + errors,
        skipped=skips + pendings + omissions,
    )


_EXUNIT = re.compile(
    r"^\d+ (?:doctests?|propert(?:y|ies)|tests?),.*\b\d+ failures?\b.*$",
    re.MULTILINE,
)
_EXUNIT_PAIR = re.compile(r"(\d+) ([a-z]+)")
_EXUNIT_KINDS = {
    "doctest": "total",
    "doctests": "total",
    "property": "total",
    "properties": "total",
    "test": "total",
    "tests": "total",
    "failure": "failed",
    "failures": "failed",
    "invalid": "failed",
    "skipped": "skipped",
    "excluded": "excluded",
}


def _exunit_line_counts(line: str) -> Counter[str] | None:
    """Counts by kind; None when the line names a count ExUnit never prints
    (another runner's summary, such as test-unit's ``1 omissions``)."""
    counts: Counter[str] = Counter()
    for amount, word in _EXUNIT_PAIR.findall(line):
        kind = _EXUNIT_KINDS.get(word)
        if kind is None:
            return None
        counts[kind] += int(amount)
    return counts


def parse_exunit(text: str) -> TestExecution | None:
    """Elixir ExUnit: ``1 doctest, 5 tests, 1 failure, 1 skipped, 2 excluded``.

    The total includes excluded tests, which never ran, so they move from
    ``executed`` to ``deselected``.
    """
    counts: Counter[str] = Counter()
    for line in _EXUNIT.findall(text):
        line_counts = _exunit_line_counts(line)
        if line_counts is None:
            return None
        counts.update(line_counts)
    if not counts:
        return None
    return measured_execution(
        "exunit",
        executed=max(0, counts["total"] - counts["excluded"]),
        failed=counts["failed"],
        skipped=counts["skipped"],
        deselected=counts["excluded"],
    )


_DART = re.compile(
    r"\+(\d+)(?: ~(\d+))?(?: -(\d+))?: "
    r"(?:All tests passed!|Some tests failed\.|All tests skipped\.|No tests ran\.)"
)


def parse_dart(text: str) -> TestExecution | None:
    """``dart test`` / ``flutter test``: the last ``+passed ~skipped -failed``
    progress line, which ends with the run's verdict."""
    rows = _DART.findall(text)
    if not rows:
        return None
    passed, skipped, failed = (int(value or 0) for value in rows[-1])
    return parts_execution("dart", passed=passed, failed=failed, skipped=skipped)


# CTest: ``75% tests passed, 1 tests failed out of 4``; CMake 4 leaves out
# ``, 0 tests failed`` when nothing failed. The total counts skipped tests as
# passed and leaves disabled ones out; both are listed under ``The following
# tests did not run:``. Tests that could not start count as failed.
_CTEST = re.compile(
    r"^\d+% tests passed(?:, (\d+) tests? failed)? out of (\d+)", re.MULTILINE
)
_CTEST_NOT_RUN = re.compile(
    r"^The following tests did not run:\n((?:[ \t]+\d+ - .*\n?)+)", re.MULTILINE
)


def parse_ctest(text: str) -> TestExecution | None:
    """CTest's summary line plus its list of skipped and disabled tests."""
    sums = summed_rows(_CTEST, text)
    if sums is None:
        return None
    failed, total = sums
    not_run = "".join(_CTEST_NOT_RUN.findall(text)).splitlines()
    disabled = sum(1 for line in not_run if line.rstrip().endswith("(Disabled)"))
    return measured_execution(
        "ctest", executed=total + disabled, failed=failed, skipped=len(not_run)
    )


# ``meson test``: ``Ok:``, ``Expected Fail:``, ``Fail:``, ``Unexpected Pass:``,
# ``Skipped:`` and ``Timeout:`` counts, one per line or all on one line; Meson
# 1.x prints only the kinds that occurred besides ``Ok`` and ``Fail``.
_MESON_KINDS = "Expected Fail|Fail|Unexpected Pass|Skipped|Timeout"
_MESON_BLOCK = re.compile(rf"^Ok:[ \t]+\d+.*(?:\n(?:{_MESON_KINDS}):.*)*", re.MULTILINE)
_MESON_PAIR = re.compile(rf"(Ok|{_MESON_KINDS}):\s+(\d+)")


def parse_meson(text: str) -> TestExecution | None:
    """Meson's summary block. Unexpected passes and timeouts fail; expected
    failures prove nothing, so they count as skipped."""
    blocks = _MESON_BLOCK.findall(text)
    if not blocks:
        return None
    counts = word_counts(_MESON_PAIR, "\n".join(blocks), word_first=True)
    failed = counts["Fail"] + counts["Unexpected Pass"] + counts["Timeout"]
    skipped = counts["Skipped"] + counts["Expected Fail"]
    return measured_execution(
        "meson",
        executed=counts["Ok"] + failed + skipped,
        passed=counts["Ok"],
        failed=failed,
        skipped=skipped,
        xfailed=counts["Expected Fail"],
    )
