"""A small Python git repository for quality-loop tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CALC = '''"""Tiny calculator."""


def add(a: int, b: int) -> int:
    if a < 0:
        return -add(-a, -b)
    return a + b
'''

TESTS = """import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(1, 2), 3)

    def test_negative(self) -> None:
        self.assertEqual(add(-1, -2), -3)
"""

UNUSED = """

def _unused(x: int) -> int:
    return x * 2
"""


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


def make_python_repository(root: Path) -> Path:
    """A committed repository whose ten gates pass."""
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(TESTS, encoding="utf-8")
    (root / ".gitignore").write_text(".quality/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def add_unused_function(root: Path) -> None:
    """An uncommitted change that fails coverage and dead code."""
    with (root / "calc.py").open("a", encoding="utf-8") as handle:
        handle.write(UNUSED)


def write_test_config(root: Path) -> None:
    """Pin the test command to this interpreter (the inferred ``python3`` may
    be another Python without coverage.py)."""
    config = {"test": {"command": [sys.executable, "-m", "unittest", "discover"]}}
    path = root / ".quality" / "quality-gate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")
