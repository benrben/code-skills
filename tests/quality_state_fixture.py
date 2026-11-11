"""Hand-made analyses and state files for the report, items, and update tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "skills/code-discipline/scripts")
)

import quality_html  # noqa: E402
import quality_items  # noqa: E402
import quality_model  # noqa: E402
import quality_report  # noqa: E402
import quality_update  # noqa: E402
from gate_types import PASS, GateItem, Status  # noqa: E402

MODULES = (quality_html, quality_items, quality_model, quality_report, quality_update)


def item(
    path: str = "app.py",
    line: int | None = 3,
    name: str = "total",
    metric: str = "cyclomatic",
    value: str = "12",
    limit: str = "10",
) -> GateItem:
    return GateItem(path, line, name, metric, value, limit)


def gate(
    key: str,
    status: Status = PASS,
    summary: str = "ok",
    items: list[GateItem] | None = None,
    details: list[str] | None = None,
    measurements: dict[str, Any] | None = None,
) -> quality_model.GateRun:
    return quality_model.GateRun(
        key,
        quality_model.GATE_TITLES.get(key, key),
        status,
        summary,
        list(details or []),
        list(items or []),
        dict(measurements or {}),
        seconds=0.5,
    )


def passing_gates() -> list[quality_model.GateRun]:
    return [gate(key) for key in quality_model.GATE_ORDER]


def analysis(
    gates: list[quality_model.GateRun] | None = None,
    *,
    root: str = "/repo",
    mode: str = quality_model.FULL,
    notes: list[str] | None = None,
) -> quality_model.Analysis:
    return quality_model.Analysis(
        root=root,
        generated_at="2026-11-11 10:00",
        scope="local changes",
        mode=mode,
        gates=passing_gates() if gates is None else gates,
        version="7.0.0",
        notes=list(notes or []),
        rerun_command="python3 quality_loop.py --root . --local-changes --fast",
        full_rerun_command="python3 quality_loop.py --root .",
    )


def with_gate(replacement: quality_model.GateRun) -> list[quality_model.GateRun]:
    """All ten gates passing except ``replacement``."""
    return [
        replacement if run.key == replacement.key else run for run in passing_gates()
    ]


def state(
    value: quality_model.Analysis | None = None, html: str = ""
) -> dict[str, Any]:
    return quality_model.analysis_state(value or analysis(), html)
