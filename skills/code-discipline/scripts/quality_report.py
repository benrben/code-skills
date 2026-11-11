#!/usr/bin/env python3
"""Terminal report for one quality-loop run: each gate's result and its issues.

The report reads the state file written from ``quality_model.analysis_state``.
It shows issues only: where, what, the measured value, and the limit.
"""

from __future__ import annotations

import dataclasses
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO, TypeGuard

from gate_types import NOT_APPLICABLE, PASS, plural
from quality_config import QUALITY_DIRECTORY, REPORT_NAME, STATE_NAME
from quality_html import NOT_PASSED, STATUS_LABELS, ReportContext, render_report
from quality_model import (
    NOT_RUN,
    STATE_SCHEMA_VERSION,
    analysis_from_state,
    gate_rows,
)

ISSUE_LINES_PER_GATE = 8
FIX_LIMIT = 12
DETAIL_WIDTH = 200
REPOSITORY = "(repository)"
LABELS: dict[str, str] = {str(key): label for key, label in STATUS_LABELS.items()}
ITEMS_SCRIPT = Path(__file__).resolve().with_name("quality_items.py")

HAND_OFF_LINE = (
    "HAND OFF NOW: the next message is the hand-off — list the deliverables "
    "(ticked) and quote the QUALITY_LOOP line. Do not add tools, configs, or "
    "checks after green."
)
PASS_LINE = "Ship report is green: every gate that ran passed."
SELECTED_LINE = "Selected gates are green. This does not certify; run the full report:"
COMMIT_LINE = "Fast checks are green. Commit this step in one command:"
COMMIT_COMMAND = '  git add -A && git commit -m "<one line: what this step did>"'
CONTINUE_LINE = "Then continue; before hand-off run the full ship report:"
RERUN_LINE = "Rerun after the fixes:"
MORE_FILES_NOTE = "quality_items.py --summary lists them"


@dataclasses.dataclass(frozen=True)
class Issue:
    """One open issue of a failing gate.

    A failing gate without itemized findings becomes one repository-level
    issue: its title, its summary, and the first line of each detail.
    """

    gate: str
    path: str
    line: int | None
    name: str
    metric: str
    value: str
    limit: str
    details: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        """Identity across runs: line numbers count only for unnamed issues."""
        position = "" if self.name else str(self.line or "")
        return (self.gate, self.path, self.name, self.metric, position)

    @property
    def repository_level(self) -> bool:
        return self.path == REPOSITORY

    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def describe(self) -> str:
        """The issue without its location."""
        if self.repository_level:
            return f"{self.metric}: {self.value}"
        head = f"{self.name}: " if self.name else ""
        return head + self._measurement()

    def text(self) -> str:
        """``path:line name: metric value (limit L)``."""
        if self.repository_level:
            return self.describe()
        label = f" {self.name}" if self.name else ""
        return f"{self.location()}{label}: {self._measurement()}"

    def _measurement(self) -> str:
        value = f" {self.value}" if self.value else ""
        limit = f" (limit {self.limit})" if self.limit else ""
        return f"{self.metric}{value}{limit}"


Groups = list[tuple[str, list[Issue]]]


def is_current_state(
    state: Mapping[str, Any] | None,
) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(state, Mapping) and (
        state.get("schema_version") == STATE_SCHEMA_VERSION
    )


def gate_failed(gate: Mapping[str, Any]) -> bool:
    """A gate that ran and did not pass (FAIL or BLOCKED)."""
    status = str(gate.get("status", ""))
    return status not in NOT_RUN and status not in (PASS, NOT_APPLICABLE)


def first_line(text: object) -> str:
    lines = str(text).strip().splitlines()
    return lines[0][:DETAIL_WIDTH] if lines else ""


