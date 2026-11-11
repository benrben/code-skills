"""Coverage gate: are the lines the change added executed by the tests?

The gate measures only executable lines the change added (every line in
whole-repository scope). Executable means the coverage report lists the
line. A production file the report does not mention was never loaded by the
tests; every added code line in it counts as uncovered, because nothing ran
it. That holds only for languages the test run measures: a JavaScript file
in a Python project is "not measured", never "uncovered", because no Python
test could cover it.

Readers live in ``gate_coverage_readers`` and the instrumented test commands
in ``gate_coverage_recipes``; this module only judges the data.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from diff_scope import ChangeSet
from gate_coverage_readers import CoverageData, FileCoverage
from gate_types import (
    BLOCKED,
    FAIL,
    PASS,
    FunctionInfo,
    GateItem,
    GateOutcome,
)
from source_files import normalize_path, read_text

KEY = "coverage"
TITLE = "Coverage"
MAX_ITEMS = 50

_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")


@dataclasses.dataclass(frozen=True)
class CoverageLimits:
    line: float = 90.0
    branch: float = 90.0

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, Any]) -> CoverageLimits:
        """Accepts the ``coverage`` section or the whole thresholds file."""
        section = thresholds.get("coverage", thresholds)
        if not isinstance(section, Mapping):
            section = {}
        return cls(
            line=float(section.get("line_coverage_limit", cls.line)),
            branch=float(section.get("branch_coverage_limit", cls.branch)),
        )


@dataclasses.dataclass
class FileScope:
    """The in-scope executable lines and branches of one production file."""

    path: str
    covered: set[int] = dataclasses.field(default_factory=set)
    uncovered: set[int] = dataclasses.field(default_factory=set)
    branches: dict[int, tuple[int, int]] = dataclasses.field(default_factory=dict)
    never_loaded: bool = False

    @property
    def partial_branches(self) -> dict[int, tuple[int, int]]:
        return {
            line: counts
            for line, counts in self.branches.items()
            if counts[0] < counts[1]
        }


@dataclasses.dataclass(frozen=True)
class Totals:
    covered_lines: int
    total_lines: int
    covered_branches: int
    total_branches: int

    @property
    def line_percent(self) -> float:
        return _percent(self.covered_lines, self.total_lines)

    @property
    def branch_percent(self) -> float | None:
        if not self.total_branches:
            return None
        return _percent(self.covered_branches, self.total_branches)


def _percent(covered: int, total: int) -> float:
    return 100.0 * covered / total if total else 100.0


def _is_code(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith(_COMMENT_PREFIXES)


def added_code_lines(
    relative: str, lines: Sequence[str], changes: ChangeSet
) -> set[int]:
    """Added lines that hold code rather than blanks or comments."""
    added = changes.added_lines(relative, len(lines))
    return {line for line in added if line <= len(lines) and _is_code(lines[line - 1])}


def file_scope(
    relative: str,
    lines: Sequence[str],
    changes: ChangeSet,
    coverage: FileCoverage | None,
) -> FileScope:
    """Added executable lines of one file split into covered and uncovered.

    Without report data the file was never loaded: every added code line
    stands in for its executable lines, all uncovered.
    """
    added = changes.added_lines(relative, len(lines))
    scope = FileScope(relative)
    if coverage is None:
        scope.never_loaded = True
        scope.uncovered = added_code_lines(relative, lines, changes)
        return scope
    for line in added & coverage.lines.keys():
        (scope.covered if coverage.lines[line] > 0 else scope.uncovered).add(line)
    scope.branches = {
        line: coverage.branches[line] for line in added & coverage.branches.keys()
    }
    return scope


def _totals(scopes: Iterable[FileScope]) -> Totals:
    covered = total = branches_covered = branches_total = 0
    for scope in scopes:
        covered += len(scope.covered)
        total += len(scope.covered) + len(scope.uncovered)
        for hit, count in scope.branches.values():
            branches_covered += hit
            branches_total += count
    return Totals(covered, total, branches_covered, branches_total)


def compact_ranges(lines: Iterable[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``"3-5, 9"``."""
    ordered = sorted(set(lines))
    parts: list[str] = []
    start = previous = ordered[0] if ordered else 0
    for line in ordered[1:] + [0]:
        if line == previous + 1:
            previous = line
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = line
    return ", ".join(parts) if ordered else ""


def _innermost(functions: Sequence[FunctionInfo], line: int) -> FunctionInfo | None:
    containing = [function for function in functions if function.contains(line)]
    if not containing:
        return None
    return min(containing, key=lambda function: function.end_line - function.start_line)


