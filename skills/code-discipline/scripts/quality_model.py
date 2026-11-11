"""The result of one quality run and the state file that records it.

The orchestrator builds an ``Analysis``; the terminal report, the item
commands, the HTML report, and later runs read the state file written from it.
Reports show issues only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gate_types import (
    BLOCKED,
    DEFERRED,
    NOT_APPLICABLE,
    OFF,
    SKIPPED,
    GateItem,
    GateOutcome,
    Status,
)

STATE_SCHEMA_VERSION = 2

GATE_ORDER = (
    "shortcuts",
    "secrets",
    "file_loc",
    "project_checks",
    "tests",
    "coverage",
    "complexity",
    "duplication",
    "dead_code",
    "vulnerabilities",
)

GATE_TITLES = {
    "shortcuts": "No shortcuts",
    "secrets": "Secrets",
    "file_loc": "File size",
    "project_checks": "Project checks",
    "tests": "Tests",
    "coverage": "Coverage",
    "complexity": "Complexity",
    "duplication": "Duplication",
    "dead_code": "Dead code",
    "vulnerabilities": "Vulnerable dependencies",
}

# A gate that cannot measure is BLOCKED, except these two: a repository with
# no declared checks or no dependency manifests has nothing to judge.
MAY_BE_NOT_APPLICABLE = frozenset({"project_checks", "vulnerabilities"})

NOT_RUN = frozenset({OFF, SKIPPED, DEFERRED})

FULL = "full"
FAST = "fast"
PARTIAL = "partial"


@dataclasses.dataclass
class GateRun(GateOutcome):
    """One gate in one run: its outcome, or why it did not run, and how long
    it took. ``seconds`` is keyword-only."""

    seconds: float | None = None

    @classmethod
    def from_outcome(cls, outcome: GateOutcome, seconds: float | None) -> GateRun:
        status: Status = outcome.status
        if status == NOT_APPLICABLE and outcome.key not in MAY_BE_NOT_APPLICABLE:
            status = BLOCKED
        return cls(
            outcome.key,
            outcome.title,
            status,
            outcome.summary,
            list(outcome.details),
            list(outcome.items),
            dict(outcome.measurements),
            seconds=seconds,
        )

    @classmethod
    def not_run(cls, key: str, status: Status, summary: str) -> GateRun:
        return cls(key, GATE_TITLES.get(key, key), status, summary)

    @property
    def ran(self) -> bool:
        return self.status not in NOT_RUN


@dataclasses.dataclass
class Analysis:
    """Everything one run decided."""

    root: str
    generated_at: str
    scope: str
    mode: str
    gates: list[GateRun]
    version: str = ""
    reference: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)
    rerun_command: str = ""
    full_rerun_command: str = ""
    baselines: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def ran_gates(self) -> list[GateRun]:
        return [gate for gate in self.gates if gate.ran]

    @property
    def passed(self) -> bool:
        """A full run where at least one gate ran and every gate that ran
        passed. Fast and partial runs never certify."""
        ran = self.ran_gates
        return (
            self.mode == FULL
            and bool(ran)
            and all(gate.passed for gate in ran)
            and not any(gate.status == DEFERRED for gate in self.gates)
        )

    @property
    def selected_passed(self) -> bool:
        ran = self.ran_gates
        return self.mode == PARTIAL and bool(ran) and all(g.passed for g in ran)

    @property
    def ready_for_full(self) -> bool:
        ran = self.ran_gates
        return self.mode == FAST and bool(ran) and all(g.passed for g in ran)

    @property
    def status(self) -> str:
        if self.passed:
            return "pass"
        if self.selected_passed:
            return "selected_pass"
        if self.ready_for_full:
            return "ready_for_full"
        return "fail"

    @property
    def failing_gates(self) -> list[GateRun]:
        return [gate for gate in self.gates if gate.ran and not gate.passed]

    def issues(self) -> list[tuple[str, GateItem]]:
        """(gate key, item) for every issue of every failing gate."""
        return [(gate.key, item) for gate in self.failing_gates for item in gate.items]


def item_record(key: str, item: GateItem) -> dict[str, Any]:
    return {
        "gate": key,
        "path": item.path,
        "line": item.line,
        "name": item.name,
        "metric": item.metric,
        "value": item.value,
        "limit": item.limit,
    }


def gate_record(gate: GateRun) -> dict[str, Any]:
    return {
        "key": gate.key,
        "title": gate.title,
        "status": gate.status,
        "summary": gate.summary,
        "details": list(gate.details),
        "items": [item_record(gate.key, item) for item in gate.items],
        "measurements": _json_safe(gate.measurements),
        "seconds": gate.seconds,
    }


def analysis_state(analysis: Analysis, html_path: str = "") -> dict[str, Any]:
    """The JSON state file for one run."""
    issues = [item_record(key, item) for key, item in analysis.issues()]
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "version": analysis.version,
        "generated_at": analysis.generated_at,
        "root": analysis.root,
        "scope": analysis.scope,
        "reference": analysis.reference,
        "mode": analysis.mode,
        "status": analysis.status,
        "gates": [gate_record(gate) for gate in analysis.gates],
        "issues": issues,
        "notes": list(analysis.notes),
        "rerun_command": analysis.rerun_command,
        "full_rerun_command": analysis.full_rerun_command,
        "baselines": _json_safe(analysis.baselines),
        "html": html_path,
    }


def analysis_from_state(state: Mapping[str, Any]) -> Analysis:
    """Rebuild an ``Analysis`` from a state file of this schema version."""
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(
            "the saved state comes from an older quality loop; run the loop once"
        )
    gates = [_gate_from_record(record) for record in state.get("gates", [])]
    return Analysis(
        root=str(state.get("root", "")),
        generated_at=str(state.get("generated_at", "")),
        scope=str(state.get("scope", "")),
        mode=str(state.get("mode", FULL)),
        gates=gates,
        version=str(state.get("version", "")),
        reference=state.get("reference"),
        notes=[str(note) for note in state.get("notes", [])],
        rerun_command=str(state.get("rerun_command", "")),
        full_rerun_command=str(state.get("full_rerun_command", "")),
        baselines=dict(state.get("baselines") or {}),
    )


def _gate_from_record(record: Mapping[str, Any]) -> GateRun:
    items = [
        GateItem(
            str(item.get("path", "")),
            item.get("line"),
            str(item.get("name", "")),
            str(item.get("metric", "")),
            str(item.get("value", "")),
            str(item.get("limit", "")),
        )
        for item in record.get("items", [])
    ]
    return GateRun(
        str(record.get("key", "")),
        str(record.get("title", "")),
        _status(record.get("status")),
        str(record.get("summary", "")),
        [str(line) for line in record.get("details", [])],
        items,
        dict(record.get("measurements") or {}),
        seconds=record.get("seconds"),
    )


_STATUSES: frozenset[str] = frozenset(
    {"pass", "fail", "blocked", "not_applicable", OFF, SKIPPED, DEFERRED}
)


def _status(value: object) -> Status:
    """A saved status, or BLOCKED for anything unknown: an unreadable result
    must never read as a pass."""
    for status in ("pass", "fail", "blocked", "not_applicable", OFF, SKIPPED, DEFERRED):
        if value == status:
            return status
    return BLOCKED


def read_state(path: Path) -> dict[str, Any] | None:
    """The saved state, or None when there is no readable state file."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No previous run, or a damaged file: both mean "no saved state".
        return None
    return value if isinstance(value, dict) else None


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [_json_safe(item) for item in items]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def gate_rows(analysis: Analysis) -> Sequence[GateRun]:
    """Gates in report order."""
    order = {key: index for index, key in enumerate(GATE_ORDER)}
    return sorted(analysis.gates, key=lambda gate: order.get(gate.key, len(order)))