def _line_number(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def item_issue(gate_key: str, item: Mapping[str, Any]) -> Issue:
    return Issue(
        gate_key,
        str(item.get("path") or REPOSITORY),
        _line_number(item.get("line")),
        str(item.get("name") or ""),
        str(item.get("metric") or ""),
        str(item.get("value") or ""),
        str(item.get("limit") or ""),
    )


def gate_issue(gate: Mapping[str, Any]) -> Issue:
    details = tuple(
        line for line in (first_line(d) for d in gate.get("details") or []) if line
    )
    key = str(gate.get("key", ""))
    return Issue(
        key,
        REPOSITORY,
        None,
        "",
        str(gate.get("title") or key),
        str(gate.get("summary") or ""),
        "",
        details,
    )


def gate_issues(gate: Mapping[str, Any]) -> list[Issue]:
    if not gate_failed(gate):
        return []
    key = str(gate.get("key", ""))
    items = gate.get("items") or []
    if items:
        return [item_issue(key, item) for item in items]
    return [gate_issue(gate)]


def issues_from_state(state: Mapping[str, Any]) -> list[Issue]:
    """Every open issue of every failing gate, in gate order."""
    return [issue for gate in state.get("gates") or [] for issue in gate_issues(gate)]


def files_in_order(issues: Sequence[Issue]) -> Groups:
    """Files with open issues: most issues first, repository-level issues last."""
    groups: dict[str, list[Issue]] = {}
    for issue in issues:
        groups.setdefault(issue.path, []).append(issue)
    return sorted(
        groups.items(),
        key=lambda entry: (entry[0] == REPOSITORY, -len(entry[1]), entry[0]),
    )


def metric_breakdown(issues: Sequence[Issue]) -> str:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.metric] = counts.get(issue.metric, 0) + 1
    ordered = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))
    return " · ".join(f"{count} {metric}" for metric, count in ordered)


def file_line(number: int, path: str, issues: Sequence[Issue]) -> str:
    return (
        f"  {number}. {path} — {plural(len(issues), 'issue')} "
        f"({metric_breakdown(issues)})"
    )


def summary_header(issues: Sequence[Issue], groups: Groups) -> str:
    return (
        f"{plural(len(issues), 'issue')} in {plural(len(groups), 'file')} "
        "— one file per cycle, top first"
    )


def grouped_fix_lines(issues: Sequence[Issue]) -> list[str]:
    groups = files_in_order(issues)
    lines = [f"  {summary_header(issues, groups)}"]
    lines.extend(
        file_line(number, path, grouped)
        for number, (path, grouped) in enumerate(groups[:FIX_LIMIT], 1)
    )
    if len(groups) > FIX_LIMIT:
        lines.append(
            f"  … and {len(groups) - FIX_LIMIT} more files ({MORE_FILES_NOTE})"
        )
    return lines


def fix_list_lines(issues: Sequence[Issue]) -> list[str]:
    if len(issues) > FIX_LIMIT:
        return grouped_fix_lines(issues)
    return [f"  {number}. {issue.text()}" for number, issue in enumerate(issues, 1)]


def state_root(state: Mapping[str, Any]) -> Path:
    return Path(str(state.get("root") or "."))


def default_state_path(root: Path) -> Path:
    return root / QUALITY_DIRECTORY / STATE_NAME


def state_path_for(state: Mapping[str, Any]) -> Path:
    """Where this state file lives: next to its HTML report."""
    explicit = state.get("state_path")
    if explicit:
        return Path(str(explicit))
    html = state.get("html")
    if html:
        return Path(str(html)).with_name(STATE_NAME)
    return default_state_path(state_root(state))


def html_path_for(state: Mapping[str, Any]) -> Path:
    html = state.get("html")
    if html:
        return Path(str(html))
    return state_root(state) / QUALITY_DIRECTORY / REPORT_NAME


def next_file_command(state: Mapping[str, Any]) -> str:
    root = state_root(state)
    command = ["python3", str(ITEMS_SCRIPT), "--root", str(root)]
    state_path = state_path_for(state)
    if state_path != default_state_path(root):
        command.extend(["--state", str(state_path)])
    command.append("--next")
    return shlex.join(command)


def status_label(status: str) -> str:
    return LABELS.get(status, status.upper())


def gate_issue_texts(gate: Mapping[str, Any]) -> list[str]:
    items = gate.get("items") or []
    if items:
        return [item_issue(str(gate.get("key", "")), item).text() for item in items]
    return list(gate_issue(gate).details)


def gate_lines(gate: Mapping[str, Any]) -> list[str]:
    title = gate.get("title") or gate.get("key", "")
    status = str(gate.get("status", ""))
    lines = [f"[{status_label(status)}] {title}: {gate.get('summary', '')}"]
    if not gate_failed(gate):
        return lines
    texts = gate_issue_texts(gate)
    lines.extend(f"  {text}" for text in texts[:ISSUE_LINES_PER_GATE])
    if len(texts) > ISSUE_LINES_PER_GATE:
        lines.append(f"  … and {len(texts) - ISSUE_LINES_PER_GATE} more")
    return lines