def _group_by_function(
    lines: Iterable[int], functions: Sequence[FunctionInfo]
) -> list[tuple[str, list[int]]]:
    groups: dict[str, list[int]] = {}
    for line in sorted(lines):
        function = _innermost(functions, line)
        groups.setdefault(function.name if function else "", []).append(line)
    return sorted(groups.items(), key=lambda group: group[1][0])


def _line_items(
    scope: FileScope, functions: Sequence[FunctionInfo], limit: float
) -> list[GateItem]:
    return [
        GateItem(
            path=scope.path,
            line=lines[0],
            name=name,
            metric="uncovered lines",
            value=compact_ranges(lines),
            limit=f"{limit:g}% line coverage",
        )
        for name, lines in _group_by_function(scope.uncovered, functions)
    ]


def _branch_items(
    scope: FileScope, functions: Sequence[FunctionInfo], limit: float
) -> list[GateItem]:
    partial = scope.partial_branches
    items: list[GateItem] = []
    for name, lines in _group_by_function(partial, functions):
        outcomes = ", ".join(
            f"line {line} ({partial[line][0]}/{partial[line][1]})" for line in lines
        )
        items.append(
            GateItem(
                path=scope.path,
                line=lines[0],
                name=name,
                metric="branch outcomes taken",
                value=outcomes,
                limit=f"{limit:g}% branch coverage",
            )
        )
    return items


def _scope_label(changes: ChangeSet) -> str:
    return "changed lines" if changes.incremental else "all lines"


def blocked_coverage(summary: str, details: Sequence[str] = ()) -> GateOutcome:
    """Coverage could not be measured: never a pass."""
    return GateOutcome(
        KEY,
        TITLE,
        BLOCKED,
        summary,
        list(details),
    )


def _functions_by_path(
    functions: Sequence[FunctionInfo],
) -> dict[str, list[FunctionInfo]]:
    grouped: dict[str, list[FunctionInfo]] = {}
    for function in functions:
        grouped.setdefault(function.path, []).append(function)
    return grouped


def is_measured(path: str, measures: Collection[str] | None) -> bool:
    """Whether the test run measures files like ``path`` (None: every file)."""
    return measures is None or Path(path).suffix.lower() in measures


@dataclasses.dataclass
class Collected:
    """In-scope files with measured added lines, and the paths of in-scope
    files with added code in a language this test run does not measure."""

    scopes: list[FileScope] = dataclasses.field(default_factory=list)
    not_measured: list[str] = dataclasses.field(default_factory=list)


def collect_scopes(
    root: Path,
    files: Sequence[Path],
    changes: ChangeSet,
    data: CoverageData,
    measures: Collection[str] | None = None,
) -> Collected:
    """The scope of every in-scope production file that has added lines.

    A file the report lists is always measured; one it does not list is
    never loaded only when the test run measures its language."""
    collected = Collected()
    for path in files:
        relative = normalize_path(path, root)
        if not changes.includes(relative):
            continue
        lines = read_text(path).splitlines()
        coverage = data.get(relative)
        if coverage is None and not is_measured(relative, measures):
            if added_code_lines(relative, lines, changes):
                collected.not_measured.append(relative)
            continue
        scope = file_scope(relative, lines, changes, coverage)
        if scope.covered or scope.uncovered or scope.branches:
            collected.scopes.append(scope)
    return collected


def _measurements(
    changes: ChangeSet,
    totals: Totals,
    collected: Collected,
    limits: CoverageLimits,
) -> dict[str, Any]:
    scopes = collected.scopes
    return {
        "scope": _scope_label(changes),
        "line_coverage_percent": round(totals.line_percent, 2),
        "covered_lines": totals.covered_lines,
        "total_lines": totals.total_lines,
        "branch_coverage_percent": (
            None if totals.branch_percent is None else round(totals.branch_percent, 2)
        ),
        "covered_branches": totals.covered_branches,
        "total_branches": totals.total_branches,
        "files": len(scopes),
        "never_loaded": sorted(scope.path for scope in scopes if scope.never_loaded),
        "not_measured": sorted(collected.not_measured),
        "line_coverage_limit": limits.line,
        "branch_coverage_limit": limits.branch,
    }


def _summary(label: str, totals: Totals) -> str:
    line = (
        f"{label.capitalize()}: {totals.line_percent:.1f}% line coverage "
        f"({totals.covered_lines}/{totals.total_lines})"
    )
    branch = totals.branch_percent
    if branch is None:
        return line + "; branch coverage not reported."
    return (
        f"{line}, {branch:.1f}% branch coverage "
        f"({totals.covered_branches}/{totals.total_branches})."
    )


def _failures(totals: Totals, limits: CoverageLimits) -> tuple[bool, bool]:
    line_failed = totals.line_percent < limits.line
    branch = totals.branch_percent
    return line_failed, branch is not None and branch < limits.branch


