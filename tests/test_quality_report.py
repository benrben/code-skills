from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_state_fixture as fx  # noqa: E402
from gate_types import BLOCKED, FAIL, SKIPPED  # noqa: E402
from quality_state_fixture import quality_html, quality_model  # noqa: E402
from quality_state_fixture import quality_report as report  # noqa: E402


def failing_state(*runs, **options):
    gates = fx.passing_gates()
    for run in runs:
        gates = [run if gate.key == run.key else gate for gate in gates]
    return fx.state(fx.analysis(gates, **options))


def complexity_items(count, prefix="app"):
    return [fx.item(path=f"{prefix}{n}.py", name=f"f{n}") for n in range(count)]


class PassingReportTests(unittest.TestCase):
    def test_green_full_run_hands_off(self):
        lines = report.report_lines(fx.state())
        self.assertEqual(lines[0], "QUALITY REPORT · full · local changes")
        self.assertEqual(lines[1], "[PASS] No shortcuts: ok")
        self.assertEqual(lines[10], "[PASS] Vulnerable dependencies: ok")
        self.assertIn("Coverage today: not measured in this run.", lines)
        self.assertIn(report.HAND_OFF_LINE, lines)
        self.assertNotIn(quality_html.NOT_PASSED, lines)
        self.assertEqual(
            lines[-4:],
            [
                "QUALITY_LOOP=PASS",
                "ITEMS_TO_FIX=0",
                "STATE=/repo/.quality/quality-gate-state.json",
                "HTML=/repo/.quality/quality-gate-report.html",
            ],
        )

    def test_selected_and_ready_runs_point_at_the_full_run(self):
        partial = fx.state(fx.analysis(mode=quality_model.PARTIAL))
        lines = report.report_lines(partial)
        self.assertIn(report.SELECTED_LINE, lines)
        self.assertIn("  python3 quality_loop.py --root .", lines)
        fast = report.report_lines(fx.state(fx.analysis(mode=quality_model.FAST)))
        self.assertIn(report.COMMIT_COMMAND, fast)
        self.assertIn("QUALITY_LOOP=READY_FOR_FULL", fast)

    def test_notes_and_html_location_are_reported(self):
        value = fx.analysis(notes=["ignored removed gate gherkin"])
        lines = report.report_lines(fx.state(value, html="/out/report.html"))
        self.assertIn("Note: ignored removed gate gherkin", lines)
        self.assertIn("STATE=/out/quality-gate-state.json", lines)
        self.assertIn("HTML=/out/report.html", lines)


class FailingReportTests(unittest.TestCase):
    def test_failing_gate_lists_issues_without_hints(self):
        broken = fx.gate("complexity", FAIL, "1 function", items=[fx.item()])
        lines = report.report_lines(failing_state(broken))
        text = "\n".join(lines)
        self.assertIn("[FAIL] Complexity: 1 function", lines)
        self.assertIn("  app.py:3 total: cyclomatic 12 (limit 10)", lines)
        self.assertIn(quality_html.NOT_PASSED, lines)
        self.assertIn("  1. app.py:3 total: cyclomatic 12 (limit 10)", lines)
        self.assertNotIn("hint never shown", text)
        self.assertNotIn("repair prompt", text)
        next_line = next(line for line in lines if line.startswith("Next file: "))
        self.assertTrue(next_line.endswith("quality_items.py --root /repo --next"))
        self.assertIn(report.RERUN_LINE, lines)
        self.assertIn("QUALITY_LOOP=FAIL", lines)
        self.assertIn("ITEMS_TO_FIX=1", lines)

    def test_gate_without_items_becomes_one_repository_issue(self):
        blocked = fx.gate(
            "tests",
            BLOCKED,
            "no test summary",
            details=["runner said nothing\nsecond line", "   ", "configure test.junit"],
        )
        lines = report.report_lines(failing_state(blocked))
        self.assertIn("[BLOCKED] Tests: no test summary", lines)
        self.assertIn("  runner said nothing", lines)
        self.assertNotIn("second line", "\n".join(lines))
        self.assertIn("  1. Tests: no test summary", lines)
        self.assertIn("ITEMS_TO_FIX=1", lines)

    def test_long_lists_are_cut_and_grouped_by_file(self):
        items = complexity_items(14) + [fx.item(path="app0.py", name="g")]
        broken = fx.gate("complexity", FAIL, "many", items=items)
        lines = report.report_lines(failing_state(broken))
        self.assertIn("  … and 7 more", lines)
        self.assertIn("  15 issues in 14 files — one file per cycle, top first", lines)
        self.assertIn("  1. app0.py — 2 issues (2 cyclomatic)", lines)
        self.assertIn("  2. app1.py — 1 issue (1 cyclomatic)", lines)
        self.assertIn(f"  … and 2 more files ({report.MORE_FILES_NOTE})", lines)

    def test_custom_state_location_is_passed_to_the_items_command(self):
        broken = fx.gate("secrets", FAIL, items=[fx.item(line=None, name="")])
        state = failing_state(broken)
        state["state_path"] = "/elsewhere/state file.json"
        lines = report.report_lines(state)
        self.assertIn("  1. app.py: cyclomatic 12 (limit 10)", lines)
        self.assertIn("STATE=/elsewhere/state file.json", lines)
        command = next(line for line in lines if line.startswith("Next file: "))
        self.assertIn("--state '/elsewhere/state file.json' --next", command)

    def test_error_state_without_commands(self):
        state = failing_state(fx.gate("tests", BLOCKED, "runner error"))
        state["status"] = "error"
        state["rerun_command"] = ""
        lines = report.report_lines(state)
        self.assertNotIn(report.RERUN_LINE, lines)
        self.assertIn("QUALITY_LOOP=ERROR", lines)
        del state["status"]
        self.assertIn("QUALITY_LOOP=ERROR", report.report_lines(state))