def coverage_measurement(state: Mapping[str, Any]) -> str | None:
    """The coverage this run measured, or None when coverage did not run."""
    for gate in state.get("gates") or []:
        if gate.get("key") != "coverage" or gate.get("status") in NOT_RUN:
            continue
        values = gate.get("measurements") or {}
        line = values.get("line_coverage_percent")
        if not isinstance(line, (int, float)):
            return None
        text = (
            f"{line:g}% of lines ({values.get('covered_lines', 0)}/"
            f"{values.get('total_lines', 0)})"
        )
        branch = values.get("branch_coverage_percent")
        if isinstance(branch, (int, float)):
            text += (
                f", {branch:g}% of branches ({values.get('covered_branches', 0)}/"
                f"{values.get('total_branches', 0)})"
            )
        return text
    return None


def coverage_today_line(
    state: Mapping[str, Any], previous_state: Mapping[str, Any] | None
) -> str:
    current = coverage_measurement(state)
    if current:
        return f"Coverage today: {current}"
    if is_current_state(previous_state):
        earlier = coverage_measurement(previous_state)
        if earlier:
            when = previous_state.get("generated_at") or "earlier"
            return (
                "Coverage today: not measured in this run — "
                f"last measured {when}: {earlier}"
            )
    return "Coverage today: not measured in this run."


def delta_line(
    issues: Sequence[Issue], previous_state: Mapping[str, Any] | None
) -> str | None:
    if not is_current_state(previous_state):
        return None
    previous = {issue.key for issue in issues_from_state(previous_state)}
    current = {issue.key for issue in issues}
    return (
        f"Since last run: fixed {len(previous - current)} · "
        f"remaining {len(issues)} · new {len(current - previous)}"
    )


def command_lines(heading: str, command: object) -> list[str]:
    text = str(command or "")
    return [heading, f"  {text}"] if text else []


def next_step_lines(state: Mapping[str, Any]) -> list[str]:
    status = state.get("status")
    full = state.get("full_rerun_command")
    if status == "pass":
        return [PASS_LINE, HAND_OFF_LINE]
    if status == "selected_pass":
        return command_lines(SELECTED_LINE, full)
    if status == "ready_for_full":
        return [COMMIT_LINE, COMMIT_COMMAND, *command_lines(CONTINUE_LINE, full)]
    return command_lines(RERUN_LINE, state.get("rerun_command"))


def header_line(state: Mapping[str, Any]) -> str:
    mode = state.get("mode") or "full"
    scope = state.get("scope") or "whole repository"
    return f"QUALITY REPORT · {mode} · {scope}"


def fix_block(state: Mapping[str, Any], issues: Sequence[Issue]) -> list[str]:
    if not issues:
        return []
    return [
        NOT_PASSED,
        *fix_list_lines(issues),
        f"Next file: {next_file_command(state)}",
    ]


def report_lines(
    state: Mapping[str, Any], previous_state: Mapping[str, Any] | None = None
) -> list[str]:
    """Every line of the terminal report."""
    issues = issues_from_state(state)
    lines = [header_line(state)]
    for gate in state.get("gates") or []:
        lines.extend(gate_lines(gate))
    lines.extend(f"Note: {note}" for note in state.get("notes") or [])
    lines.append(coverage_today_line(state, previous_state))
    lines.extend(fix_block(state, issues))
    delta = delta_line(issues, previous_state)
    if delta:
        lines.append(delta)
    lines.extend(next_step_lines(state))
    lines.extend(
        [
            f"QUALITY_LOOP={str(state.get('status') or 'error').upper()}",
            f"ITEMS_TO_FIX={len(issues)}",
            f"STATE={state_path_for(state)}",
            f"HTML={html_path_for(state)}",
        ]
    )
    return lines


def print_report(
    state: dict[str, Any],
    *,
    previous_state: dict[str, Any] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Print the terminal report (``stream`` defaults to standard output)."""
    output = stream if stream is not None else sys.stdout
    output.write("\n".join(report_lines(state, previous_state)) + "\n")
    output.flush()


def verdict_label(status: str) -> str:
    return status.replace("_", " ").upper()


def render_html(state: Mapping[str, Any]) -> str:
    """The HTML report for a schema-2 state; raises ValueError for older states."""
    analysis = analysis_from_state(state)
    context = ReportContext(
        repository=analysis.root,
        scope=analysis.scope,
        generated_at=analysis.generated_at,
        verdict=verdict_label(str(state.get("status") or analysis.status)),
        command=analysis.rerun_command,
        notes=tuple(analysis.notes),
    )
    return render_report(context, gate_rows(analysis))
