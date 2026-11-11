import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_state_fixture as fx  # noqa: E402
from gate_types import (  # noqa: E402
    BLOCKED,
    DEFERRED,
    FAIL,
    NOT_APPLICABLE,
    OFF,
    PASS,
    SKIPPED,
    GateOutcome,
)
from quality_state_fixture import quality_model  # noqa: E402


class GateRunTests(unittest.TestCase):
    def test_from_outcome_blocks_not_applicable_except_for_two_gates(self):
        outcome = GateOutcome("tests", "Tests", NOT_APPLICABLE, "nothing")
        run = quality_model.GateRun.from_outcome(outcome, 1.5)
        self.assertEqual(run.status, BLOCKED)
        self.assertEqual(run.seconds, 1.5)
        checks = GateOutcome("project_checks", "Project checks", NOT_APPLICABLE, "-")
        kept = quality_model.GateRun.from_outcome(checks, None)
        self.assertEqual(kept.status, NOT_APPLICABLE)
        self.assertTrue(kept.passed)

    def test_not_run_gates_did_not_run(self):
        run = quality_model.GateRun.not_run("coverage", SKIPPED, "not selected")
        self.assertEqual(run.title, "Coverage")
        self.assertFalse(run.ran)
        self.assertTrue(fx.gate("tests").ran)
        self.assertEqual(quality_model.GateRun.not_run("x", OFF, "-").title, "x")


class AnalysisStatusTests(unittest.TestCase):
    def test_full_run_with_every_gate_passing_passes(self):
        self.assertEqual(fx.analysis().status, "pass")

    def test_deferred_gate_or_nothing_ran_never_certifies(self):
        deferred = fx.with_gate(fx.gate("vulnerabilities", DEFERRED))
        self.assertEqual(fx.analysis(deferred).status, "fail")
        nothing = [fx.gate("tests", SKIPPED)]
        self.assertEqual(fx.analysis(nothing).status, "fail")

    def test_partial_and_fast_runs(self):
        partial = fx.analysis(mode=quality_model.PARTIAL)
        self.assertEqual(partial.status, "selected_pass")
        fast = fx.analysis(mode=quality_model.FAST)
        self.assertEqual(fast.status, "ready_for_full")
        failing = fx.analysis(fx.with_gate(fx.gate("tests", FAIL)), mode="fast")
        self.assertEqual(failing.status, "fail")

    def test_issues_come_only_from_failing_gates(self):
        broken = fx.gate("complexity", FAIL, items=[fx.item()])
        gates = fx.with_gate(broken)
        gates[0] = fx.gate("shortcuts", PASS, items=[fx.item(name="ignored")])
        value = fx.analysis(gates)
        self.assertEqual([gate.key for gate in value.failing_gates], ["complexity"])
        self.assertEqual(value.issues(), [("complexity", fx.item())])


class StateTests(unittest.TestCase):
    def test_state_round_trips(self):
        broken = fx.gate(
            "coverage",
            FAIL,
            items=[fx.item()],
            measurements={"files": {"b", "a"}, "path": Path("x"), "n": (1, None)},
        )
        state = fx.state(fx.analysis(fx.with_gate(broken)), "/repo/r.html")
        self.assertIsInstance(json.dumps(state), str)
        self.assertEqual(state["issues"][0]["gate"], "coverage")
        coverage = state["gates"][5]
        self.assertEqual(coverage["measurements"]["files"], ["a", "b"])
        self.assertEqual(coverage["measurements"]["path"], "x")
        self.assertEqual(coverage["measurements"]["n"], [1, None])
        restored = quality_model.analysis_from_state(state)
        self.assertEqual(restored.status, "fail")
        self.assertEqual(restored.gates[5].items, broken.items)
        self.assertEqual(restored.gates[5].seconds, 0.5)
        self.assertEqual(restored.rerun_command, state["rerun_command"])

    def test_older_state_is_refused(self):
        with self.assertRaisesRegex(ValueError, "older quality loop"):
            quality_model.analysis_from_state({"schema_version": 1})

    def test_sparse_records_get_defaults_and_unknown_status_blocks(self):
        state = {
            "schema_version": 2,
            "gates": [{"key": "tests", "status": "weird", "items": [{}]}, {}],
        }
        restored = quality_model.analysis_from_state(state)
        self.assertEqual(restored.mode, quality_model.FULL)
        self.assertEqual(restored.gates[0].status, BLOCKED)
        self.assertEqual(restored.gates[0].items[0].path, "")
        self.assertEqual(restored.gates[1].measurements, {})
        self.assertEqual(restored.baselines, {})

    def test_gate_rows_follow_gate_order_with_unknown_keys_last(self):
        gates = [fx.gate("other"), fx.gate("vulnerabilities"), fx.gate("shortcuts")]
        rows = quality_model.gate_rows(fx.analysis(gates))
        self.assertEqual(
            [row.key for row in rows], ["shortcuts", "vulnerabilities", "other"]
        )


class FileTests(unittest.TestCase):
    def test_read_state_returns_none_for_missing_damaged_or_non_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            self.assertIsNone(quality_model.read_state(path))
            path.write_text("{broken")
            self.assertIsNone(quality_model.read_state(path))
            path.write_text("[]")
            self.assertIsNone(quality_model.read_state(path))
            path.write_text('{"a": 1}')
            self.assertEqual(quality_model.read_state(path), {"a": 1})

    def test_write_json_atomic_writes_and_cleans_up_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            quality_model.write_json_atomic(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            with mock.patch.object(
                quality_model.json, "dump", side_effect=OSError("disk full")
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    quality_model.write_json_atomic(path, {"a": 2})
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])
            self.assertEqual(json.loads(path.read_text()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