def _failure_items(
    scopes: Sequence[FileScope],
    functions: Sequence[FunctionInfo],
    limits: CoverageLimits,
    failed: tuple[bool, bool],
) -> list[GateItem]:
    by_path = _functions_by_path(functions)
    items: list[GateItem] = []
    for scope in scopes:
        own = by_path.get(scope.path, [])
        if failed[0]:
            items.extend(_line_items(scope, own, limits.line))
        if failed[1]:
            items.extend(_branch_items(scope, own, limits.branch))
    return items


def _not_measured_detail(path: str) -> str:
    suffix = Path(path).suffix or "these"
    return f"{path}: not measured (this test run cannot measure {suffix} files)."


def _details(
    totals: Totals, collected: Collected, items: Sequence[GateItem]
) -> list[str]:
    details = [item.describe() for item in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        details.append(f"... and {len(items) - MAX_ITEMS} more not listed.")
    for scope in collected.scopes:
        if scope.never_loaded:
            details.append(f"{scope.path}: never loaded by tests.")
    details.extend(_not_measured_detail(path) for path in collected.not_measured)
    if totals.branch_percent is None:
        details.append("Branch coverage not reported by this runtime for these lines.")
    return details


def _missing_report(problem: str, prerequisite: str) -> GateOutcome:
    reason = problem or "the test run did not write a fresh coverage report."
    summary = f"Coverage report missing or stale: {reason}"
    if prerequisite:
        summary += f" This coverage recipe needs {prerequisite}."
    return blocked_coverage(summary)


def _only_unmeasured(collected: Collected) -> GateOutcome:
    """The change added code only in languages the test run cannot measure."""
    paths = ", ".join(collected.not_measured[:5])
    more = len(collected.not_measured) - 5
    if more > 0:
        paths += f" and {more} more"
    return blocked_coverage(
        f"The changed code is in files this test run cannot measure: {paths}.",
        [_not_measured_detail(path) for path in collected.not_measured],
    )


def evaluate_coverage(
    root: Path,
    files: Sequence[Path],
    changes: ChangeSet,
    data: CoverageData | None,
    functions: Sequence[FunctionInfo],
    thresholds: Mapping[str, Any],
    *,
    report_fresh: bool,
    problem: str = "",
    prerequisite: str = "",
    measures: Collection[str] | None = None,
) -> GateOutcome:
    """Judge line and branch coverage of the in-scope lines.

    ``files`` are the production files; ``functions`` are lizard's function
    ranges for them (used to name uncovered code). ``data`` is None when no
    report could be read; ``problem`` then says why. Pass the recipe's
    ``prerequisite`` so a missing report names what the project lacks, and
    its ``measures`` (file extensions) so files in other languages count as
    not measured instead of never loaded.
    """
    if data is None or not report_fresh:
        return _missing_report(problem, prerequisite)
    limits = CoverageLimits.from_thresholds(thresholds)
    collected = collect_scopes(root, files, changes, data, measures)
    totals = _totals(collected.scopes)
    measurements = _measurements(changes, totals, collected, limits)
    if not totals.total_lines and not totals.total_branches:
        if collected.not_measured:
            return _only_unmeasured(collected)
        return GateOutcome(
            KEY, TITLE, PASS, "No changed executable lines.", measurements=measurements
        )
    failed = _failures(totals, limits)
    items = _failure_items(collected.scopes, functions, limits, failed)
    status = FAIL if any(failed) else PASS
    summary = _summary(_scope_label(changes), totals)
    if status == FAIL:
        summary += f" Required: {limits.line:g}% lines, {limits.branch:g}% branches."
    return GateOutcome(
        KEY,
        TITLE,
        status,
        summary,
        _details(totals, collected, items),
        items[:MAX_ITEMS],
        measurements,
    )


def function_line_coverage(
    data: CoverageData,
    path: str,
    start: int,
    end: int,
    measures: Collection[str] | None = None,
) -> float | None:
    """Line coverage percent (0..100) of ``start..end`` in ``path``, for CRAP.

    A file the report never mentions was not loaded by tests: 0%, unless the
    test run cannot measure its language (``measures``). None then, and when
    the report has the file but no executable lines in the range.
    """
    coverage = data.get(path)
    if coverage is None:
        return 0.0 if is_measured(path, measures) else None
    hits = [count for line, count in coverage.lines.items() if start <= line <= end]
    if not hits:
        return None
    return _percent(sum(1 for count in hits if count > 0), len(hits))


def coverage_lookup(
    data: CoverageData, measures: Collection[str] | None = None
) -> Callable[[str, int, int], float | None]:
    """``function_line_coverage`` bound to one report, as gate_complexity wants."""

    def lookup(path: str, start: int, end: int) -> float | None:
        return function_line_coverage(data, path, start, end, measures)

    return lookup
