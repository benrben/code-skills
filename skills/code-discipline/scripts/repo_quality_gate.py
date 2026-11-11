#!/usr/bin/env python3
"""The repository quality gate: ten generic gates over a repository or a change.

This entry point runs the quality loop (``quality_loop.py``) and adds
``--update-from-github [REF]``. The engine lives in small modules beside it:
``quality_config`` (policy files), ``quality_tools`` (pinned tools),
``quality_commands`` (running project commands), ``quality_run`` (the gates),
and ``quality_model`` (the state file).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import quality_loop
import quality_self_update
from quality_config import (
    CONFIG_NAME,
    THRESHOLDS_NAME,
    VERSION,
    default_thresholds,
    load_config,
    load_thresholds,
)
from quality_model import GATE_ORDER, analysis_state, write_json_atomic
from quality_run import CHECK_FLAGS, run_analysis, selected_gates

__all__ = [
    "CHECK_FLAGS",
    "CONFIG_NAME",
    "GATE_ORDER",
    "THRESHOLDS_NAME",
    "VERSION",
    "analysis_state",
    "default_thresholds",
    "load_config",
    "load_thresholds",
    "main",
    "run_analysis",
    "selected_gates",
    "write_json_atomic",
]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--update-from-github",
        nargs="?",
        const=quality_self_update.GITHUB_DEFAULT_REF,
        metavar="REF",
    )
    known, rest = parser.parse_known_args(arguments)
    if known.update_from_github is not None:
        runner = Path(__file__).resolve()
        return quality_self_update.update_from_github(runner, known.update_from_github)
    return quality_loop.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
