"""The "Project checks" gate: run the lint, type, format, and build checks the
repository itself declares (see ``gate_project_checks_inference``).

A check that can never fail (``|| true``) or that rewrites files (``--fix``)
is refused rather than run, because passing it would prove nothing (see
``gate_project_checks_safety``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gate_project_checks_inference import (
    CheckSpec,
    configured_project_checks,
    infer_project_checks,
    project_check_specs,
)
from gate_project_checks_safety import command_problem, unsafe_reason
from gate_types import (
    BLOCKED,
    FAIL,
    NOT_APPLICABLE,
    PASS,
    CommandRun,
    GateItem,
    GateOutcome,
    Runner,
    Status,
    plural,
)

__all__ = [
    "CheckSpec",
    "command_problem",
    "configured_project_checks",
    "infer_project_checks",
    "project_check_specs",
    "run_project_checks",
    "unsafe_reason",
]

KEY = "project_checks"
TITLE = "Project checks"
NOT_DECLARED_SUMMARY = (
    "No lint, type, format, or build command is declared in this repository."
)
MISSING_EXECUTABLE = 127
OUTPUT_TAIL_LINES = 30
MAX_LISTED_FILES = 50


# ---------------------------------------------------------------------------
# Running


def _output_tail(output: str) -> list[str]:
    lines = [line for line in output.rstrip().splitlines() if line.strip()]
    return [f"    {line}" for line in lines[-OUTPUT_TAIL_LINES:]]


def _item(spec: CheckSpec, metric: str, value: str, limit: str) -> GateItem:
    return GateItem(spec.source, None, spec.name, metric, value, limit)


def _listed_file_items(spec: CheckSpec, output: str) -> list[GateItem]:
    """One item per file a listing tool (``gofmt -l``) printed."""
    paths = [line.strip() for line in output.splitlines() if line.strip()]
    items = [
        GateItem(
            path,
            None,
            spec.name,
            "needs formatting",
            "listed",
            "none",
        )
        for path in paths[:MAX_LISTED_FILES]
    ]
    if len(paths) > MAX_LISTED_FILES:
        rest = len(paths) - MAX_LISTED_FILES
        items.append(
            _item(
                spec,
                "more files listed",
                str(rest),
                "0",
            )
        )
    return items


def _failure_items(spec: CheckSpec, run: CommandRun, timeout: int) -> list[GateItem]:
    if run.timed_out:
        return [
            _item(
                spec,
                "runtime seconds",
                f"over {timeout}",
                str(timeout),
            )
        ]
    if run.returncode != 0:
        return [
            _item(
                spec,
                "exit code",
                str(run.returncode),
                "0",
            )
        ]
    if spec.fail_on_output and run.output.strip():
        return _listed_file_items(spec, run.output)
    return []


@dataclasses.dataclass
class _Tally:
    items: list[GateItem] = dataclasses.field(default_factory=list)
    details: list[str] = dataclasses.field(default_factory=list)
    rows: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    blocked: list[str] = dataclasses.field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row["status"] == status)

    def record(self, spec: CheckSpec, status: str, seconds: float = 0.0) -> None:
        self.rows.append(
            {
                "name": spec.name,
                "command": spec.display(),
                "source": spec.source,
                "status": status,
                "duration_seconds": round(seconds, 3),
            }
        )


def _run_one(spec: CheckSpec, runner: Runner, timeout: int, tally: _Tally) -> None:
    run = runner(spec.command, timeout)
    if run.returncode == MISSING_EXECUTABLE and not run.timed_out:
        tally.blocked.append(spec.name)
        tally.details.append(
            f"BLOCKED {spec.name}: executable not found ({spec.display()})"
        )
        tally.details.extend(_output_tail(run.output))
        tally.record(spec, "blocked", run.duration_seconds)
        return
    failures = _failure_items(spec, run, timeout)
    status = "fail" if failures else "pass"
    tally.items.extend(failures)
    tally.details.append(
        f"{status.upper()} {spec.name}: {spec.display()} ({run.duration_seconds:.1f}s)"
    )
    if failures:
        tally.details.extend(_output_tail(run.output))
    tally.record(spec, status, run.duration_seconds)


def _check(
    spec: CheckSpec, root: Path, runner: Runner, timeout: int, tally: _Tally
) -> None:
    reason = unsafe_reason(spec, root)
    if reason is None:
        _run_one(spec, runner, timeout, tally)
        return
    tally.items.append(
        _item(
            spec,
            "unsafe check",
            reason,
            "a check that can fail and only reads",
        )
    )
    tally.details.append(f"FAIL {spec.name}: not run, {reason}")
    tally.record(spec, "unsafe")


def _status_and_summary(tally: _Tally, total: int) -> tuple[Status, str]:
    if tally.items:
        failed = tally.count("fail") + tally.count("unsafe")
        return FAIL, f"{failed} of {plural(total, 'project check')} failed."
    if tally.blocked:
        return BLOCKED, (
            f"{len(tally.blocked)} of {plural(total, 'project check')} could not "
            "start: "
            "executable not found."
        )
    return PASS, f"Project checks passed: {total} of {total}."


def run_project_checks(
    root: Path,
    specs: Sequence[CheckSpec],
    runner: Runner,
    timeout_seconds: int,
) -> GateOutcome:
    """Run every declared check; each must exit 0 (listing tools: print nothing)."""
    if not specs:
        return GateOutcome(KEY, TITLE, NOT_APPLICABLE, NOT_DECLARED_SUMMARY)
    tally = _Tally()
    for spec in specs:
        _check(spec, root, runner, timeout_seconds, tally)
    status, summary = _status_and_summary(tally, len(specs))
    measurements: dict[str, Any] = {
        "checks": tally.rows,
        "failures": tally.count("fail") + tally.count("unsafe"),
        "blocked": len(tally.blocked),
    }
    return GateOutcome(
        KEY,
        TITLE,
        status,
        summary,
        tally.details,
        tally.items,
        measurements,
    )
