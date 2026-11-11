import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_types  # noqa: E402


class GateTypesTests(unittest.TestCase):
    def test_item_location_and_description(self):
        item = gate_types.GateItem("a.py", 3, "run", "complexity", "12", "10")
        self.assertEqual(item.location(), "a.py:3")
        self.assertEqual(item.describe(), "a.py:3 run: complexity 12 (limit 10)")
        bare = gate_types.GateItem("a.py", None, "", "lines", "700", "600")
        self.assertEqual(bare.describe(), "a.py: lines 700 (limit 600)")

    def test_outcome_status_properties(self):
        for status, passed, blocked in (
            (gate_types.PASS, True, False),
            (gate_types.NOT_APPLICABLE, True, False),
            (gate_types.FAIL, False, False),
            (gate_types.BLOCKED, False, True),
        ):
            outcome = gate_types.GateOutcome("k", "Title", status, "summary")
            self.assertEqual(outcome.passed, passed)
            self.assertEqual(outcome.blocked, blocked)

    def test_outcome_for_items(self):
        item = gate_types.GateItem("a.py", 1, "f", "m", "2", "1")
        failed = gate_types.outcome_for_items("k", "T", [item], "ok", "bad")
        self.assertEqual(failed.status, gate_types.FAIL)
        self.assertEqual(failed.summary, "bad")
        self.assertEqual(failed.details, [item.describe()])
        passed = gate_types.outcome_for_items(
            "k", "T", [], "ok", "bad", measurements={"n": 1}
        )
        self.assertEqual(passed.status, gate_types.PASS)
        self.assertEqual(passed.measurements, {"n": 1})


class FunctionInfoTests(unittest.TestCase):
    def test_contains_and_bare_name(self):
        info = gate_types.FunctionInfo("a.cpp", "Cart::total", "Cart::total()", 3, 9)
        self.assertTrue(info.contains(3))
        self.assertTrue(info.contains(9))
        self.assertFalse(info.contains(10))
        self.assertEqual(info.bare_name, "total")
        dotted = gate_types.FunctionInfo("a.py", "Cart.total", "Cart.total", 1, 2)
        self.assertEqual(dotted.bare_name, "total")
        plain = gate_types.FunctionInfo("a.py", "run", "run", 1, 2)
        self.assertEqual(plain.bare_name, "run")
        self.assertEqual(plain.cyclomatic, 1)


if __name__ == "__main__":
    unittest.main()
