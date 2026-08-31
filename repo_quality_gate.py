#!/usr/bin/env python3
"""Compatibility entrypoint for the self-contained code-discipline skill."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


CORE_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "code-discipline"
    / "scripts"
    / "repo_quality_gate.py"
)


def load_core() -> ModuleType:
    spec = importlib.util.spec_from_file_location("code_discipline_quality_gate", CORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load code-discipline quality engine: {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_core = load_core()

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


if __name__ == "__main__":
    raise SystemExit(_core.main())
