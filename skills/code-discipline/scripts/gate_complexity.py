"""Complexity gate: changed functions must stay simple enough to read and test.

Lizard (see ``gate_complexity_lizard``) measures every function the change
touches, in any language it can read: cyclomatic complexity, cognitive
complexity, and maximum nesting depth. When coverage is known, the gate also
computes CRAP (Change Risk Anti-Patterns), which weighs complexity by how much
of the function the tests leave unexecuted.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from diff_scope import ChangeSet
from gate_complexity_indent import INDENT_NESTING_SUFFIXES
from gate_complexity_lizard import AnalysisError
from gate_complexity_lizard import analyze_functions as analyze_functions
from gate_types import (
    BLOCKED,
    FAIL,
    PASS,
    FunctionInfo,
    GateItem,
    GateOutcome,
    Status,
    plural,
)

KEY = "complexity"
TITLE = "Complexity"
REPORT_ROWS = 20

# Coverage of the lines from ``start`` to ``end`` of ``path`` as a percentage
# (0..100), or None when the coverage report has no data for them.
CoverageLookup = Callable[[str, int, int], float | None]


def crap_score(cyclomatic: int, coverage_percent: float) -> float:
    """CRAP = c² · (1 − coverage)³ + c, with coverage clamped to 0..100%."""
    uncovered = max(0.0, min(1.0, 1.0 - coverage_percent / 100.0))
    return float(cyclomatic**2 * uncovered**3 + cyclomatic)


# --- Evaluation -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ComplexityLimits:
    """Upper limits; a function fails a metric when its value is greater."""

    cyclomatic: float = 10
    cognitive: float = 15
    nesting: float = 4
    crap: float = 30

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, Any]) -> ComplexityLimits:
        """Read limits from the thresholds document or its ``complexity`` section."""
        section = thresholds.get("complexity", thresholds)
        defaults = cls()
        return cls(
            cyclomatic=float(section.get("cyclomatic_limit", defaults.cyclomatic)),
            cognitive=float(section.get("cognitive_limit", defaults.cognitive)),
            nesting=float(section.get("nesting_limit", defaults.nesting)),
            crap=float(section.get("crap_limit", defaults.crap)),
        )


@dataclasses.dataclass(frozen=True)
class FunctionScore:
    """A function with its coverage and CRAP score (None when not measured)."""

    function: FunctionInfo
    coverage_percent: float | None
    crap: float | None

    @property
    def risk(self) -> float:
        return self.crap if self.crap is not None else float(self.function.cyclomatic)


def score_function(
    function: FunctionInfo, coverage_lookup: CoverageLookup | None
) -> FunctionScore:
    coverage = (
        coverage_lookup(function.path, function.start_line, function.end_line)
        if coverage_lookup is not None
        else None
    )
    crap = crap_score(function.cyclomatic, coverage) if coverage is not None else None
    return FunctionScore(function, coverage, crap)


def violations(score: FunctionScore, limits: ComplexityLimits) -> list[GateItem]:
    """One item per metric over its limit."""
    function = score.function
    measured: list[tuple[str, float | None, float]] = [
        ("cyclomatic", function.cyclomatic, limits.cyclomatic),
        ("cognitive", function.cognitive, limits.cognitive),
        ("nesting", function.nesting, limits.nesting),
        ("CRAP", score.crap, limits.crap),
    ]
    return [
        GateItem(
            path=function.path,
            line=function.start_line,
            name=function.name,
            metric=metric,
            value=_format_value(metric, value, score.coverage_percent),
            limit=f"{limit:g}",
        )
        for metric, value, limit in measured
        if value is not None and value > limit
    ]


def _format_value(metric: str, value: float, coverage: float | None) -> str:
    if metric != "CRAP" or coverage is None:
        return f"{value:g}"
    return f"{value:.2f} (line coverage {coverage:.0f}%)"


def evaluate_complexity(
    functions: Sequence[FunctionInfo],
    changes: ChangeSet,
    coverage_lookup: CoverageLookup | None,
    thresholds: Mapping[str, Any],
    *,
    unsupported: Sequence[str] = (),
    errors: Sequence[AnalysisError | str] = (),
) -> GateOutcome:
    """Judge the functions the change touches (all functions in repository
    scope). Fails on any metric over its limit; blocked when lizard failed on
    a file in the change, or failed to run, and nothing else failed.

    ``functions`` may cover more files than the change (dead_code reuses one
    list for every production file), so unsupported files and per-file errors
    outside the change are ignored too. A plain-string error has no path and
    always counts.
    """
    limits = ComplexityLimits.from_thresholds(thresholds)
    unsupported = [path for path in unsupported if changes.includes(path)]
    errors = [error for error in errors if _error_in_scope(error, changes)]
    scores = [
        score_function(function, coverage_lookup)
        for function in _functions_in_scope(functions, changes)
    ]
    items = [item for score in scores for item in violations(score, limits)]
    status: Status = FAIL if items else (BLOCKED if errors else PASS)
    return GateOutcome(
        KEY,
        TITLE,
        status,
        _summary(status, items, scores, limits, changes.incremental, errors),
        _details(items, scores, unsupported, errors),
        items,
        _measurements(scores, limits, len(functions), unsupported),
    )


def _functions_in_scope(
    functions: Sequence[FunctionInfo], changes: ChangeSet
) -> list[FunctionInfo]:
    return [
        function
        for function in functions
        if changes.touches(function.path, function.start_line, function.end_line)
    ]


def _error_in_scope(error: AnalysisError | str, changes: ChangeSet) -> bool:
    if isinstance(error, str) or error.path is None:
        return True
    return changes.includes(error.path)


def _summary(
    status: Status,
    items: Sequence[GateItem],
    scores: Sequence[FunctionScore],
    limits: ComplexityLimits,
    incremental: bool,
    errors: Sequence[AnalysisError | str],
) -> str:
    noun = "changed functions" if incremental else "functions"
    if status == FAIL:
        failing = len({(item.path, item.line, item.name) for item in items})
        return (
            f"{noun.capitalize()} over the complexity limits: {failing} of "
            f"{len(scores)}."
        )
    if status == BLOCKED:
        return f"Lizard could not analyze the code: {errors[0]}"
    if not scores:
        return f"No {noun} to measure."
    return (
        f"{noun.capitalize()} within limits: {len(scores)} of {len(scores)} "
        f"(cyclomatic <= "
        f"{limits.cyclomatic:g}, cognitive <= {limits.cognitive:g}, nesting <= "
        f"{limits.nesting:g}, CRAP <= {limits.crap:g})."
    )


def _details(
    items: Sequence[GateItem],
    scores: Sequence[FunctionScore],
    unsupported: Sequence[str],
    errors: Sequence[AnalysisError | str],
) -> list[str]:
    details = [item.describe() for item in items]
    missing = sum(1 for score in scores if score.crap is None)
    if missing:
        details.append(
            f"CRAP not computed for {plural(missing, 'function')}: no line coverage "
            "data."
        )
    if unsupported:
        details.append(
            "Not analyzed (lizard has no reader for these files): "
            + ", ".join(sorted(unsupported))
        )
    flat = _nesting_from_indentation(scores)
    if flat:
        details.append(
            "Nesting measured from indentation (lizard tracks no nesting in "
            "these languages): " + ", ".join(flat)
        )
    details.extend(f"Lizard error: {error}" for error in errors)
    return details


def _nesting_from_indentation(scores: Sequence[FunctionScore]) -> list[str]:
    paths = {score.function.path for score in scores}
    return sorted(
        path for path in paths if Path(path).suffix.lower() in INDENT_NESTING_SUFFIXES
    )


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _row(score: FunctionScore, limits: ComplexityLimits) -> dict[str, Any]:
    function = score.function
    return {
        "path": function.path,
        "name": function.name,
        "start_line": function.start_line,
        "end_line": function.end_line,
        "cyclomatic": function.cyclomatic,
        "cognitive": function.cognitive,
        "nesting": function.nesting,
        "parameters": function.parameters,
        "coverage_percent": score.coverage_percent,
        "crap": score.crap,
        "passed": not violations(score, limits),
    }


def _measurements(
    scores: Sequence[FunctionScore],
    limits: ComplexityLimits,
    analyzed: int,
    unsupported: Sequence[str],
) -> dict[str, Any]:
    ranked = sorted(
        scores,
        key=lambda score: (
            -score.risk,
            -score.function.cyclomatic,
            score.function.path,
            score.function.start_line,
        ),
    )
    crap_values = [score.crap for score in scores if score.crap is not None]
    return {
        "functions_analyzed": analyzed,
        "functions_in_scope": len(scores),
        "limits": dataclasses.asdict(limits),
        "rows": [_row(score, limits) for score in ranked[:REPORT_ROWS]],
        "cyclomatic": _distribution([s.function.cyclomatic for s in scores]),
        "cognitive": _distribution([s.function.cognitive for s in scores]),
        "nesting": _distribution([s.function.nesting for s in scores]),
        "crap": _distribution(crap_values),
        "crap_not_computed": len(scores) - len(crap_values),
        "not_analyzed": sorted(unsupported),
        "nesting_from_indentation": _nesting_from_indentation(scores),
    }