class SinceLastRunTests(unittest.TestCase):
    def test_counts_fixed_remaining_and_new_issues(self):
        before = complexity_items(3)
        after = before[1:] + [fx.item(path="new.py", name="h")]
        previous = failing_state(fx.gate("complexity", FAIL, items=before))
        current = failing_state(fx.gate("complexity", FAIL, items=after))
        lines = report.report_lines(current, previous)
        self.assertIn("Since last run: fixed 1 · remaining 3 · new 1", lines)

    def test_line_moves_do_not_count_for_named_issues(self):
        previous = failing_state(fx.gate("complexity", FAIL, items=[fx.item()]))
        moved = fx.gate("complexity", FAIL, items=[fx.item(line=40)])
        lines = report.report_lines(failing_state(moved), previous)
        self.assertIn("Since last run: fixed 0 · remaining 1 · new 0", lines)

    def test_legacy_or_missing_previous_state_prints_no_delta(self):
        lines = report.report_lines(fx.state(), {"failures": {}})
        self.assertFalse(any(line.startswith("Since last run") for line in lines))
        self.assertIsNone(report.delta_line([], None))


class CoverageTodayTests(unittest.TestCase):
    def coverage_state(self, measurements, status="pass"):
        return failing_state(fx.gate("coverage", status, measurements=measurements))

    def test_measured_lines_and_branches(self):
        state = self.coverage_state(
            {
                "line_coverage_percent": 92.5,
                "covered_lines": 37,
                "total_lines": 40,
                "branch_coverage_percent": 80,
                "covered_branches": 8,
                "total_branches": 10,
            }
        )
        self.assertEqual(
            report.coverage_today_line(state, None),
            "Coverage today: 92.5% of lines (37/40), 80% of branches (8/10)",
        )

    def test_lines_only_and_unmeasured(self):
        state = self.coverage_state({"line_coverage_percent": 100})
        self.assertEqual(
            report.coverage_today_line(state, None),
            "Coverage today: 100% of lines (0/0)",
        )
        blocked = self.coverage_state({"line_coverage_percent": None}, BLOCKED)
        self.assertIsNone(report.coverage_measurement(blocked))

    def test_previous_measurement_is_repeated_when_coverage_did_not_run(self):
        previous = self.coverage_state({"line_coverage_percent": 91})
        skipped = self.coverage_state({}, SKIPPED)
        self.assertEqual(
            report.coverage_today_line(skipped, previous),
            "Coverage today: not measured in this run — last measured "
            "2026-11-11 10:00: 91% of lines (0/0)",
        )
        self.assertEqual(
            report.coverage_today_line(skipped, fx.state()),
            "Coverage today: not measured in this run.",
        )


class IssueTests(unittest.TestCase):
    def test_issue_text_and_identity(self):
        issue = report.item_issue("secrets", {"path": "a.env", "line": 0})
        self.assertIsNone(issue.line)
        self.assertEqual(issue.text(), "a.env: ")
        self.assertEqual(issue.key, ("secrets", "a.env", "", "", ""))
        unnamed = report.item_issue("shortcuts", {"path": "a.py", "line": 7})
        self.assertEqual(unnamed.key[-1], "7")
        self.assertEqual(report.item_issue("x", {}).path, report.REPOSITORY)

    def test_gate_failed(self):
        self.assertFalse(report.gate_failed({"status": "pass"}))
        self.assertFalse(report.gate_failed({"status": "deferred"}))
        self.assertTrue(report.gate_failed({"status": "blocked"}))
        self.assertEqual(report.gate_issues({"status": "not_applicable"}), [])


class OutputTests(unittest.TestCase):
    def test_print_report_writes_to_the_stream_or_stdout(self):
        stream = io.StringIO()
        report.print_report(fx.state(), stream=stream)
        self.assertTrue(stream.getvalue().startswith("QUALITY REPORT · full"))
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            report.print_report(fx.state(), previous_state=fx.state())
        self.assertIn(
            "Since last run: fixed 0 · remaining 0 · new 0", captured.getvalue()
        )

    def test_render_html_uses_the_saved_status(self):
        state = failing_state(fx.gate("tests", FAIL, "2 failed"))
        page = report.render_html(state)
        self.assertIn("<h1>FAIL</h1>", page)
        state["status"] = "ready_for_full"
        self.assertIn("<h1>READY FOR FULL</h1>", report.render_html(state))
        with self.assertRaises(ValueError):
            report.render_html({"report": {}})

    def test_header_defaults(self):
        self.assertEqual(
            report.header_line({}), "QUALITY REPORT · full · whole repository"
        )
        self.assertEqual(report.first_line(""), "")


if __name__ == "__main__":
    unittest.main()
