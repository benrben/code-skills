from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_state_fixture as fx  # noqa: E402
from gate_types import BLOCKED, FAIL, NOT_APPLICABLE, PASS  # noqa: E402
from quality_state_fixture import quality_html  # noqa: E402

CONTEXT = quality_html.ReportContext(
    repository="/repo",
    scope="local changes",
    generated_at="2026-11-11 10:00",
    verdict="FAIL",
    command="python3 quality_loop.py --root .",
    notes=("ignored removed gate <gherkin>",),
)


class RenderReportTests(unittest.TestCase):
    def test_failing_gate_lists_issues_only_and_escapes_values(self):
        broken = fx.gate(
            "complexity",
            FAIL,
            "1 function over the limits",
            items=[fx.item(name="<total>")],
            details=[fx.item(name="<total>").describe(), "extra <detail>"],
        )
        page = quality_html.render_report(CONTEXT, [broken, fx.gate("tests")])
        self.assertIn(quality_html.NOT_PASSED, page)
        self.assertIn("&lt;total&gt;", page)
        self.assertNotIn("<total>", page)
        self.assertIn("extra &lt;detail&gt;", page)
        self.assertEqual(page.count("app.py:3"), 1)
        self.assertIn("ignored removed gate &lt;gherkin&gt;", page)
        self.assertIn("<code>python3 quality_loop.py --root .</code>", page)
        self.assertIn('class="hero fail"', page)
        self.assertIn("2 gates: 1 PASS, 1 FAIL.", page)

    def test_long_issue_lists_are_cut_with_a_count(self):
        items = [fx.item(line=n) for n in range(1, 206)]
        broken = fx.gate("coverage", BLOCKED, "blocked", items=items)
        page = quality_html.render_report(CONTEXT, [broken])
        self.assertIn("5 more issues are in the state file.", page)
        self.assertIn("Issues (205)", page)

    def test_passing_page_has_tiles_without_notes_or_command(self):
        context = quality_html.ReportContext("/repo", "whole repository", "now", "PASS")
        rows = [
            fx.gate("coverage", measurements={"line_coverage_percent": 93.5}),
            fx.gate("tests", measurements={"executed": 12}),
            fx.gate("project_checks", NOT_APPLICABLE, "no checks declared"),
            fx.gate("secrets"),
            fx.gate("dead_code", "deferred", "later"),
            fx.gate("duplication", "mystery", "odd"),
        ]
        rows[-1].seconds = None
        page = quality_html.render_report(context, rows)
        self.assertIn('class="hero pass"', page)
        self.assertIn("93.5% of lines", page)
        self.assertIn("12 tests ran", page)
        self.assertIn('class="tile na"', page)
        self.assertIn("no issues", page)
        self.assertIn("MYSTERY", page)
        self.assertNotIn('class="notes"', page)
        self.assertNotIn('class="command"', page)

    def test_headline_counts_issues(self):
        one = fx.gate("secrets", FAIL, items=[fx.item()])
        two = fx.gate("secrets", FAIL, items=[fx.item(), fx.item(line=4)])
        self.assertEqual(quality_html._headline(one), "1 issue")
        self.assertEqual(quality_html._headline(two), "2 issues")
        self.assertEqual(quality_html._headline(fx.gate("x", BLOCKED)), "")
        self.assertEqual(quality_html._headline(fx.gate("x", PASS)), "no issues")


if __name__ == "__main__":
    unittest.main()
