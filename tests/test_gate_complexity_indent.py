"""Nesting from indentation for languages lizard's counter treats as flat."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_complexity_indent as indent  # noqa: E402


def body(text: str) -> list[str]:
    return text.strip("\n").split("\n")


class MeasureTests(unittest.TestCase):
    def test_flat_body_has_no_nesting(self):
        lines = body("  total = 0\n  total = total + 1\n  return total")
        self.assertEqual(indent.measure_indent_nesting(lines), (0, 0))

    def test_empty_body_has_no_nesting(self):
        self.assertEqual(indent.measure_indent_nesting(["", "   "]), (0, 0))

    def test_one_top_level_structure_is_depth_one(self):
        lines = body("  if ok then\n    run()\n  end")
        self.assertEqual(indent.measure_indent_nesting(lines), (1, 0))

    def test_nested_structures_add_their_depths(self):
        lines = body(
            """
    for i = 1, 3 do
        while busy do
            if ready then
                go()
            end
        end
    end
"""
        )
        self.assertEqual(indent.measure_indent_nesting(lines), (3, 0 + 1 + 2))

    def test_branch_continuations_and_data_lines_add_nothing(self):
        lines = body(
            """
  if a
    x = {
          deep: {
                  deeper: 1
                }
        }
  elsif b
    y
  else
    z
  end
"""
        )
        self.assertEqual(indent.measure_indent_nesting(lines), (1, 0))

    def test_keywords_match_case_insensitively_and_comments_do_not(self):
        lines = body(
            """
  IF a THEN
    -- if this were code it would nest
    FOR i IN 1..3 LOOP
      NULL;
    END LOOP;
  END IF;
"""
        )
        self.assertEqual(indent.measure_indent_nesting(lines), (2, 1))

    def test_a_modifier_keyword_after_code_is_not_a_structure(self):
        lines = body("  run if ready\n  stop unless busy")
        self.assertEqual(indent.measure_indent_nesting(lines), (0, 0))

    def test_tabs_count_as_one_level(self):
        lines = ["\tif a", "\t\tif b", "\t\t\tgo()", "\t\tend", "\tend"]
        self.assertEqual(indent.measure_indent_nesting(lines), (2, 1))


class UnitTests(unittest.TestCase):
    def test_most_common_step_wins_over_aligned_continuations(self):
        self.assertEqual(indent.indent_unit([0, 2, 4, 2, 4, 6, 13, 4]), 2)

    def test_ties_choose_the_smaller_step(self):
        self.assertEqual(indent.indent_unit([0, 4, 0, 2]), 2)

    def test_no_step_means_unit_one(self):
        self.assertEqual(indent.indent_unit([4, 4, 2]), 1)

    def test_indent_width_expands_tabs(self):
        self.assertEqual(indent.indent_width("\t  x"), 6)

    def test_function_body_skips_header_and_closing_line(self):
        lines = ["def f", "  a", "  b", "end"]
        self.assertEqual(indent.function_body(lines, 1, 4), ["  a", "  b"])
        self.assertEqual(indent.function_body(lines, 1, 1), [])


if __name__ == "__main__":
    unittest.main()
