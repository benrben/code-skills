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
REPOSITORY = "(repository)"
DEFAULT_LIMITS = {"coverage_limit": 100.0, "complexity_limit": 6.0, "craap_limit": 6.0}
HAND_OFF_LINE = (
    "HAND OFF NOW: the next message is the hand-off — list the deliverables (ticked) "
    "and quote the QUALITY_LOOP line. Do not add tools, configs, or checks after green."
)
COMMIT_LINE = "Fast checks are green. Commit this step in one command:"
COMMIT_COMMAND = '  git add -A && git commit -m "<one line: what this step did>"'
CONTINUE_LINE = "Then continue; before hand-off run the full ship report:"
EMPTY_SCOPE_LINE = (
    "Local-changes scope is empty: everything is committed and nothing was "
    "measured — this is not a certification."
)
MORE_FILES_HINT = "quality_items.py --summary lists them"


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


def metric_limits(state: dict[str, Any]) -> dict[str, float]:
    metrics = state.get("thresholds", {}).get("metrics", {})
    return {
        key: float(metrics.get(key, default)) for key, default in DEFAULT_LIMITS.items()
    }


def function_hint(item: dict[str, Any], limits: dict[str, float]) -> str:
    hints: list[str] = []
    if item["coverage_percent"] < limits["coverage_limit"]:
        missing = item.get("total_lines", 0) - item.get("covered_lines", 0)
        where = (
            f"its {missing} uncovered lines" if missing > 0 else "its untested paths"
        )
        hints.append(f"add a test that reaches {where}")
    if item["complexity"] > limits["complexity_limit"]:
        hints.append(
            f"complexity {item['complexity']} > {limits['complexity_limit']:g}: "
            "split it into smaller functions"
        )
    if item.get("craap_score", 0) > limits["craap_limit"]:
        hints.append(
            f"CRAAP {item['craap_score']:g} > {limits['craap_limit']:g}: "
            "cover it or simplify it"
        )
    return "; ".join(hints) or "cover it or simplify it"


def function_record(item: dict[str, Any], limits: dict[str, float]) -> dict[str, Any]:
    below_coverage = item["coverage_percent"] < limits["coverage_limit"]
    return {
        "kind": "function",
        "path": item["path"],
        "line": item["line"],
        "key": f"function {item['path']} {item['name']}",
        "text": (
            f"{item['path']}:{item['line']} {item['name']} — "
            f"coverage {item['coverage_percent']:g}%, complexity {item['complexity']}"
        ),
        "hint": function_hint(item, limits),
        "metric": "coverage" if below_coverage else "complexity",
    }


def file_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "file",
        "path": item["path"],
        "line": 1,
        "key": f"file {item['path']}",
        "text": f"{item['path']} — {item['lines']} lines (max {item['limit']})",
        "hint": f"split the file: {item['lines']} lines, max {item['limit']}",
        "metric": "file size",
    }


def mutant_record(item: dict[str, Any]) -> dict[str, Any]:
    text = f"{item['path']}:{item['line']} surviving mutant `{item['change']}`"
    return {
        "kind": "mutant",
        "path": item["path"],
        "line": item["line"],
        "key": f"mutant {text}",
        "text": text,
        "hint": f"add a test that fails when the code changes `{item['change']}`",
        "metric": "mutant",
    }


def dependency_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "dependency",
        "path": item["source"],
        "line": item["line"],
        "key": f"dependency {item['source']} -> {item['target']}",
        "text": f"{item['source']}:{item['line']} -> {item['target']}: {item['rule']}",
        "hint": (
            f"remove the import of {item['target']} or allow the edge "
            "in the dependency rules"
        ),
        "metric": "dependency",
    }


def scope_record(detail: str) -> dict[str, Any]:
    return {
        "kind": "scope",
        "path": detail.split(" — ", 1)[0],
        "line": 0,
        "key": f"scope {detail}",
        "text": detail,
        "hint": "remove the exclude entry; give the file a startup or smoke test instead",
        "metric": "scope",
    }


def check_record(item: dict[str, Any]) -> dict[str, Any]:
    details = [first_line(detail) for detail in item.get("details", [])[:20]]
    return {
        "kind": "check",
        "path": REPOSITORY,
        "line": 0,
        "key": f"check {item['key']}",
        "text": f"{item['title']}: {item['summary']}",
        "hint": details[0] if details else "read the command output in the report",
        "metric": item["title"],
        "details": details,
    }


