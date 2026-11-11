import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_state_fixture as fx  # noqa: E402
from gate_types import BLOCKED, FAIL  # noqa: E402
from quality_state_fixture import quality_items as items  # noqa: E402

ISSUES = [
    fx.item(path="src/cart.py", line=4, name="total"),
    fx.item(path="src/cart.py", line=9, name="tax", metric="cognitive"),
    fx.item(path="src/user.py", line=2, name="load"),
]


def write_state(root: Path, runs) -> Path:
    gates = fx.passing_gates()
    for run in runs:
        gates = [run if gate.key == run.key else gate for gate in gates]
    path = root / ".quality" / "quality-gate-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(fx.state(fx.analysis(gates, root=str(root)))))
    return path


def failing_runs():
    return [
        fx.gate("complexity", FAIL, "3 functions", items=ISSUES),
        fx.gate("tests", BLOCKED, "no summary", details=["runner printed nothing"]),
    ]


class ItemsCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_items(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = items.main(["--root", str(self.root), *arguments])
        return code, output.getvalue()

    def test_next_prints_the_file_with_most_issues_without_hints(self):
        write_state(self.root, failing_runs())
        code, output = self.run_items("--next")
        self.assertEqual(code, 0)
        self.assertEqual(
            output.splitlines(),
            [
                "src/cart.py — 2 issues (1 cognitive · 1 cyclomatic)",
                "  L4     total: cyclomatic 12 (limit 10)",
                "  L9     tax: cognitive 12 (limit 10)",
                "2 more files after this one.",
            ],
        )
        self.assertNotIn("hint", output)

    def test_file_selects_by_name_and_shows_repository_details(self):
        write_state(self.root, failing_runs())
        _, output = self.run_items("--file", "user.py")
        self.assertIn("src/user.py — 1 issue (1 cyclomatic)", output)
        _, output = self.run_items("--file", "(repository)")
        self.assertIn("  —      Tests: no summary", output)
        self.assertIn("         runner printed nothing", output)
        _, output = self.run_items("--file", "missing.py")
        self.assertTrue(output.startswith("No open issues in missing.py."))
        self.assertIn("4 issues in 3 files", output)

    def test_summary_and_briefs(self):
        write_state(self.root, failing_runs())
        _, output = self.run_items("--summary")
        lines = output.splitlines()
        self.assertEqual(
            lines[1], "  1. src/cart.py — 2 issues (1 cognitive · 1 cyclomatic)"
        )
        self.assertEqual(lines[3], "  3. (repository) — 1 issue (1 Tests)")
        _, output = self.run_items("--briefs", "5")
        self.assertIn("=== brief 1 of 2: src/cart.py ===", output)
        self.assertIn("  - src/cart.py:4 total: cyclomatic 12 (limit 10)", output)
        self.assertIn(items.BRIEF_RULES, output)
        self.assertIn(items.PARENT_LINE, output)
        self.assertNotIn("(repository) ===", output)
        _, output = self.run_items("--briefs", "0")
        self.assertEqual(output.strip(), "No file-level issues to delegate.")

    def test_green_state_has_nothing_to_fix(self):
        write_state(self.root, [])
        for arguments in ([], ["--summary"]):
            self.assertEqual(
                self.run_items(*arguments), (0, items.NOTHING_TO_FIX + "\n")
            )

    def test_missing_or_legacy_state_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.run_items()[0], 2)
        self.assertIn("run the quality loop first", errors.getvalue())
        legacy = self.root / "old.json"
        legacy.write_text('{"failures": {}}')
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.run_items("--state", str(legacy))[0], 2)
        self.assertIn("older quality loop", errors.getvalue())

    def test_script_runs_from_any_directory(self):
        write_state(self.root, failing_runs())
        completed = subprocess.run(
            [sys.executable, items.__file__, "--root", str(self.root), "--summary"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("4 issues in 3 files", completed.stdout)


if __name__ == "__main__":
    unittest.main()
