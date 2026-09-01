#!/usr/bin/env python3
"""Terminal report for one quality-loop run: what passed, what to fix, coverage today."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Sequence

DETAIL_LIMIT = 8
FIX_LIMIT = 12
GAP_LIMIT = 4
HAND_OFF_LINE = (
    "HAND OFF NOW: the next message is the hand-off — list the deliverables (ticked) "
    "and quote the QUALITY_LOOP line. Do not add tools, configs, or checks after green."
)


def report_header(analysis: Any) -> str:
    flags = " ".join(f"--{name}" for name in analysis.selection)
    mode = f"partial ({flags})" if analysis.mode == "partial" else analysis.mode
    return f"QUALITY REPORT · mode: {mode} · scope: {analysis.scope.description}"


def first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0][:200] if lines else ""


def gate_lines(gate: ModuleType, result: Any) -> list[str]:
    status = gate.gate_status(result)
    if status == "not_applicable":
        return [
            f"[N/A] {result.title}: {result.summary} Nothing to do here; not needed for hand-off."
        ]
    lines = [f"[{gate.gate_outcome(result)}] {result.title}: {result.summary}"]
    if status != "fail":
        return lines
    lines.extend(
        f"    {first_line(detail)}" for detail in result.details[:DETAIL_LIMIT]
    )
    return lines


def coverage_today(functions: Sequence[Any], limit: float) -> dict[str, Any] | None:
    measured = [
        function
        for function in functions
        if getattr(function, "coverage_measured", True)
    ]
    if not measured:
        return None
    gaps: dict[str, int] = {}
    for function in measured:
        if function.coverage_percent < limit:
            gaps[function.path] = gaps.get(function.path, 0) + 1
    covered = len(measured) - sum(gaps.values())
    return {
        "covered": covered,
        "total": len(measured),
        "percent": round(100 * covered / len(measured)),
        "gaps": sorted(gaps.items(), key=lambda item: (-item[1], item[0]))[:GAP_LIMIT],
    }


def coverage_summary_text(summary: dict[str, Any]) -> str:
    text = (
        f"{summary['covered']} of {summary['total']} functions fully covered "
        f"({summary['percent']}%)"
    )
    missing = summary["total"] - summary["covered"]
    if missing == 0:
        return text
    where = ", ".join(f"{path} ({count})" for path, count in summary["gaps"])
    return f"{text} · {missing} not covered — most in: {where}"


def coverage_today_line(
    analysis: Any, previous: tuple[str, dict[str, Any]] | None
) -> str:
    limit = float(analysis.thresholds.get("metrics", {}).get("coverage_limit", 100))
    summary = coverage_today(analysis.functions, limit)
    if summary:
        return f"Coverage today: {coverage_summary_text(summary)}"
    if previous:
        generated_at, older = previous
        return (
            f"Coverage today: not measured in this run — last measured {generated_at}: "
            f"{coverage_summary_text(older)}"
        )
    return "Coverage today: not measured yet — run --fast or --coverage."


def previous_measurement(
    state_path: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Coverage summary from the previous state file, for runs that measure nothing."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    functions = [
        SimpleNamespace(**item)
        for item in state.get("metrics", {}).get("functions", [])
    ]
    limit = float(
        state.get("thresholds", {}).get("metrics", {}).get("coverage_limit", 100)
    )
    summary = coverage_today(functions, limit)
    return (str(state.get("generated_at", "earlier")), summary) if summary else None


def to_fix_items(state: dict[str, Any]) -> list[str]:
    failures = state["failures"]
    items = [
        f"{item['path']}:{item['line']} {item['name']} — coverage {item['coverage_percent']:g}%, complexity {item['complexity']}"
        for item in failures["functions"]
    ]
    items.extend(
        f"{item['path']} — {item['lines']} lines (max {item['limit']})"
        for item in failures["files"]
    )
    items.extend(
        f"{item['path']}:{item['line']} surviving mutant `{item['original']}` -> `{item['replacement']}`"
        for item in failures["surviving_mutants"]
    )
    items.extend(
        f"{item['source']}:{item['line']} -> {item['target']}: {item['rule']}"
        for item in failures["dependencies"]
    )
    items.extend(check_failure_items(failures["checks"], itemized_gates(failures)))
    return items


def itemized_gates(failures: dict[str, Any]) -> set[str]:
    """Gates whose failures are already listed item by item above the check summaries."""
    sources = (
        ("quality", "functions"),
        ("file_loc", "files"),
        ("mutation", "surviving_mutants"),
        ("dependencies", "dependencies"),
    )
    return {key for key, field in sources if failures.get(field)}


def check_failure_items(
    checks: Sequence[dict[str, Any]], itemized: set[str] | frozenset[str] = frozenset()
) -> list[str]:
    items: list[str] = []
    for item in checks:
        if item["key"] in itemized:
            continue
        if item["key"] == "scope":
            items.extend(item.get("details", []))
            continue
        items.append(f"{item['title']}: {item['summary']}")
    return items


def to_fix_lines(state: dict[str, Any]) -> list[str]:
    items = to_fix_items(state)
    lines = [f"  {number}. {item}" for number, item in enumerate(items[:FIX_LIMIT], 1)]
    if len(items) > FIX_LIMIT:
        lines.append(f"  … and {len(items) - FIX_LIMIT} more (see STATE)")
    return lines


def next_step_lines(state: dict[str, Any], analysis: Any) -> list[str]:
    if state["status"] == "pass" and analysis.mode != "full":
        return [
            "Selected checks are green. This does not certify: run the full ship report:",
            f"  {state['full_rerun_command']}",
        ]
    if state["status"] == "ready_for_full":
        return [
            "Fast checks are green. Run the full ship report now:",
            f"  {state['full_rerun_command']}",
        ]
    if state["status"] == "pass":
        return [
            "Ship report is green: every executed check passed.",
            HAND_OFF_LINE,
        ]
    return ["Fix the items above, then rerun:", f"  {state['rerun_command']}"]


def print_report(
    gate: ModuleType,
    analysis: Any,
    state: dict[str, Any],
    state_path: Path,
    html_path: Path,
    print_prompt: bool,
    previous: tuple[str, dict[str, Any]] | None = None,
) -> None:
    print(report_header(analysis))
    for result in analysis.gates:
        print("\n".join(gate_lines(gate, result)))
    print(coverage_today_line(analysis, previous))
    fixes = to_fix_lines(state)
    if fixes:
        print("To fix:")
        print("\n".join(fixes))
    print("\n".join(next_step_lines(state, analysis)))
    print(f"QUALITY_LOOP={state['status'].upper()}")
    print(f"ITEMS_TO_FIX={len(to_fix_items(state))}")
    print(f"STATE={state_path}")
    print(f"HTML={html_path}")
    if print_prompt and state["fix_prompt"]:
        print("\n" + state["fix_prompt"])