def check_records(
    checks: Sequence[dict[str, Any]], itemized: set[str] | frozenset[str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in checks:
        if item["key"] in itemized:
            continue
        if item["key"] == "scope":
            records.extend(scope_record(detail) for detail in item.get("details", []))
            continue
        records.append(check_record(item))
    return records


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
    return [record["text"] for record in check_records(checks, itemized)]


def item_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every open item as a record: path, line, key, text, hint, metric."""
    failures = state["failures"]
    limits = metric_limits(state)
    records = [function_record(item, limits) for item in failures["functions"]]
    records.extend(file_record(item) for item in failures["files"])
    records.extend(mutant_record(item) for item in failures["surviving_mutants"])
    records.extend(dependency_record(item) for item in failures["dependencies"])
    records.extend(check_records(failures["checks"], itemized_gates(failures)))
    return records


def to_fix_items(state: dict[str, Any]) -> list[str]:
    return [record["text"] for record in item_records(state)]


def files_in_order(
    records: Sequence[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Files with open items: most items first, repository-level items last."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record["path"], []).append(record)
    return sorted(
        groups.items(),
        key=lambda entry: (entry[0] == REPOSITORY, -len(entry[1]), entry[0]),
    )


def metric_breakdown(records: Sequence[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["metric"]] = counts.get(record["metric"], 0) + 1
    ordered = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))
    return " · ".join(f"{count} {metric}" for metric, count in ordered)


def file_line(number: int, path: str, records: Sequence[dict[str, Any]]) -> str:
    noun = "item" if len(records) == 1 else "items"
    return f"  {number}. {path} — {len(records)} {noun} ({metric_breakdown(records)})"


def grouped_fix_lines(records: Sequence[dict[str, Any]]) -> list[str]:
    groups = files_in_order(records)
    lines = [
        f"  {len(records)} items in {len(groups)} files — one file per cycle, top first"
    ]
    lines.extend(
        file_line(number, path, items)
        for number, (path, items) in enumerate(groups[:FIX_LIMIT], 1)
    )
    if len(groups) > FIX_LIMIT:
        lines.append(
            f"  … and {len(groups) - FIX_LIMIT} more files ({MORE_FILES_HINT})"
        )
    return lines


def to_fix_lines(state: dict[str, Any]) -> list[str]:
    records = item_records(state)
    if len(records) <= FIX_LIMIT:
        return [
            f"  {number}. {record['text']}" for number, record in enumerate(records, 1)
        ]
    return grouped_fix_lines(records)


def item_keys(state: dict[str, Any]) -> set[str]:
    return {record["key"] for record in item_records(state)}


def previous_item_keys(state_path: Path) -> set[str] | None:
    """Open-item keys from the previous state file, for the 'Since last run' line."""
    try:
        return item_keys(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def delta_line(previous: set[str] | None, current: set[str]) -> str | None:
    if previous is None:
        return None
    fixed = len(previous - current)
    new = len(current - previous)
    return f"Since last run: fixed {fixed} · remaining {len(current)} · new {new}"


def next_file_line(state_path: Path) -> str:
    script = Path(__file__).with_name("quality_items.py")
    return f"Next file:  python3 {script} --state {state_path} --next"


def whole_repository_command(command: str) -> str:
    """Strip incremental-scope flags so the suggestion certifies the whole repository."""
    parts = [part for part in command.split() if part != "--local-changes"]
    if "--commit" in parts:
        index = parts.index("--commit")
        del parts[index : index + 2]
    return " ".join(parts)


def empty_incremental_scope(state: dict[str, Any]) -> bool:
    scope = state.get("scope") or {}
    return scope.get("kind") in ("local_changes", "commit") and not scope.get(
        "changed_files"
    )


def next_step_lines(state: dict[str, Any], analysis: Any) -> list[str]:
    if state["status"] == "pass" and analysis.mode != "full":
        return [
            "Selected checks are green. This does not certify: run the full ship report:",
            f"  {state['full_rerun_command']}",
        ]
    if state["status"] == "ready_for_full":
        return [
            COMMIT_LINE,
            COMMIT_COMMAND,
            CONTINUE_LINE,
            f"  {state['full_rerun_command']}",
        ]
    if empty_incremental_scope(state):
        return [
            EMPTY_SCOPE_LINE,
            "Run the whole-repository ship report:",
            f"  {whole_repository_command(state['full_rerun_command'])}",
        ]
    if state["status"] == "pass":
        return [
            "Ship report is green: every executed check passed.",
            HAND_OFF_LINE,
        ]
    return ["Fix the items above, then rerun:", f"  {state['rerun_command']}"]


def print_fix_block(
    state: dict[str, Any], state_path: Path, previous_items: set[str] | None
) -> int:
    records = item_records(state)
    delta = delta_line(previous_items, {record["key"] for record in records})
    if delta:
        print(delta)
    if records:
        print("To fix:")
        print("\n".join(to_fix_lines(state)))
        print(next_file_line(state_path))
    return len(records)


def print_report(
    gate: ModuleType,
    analysis: Any,
    state: dict[str, Any],
    state_path: Path,
    html_path: Path,
    print_prompt: bool,
    previous: tuple[str, dict[str, Any]] | None = None,
    previous_items: set[str] | None = None,
) -> None:
    print(report_header(analysis))
    for result in analysis.gates:
        print("\n".join(gate_lines(gate, result)))
    print(coverage_today_line(analysis, previous))
    count = print_fix_block(state, state_path, previous_items)
    print("\n".join(next_step_lines(state, analysis)))
    print(f"QUALITY_LOOP={state['status'].upper()}")
    print(f"ITEMS_TO_FIX={count}")
    print(f"STATE={state_path}")
    print(f"HTML={html_path}")
    if print_prompt and state["fix_prompt"]:
        print("\n" + state["fix_prompt"])
