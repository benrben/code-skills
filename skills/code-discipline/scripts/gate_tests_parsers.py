"""Read test counts from the summaries that test runners print.

Each parser takes output already passed through ``clean_output`` and
recognizes one runner's explicit summary line and nothing else, returning None
otherwise, so output that merely looks successful never counts as tests having
run. ``gate_tests.parse_test_execution`` runs them all and adds up what each
runner reports; the Go parsers live there.

Counting conventions shared by every parser:

* ``executed`` is every test the runner started, including skipped ones.
* ``skipped`` includes expected failures (xfail) and todo tests: neither
  proves the behavior works, so the tests gate treats them like skips.
* ``deselected`` counts tests a filter removed before the run (pytest
  ``deselected``, cargo ``filtered out``, ExUnit ``excluded``).
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Callable

UNKNOWN_PARSER = "unknown"


@dataclasses.dataclass(frozen=True)
class TestExecution:
    """How many tests a run executed and how they ended.

    ``measured`` is False when the output had no summary this module trusts;
    the counts are then zero or, for plain ``go test``, package counts.
    """

    __test__ = False  # not a test class, despite the name

    executed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    retried: int = 0
    parser: str = ""
    measured: bool = False
    deselected: int = 0


def measured_execution(
    parser: str,
    *,
    executed: int,
    failed: int = 0,
    skipped: int = 0,
    passed: int | None = None,
    xfailed: int = 0,
    retried: int = 0,
    deselected: int = 0,
) -> TestExecution:
    """A measured execution; ``passed`` defaults to what is left after
    failures and skips."""
    if passed is None:
        passed = max(0, executed - failed - skipped)
    return TestExecution(
        executed=executed,
        passed=passed,
        failed=failed,
        skipped=skipped,
        xfailed=xfailed,
        retried=retried,
        parser=parser,
        measured=True,
        deselected=deselected,
    )


_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


def clean_output(output: str) -> str:
    """Drop terminal colors and cursor codes and turn carriage returns into
    newlines, so summaries match line-anchored patterns."""
    text = _ANSI_ESCAPE.sub("", output)
    return text.replace("\r\n", "\n").replace("\r", "\n")


_COUNT_WORDS = re.compile(
    r"(?<![\w.])(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?|ignored"
    r"|retried|rerun|todo|deselected|total)\b",
    re.IGNORECASE,
)


def summary_counts(text: str) -> Counter[str]:
    """``"2 failed, 10 passed"`` -> ``{"failed": 2, "passed": 10}``."""
    counts: Counter[str] = Counter()
    for amount, name in _COUNT_WORDS.findall(text):
        key = name.lower()
        counts["errors" if key == "error" else key] += int(amount)
    return counts


def column_sums(rows: list[tuple[str, ...]]) -> list[int]:
    """Add up each regex group over every match; an absent optional group
    counts as 0."""
    return [
        sum(int(value or 0) for value in column) for column in zip(*rows, strict=True)
    ]


def summed_rows(pattern: re.Pattern[str], text: str) -> list[int] | None:
    """Column sums over every summary line ``pattern`` finds, or None when the
    output has no such line."""
    rows = pattern.findall(text)
    return column_sums(rows) if rows else None


def parts_execution(
    parser: str, *, passed: int, failed: int, skipped: int
) -> TestExecution:
    """An execution from separate passed, failed, and skipped counts."""
    return measured_execution(
        parser,
        executed=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
    )


Parser = Callable[[str], TestExecution | None]
Fields = Callable[[list[int]], dict[str, int]]


def column_parser(parser: str, pattern: re.Pattern[str], fields: Fields) -> Parser:
    """A parser for summaries with fixed numeric columns: every matching line
    is summed, then ``fields`` names the columns."""

    def parse(text: str) -> TestExecution | None:
        sums = summed_rows(pattern, text)
        return None if sums is None else measured_execution(parser, **fields(sums))

    return parse


def word_counts(
    pattern: re.Pattern[str], text: str, *, word_first: bool = False
) -> Counter[str]:
    """Totals per word from ``(amount, word)`` matches, summed over the output."""
    counts: Counter[str] = Counter()
    for first, second in pattern.findall(text):
        word, amount = (first, second) if word_first else (second, first)
        counts[word] += int(amount)
    return counts


_CARGO = re.compile(
    r"test result:\s+\w+\.\s+(\d+) passed;\s+(\d+) failed;\s+(\d+) ignored;"
    r"(?:\s+\d+ measured;)?(?:\s+(\d+) filtered out;)?",
    re.IGNORECASE,
)


def _cargo_fields(sums: list[int]) -> dict[str, int]:
    passed, failed, ignored, filtered = sums
    return {
        "executed": passed + failed + ignored,
        "passed": passed,
        "failed": failed,
        "skipped": ignored,
        "deselected": filtered,
    }


# ``cargo test``: one ``test result`` line per test binary, summed.
parse_cargo = column_parser("cargo", _CARGO, _cargo_fields)


_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_STATUS = re.compile(
    r"^(?:OK|FAILED|PASSED|NO TESTS RAN)\b(?:\s*\((.*)\))?", re.MULTILINE
)
# unittest's status keys and Twisted trial's, by what they mean here.
_UNITTEST_KEYS = {
    "failures": "failed",
    "errors": "failed",
    "unexpected successes": "failed",
    "unexpectedSuccesses": "failed",
    "skipped": "skipped",
    "skips": "skipped",
    "expected failures": "xfailed",
    "expectedFailures": "xfailed",
    "successes": "passed",
}


def _unittest_status(segment: str) -> Counter[str] | None:
    """``FAILED (failures=1, expected failures=2)`` -> counts by meaning.

    None when the status line is missing or names a count this parser does
    not know, so a run it cannot fully read is never reported as clean.
    """
    status = _UNITTEST_STATUS.search(segment)
    if status is None:
        return None
    counts: Counter[str] = Counter()
    for part in (status.group(1) or "").split(","):
        name, _, amount = part.strip().partition("=")
        kind = _UNITTEST_KEYS.get(name.strip())
        if not amount.isdigit():
            continue
        if kind is None:
            return None
        counts[kind] += int(amount)
    return counts


def parse_unittest(text: str) -> TestExecution | None:
    """``unittest`` and Twisted trial: every ``Ran N tests`` block with its
    OK/FAILED/PASSED line."""
    runs = list(_UNITTEST_RAN.finditer(text))
    if not runs:
        return None
    executed = 0
    counts: Counter[str] = Counter()
    for index, run in enumerate(runs):
        end = runs[index + 1].start() if index + 1 < len(runs) else len(text)
        status = _unittest_status(text[run.end() : end])
        if status is None:
            return None
        executed += int(run.group(1))
        counts.update(status)
    xfailed = counts["xfailed"]
    return measured_execution(
        "unittest",
        executed=executed,
        failed=counts["failed"],
        skipped=counts["skipped"] + xfailed,
        xfailed=xfailed,
    )


_VITEST = re.compile(r"^\s*Tests\s{2,}(.+?)\s*\((\d+)\)\s*$", re.MULTILINE)


def parse_vitest(text: str) -> TestExecution | None:
    """Vitest: ``Tests  2 failed | 10 passed (12)``, summed over workspaces."""
    executed = 0
    counts: Counter[str] = Counter()
    for match in _VITEST.finditer(text):
        found = summary_counts(match.group(1))
        if found:
            executed += int(match.group(2))
            counts.update(found)
    if not counts:
        return None
    return measured_execution(
        "vitest",
        executed=executed,
        passed=counts["passed"],
        failed=counts["failed"] + counts["errors"],
        skipped=counts["skipped"] + counts["todo"],
        retried=counts["retried"],
    )


_JEST = re.compile(r"^\s*Tests:\s+(.+)$", re.MULTILINE)


def parse_jest(text: str) -> TestExecution | None:
    """Jest: ``Tests: 1 failed, 4 passed, 5 total``, summed over workspaces."""
    counts: Counter[str] = Counter()
    for match in _JEST.finditer(text):
        found = summary_counts(match.group(1))
        if found["total"]:
            counts.update(found)
    if not counts:
        return None
    return measured_execution(
        "jest",
        executed=counts["total"],
        passed=counts.get("passed"),
        failed=counts["failed"],
        skipped=counts["skipped"] + counts["todo"],
    )


# Items may have two words (``5 subtests passed``); pytest < 6 printed
# ``in 0.01 seconds``.
_PYTEST = re.compile(
    r"^=*\s*((?:\d+ [a-z]+(?: [a-z]+)?(?:, )?)+|no tests ran)"
    r" in \d+(?:\.\d+)?(?:s| seconds)\b",
)
_PYTEST_SUBTESTS = re.compile(r"(\d+) subtests (failed|skipped|xfailed)\b")


def _pytest_counts(summaries: list[str]) -> Counter[str]:
    """Outcome counts over every summary; subtest outcomes are kept apart
    as ``subtest failed`` and so on."""
    counts: Counter[str] = Counter()
    for summary in summaries:
        counts.update(summary_counts(summary))
        for amount, outcome in _PYTEST_SUBTESTS.findall(summary):
            counts[f"subtest {outcome}"] += int(amount)
    return counts


def parse_pytest(text: str) -> TestExecution | None:
    """pytest: every ``12 passed, 1 skipped in 0.5s`` line, summed. pytest
    prints one per session, so tox environments or chained runs add up.

    Subtests (pytest 9, pytest-subtests) are not tests of their own, so they
    never add to ``executed``; failed ones still count as failures and
    skipped or xfailed ones as skips.
    """
    summaries = [
        match.group(1)
        for line in text.splitlines()
        if (match := _PYTEST.match(line.strip()))
    ]
    if not summaries:
        return None
    counts = _pytest_counts(summaries)
    xfailed = counts["xfailed"]
    failed = counts["failed"] + counts["errors"]
    skipped = counts["skipped"] + xfailed
    passed = counts["passed"]
    return measured_execution(
        "pytest",
        executed=passed + failed + skipped + counts["xpassed"],
        passed=passed,
        failed=failed + counts["subtest failed"],
        skipped=skipped + counts["subtest skipped"] + counts["subtest xfailed"],
        xfailed=xfailed,
        retried=counts["rerun"],
        deselected=counts["deselected"],
    )


_MOCHA = re.compile(r"^\s*(\d+) (passing|pending|failing)\b", re.MULTILINE)


def parse_mocha(text: str) -> TestExecution | None:
    """Mocha: ``12 passing``, ``2 pending``, ``1 failing``, summed."""
    counts = word_counts(_MOCHA, text)
    if "passing" not in counts:
        return None
    return parts_execution(
        "mocha",
        passed=counts["passing"],
        failed=counts["failing"],
        skipped=counts["pending"],
    )


_TAP_SUMMARY = re.compile(
    r"^(?:#|ℹ)\s*(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$",
    re.MULTILINE,
)


def parse_tap_summary(text: str) -> TestExecution | None:
    """node:test (TAP or spec reporter) and tape: ``# tests 5`` / ``# pass 4``
    footers, summed over runs."""
    counts = word_counts(_TAP_SUMMARY, text, word_first=True)
    if "tests" not in counts:
        return None
    return measured_execution(
        "tap",
        executed=counts["tests"],
        passed=counts.get("pass"),
        failed=counts["fail"] + counts["cancelled"],
        skipped=counts["skipped"] + counts["todo"],
    )


_TAP_PLAN = re.compile(r"^1\.\.\d+", re.MULTILINE)
_TAP_RESULT = re.compile(r"^(not )?ok\b(.*)$", re.MULTILINE)
_TAP_DIRECTIVE = re.compile(r"#\s*(skip|todo)\b", re.IGNORECASE)


def parse_tap_lines(text: str) -> TestExecution | None:
    """Plain TAP without a footer: top-level ``ok``/``not ok`` lines under a
    ``1..N`` plan. ``# SKIP`` and ``# TODO`` results count as skipped."""
    if _TAP_PLAN.search(text) is None:
        return None
    results = _TAP_RESULT.findall(text)
    if not results:
        return None
    skipped = sum(1 for _, rest in results if _TAP_DIRECTIVE.search(rest))
    failed = sum(
        1 for negated, rest in results if negated and not _TAP_DIRECTIVE.search(rest)
    )
    return measured_execution(
        "tap", executed=len(results), failed=failed, skipped=skipped
    )
