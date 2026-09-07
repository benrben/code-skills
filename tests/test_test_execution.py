import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "skills/code-discipline/scripts/repo_quality_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("test_execution_gate_test", GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


class TestExecutionParsingTests(unittest.TestCase):
    def assert_counts(self, output, *, executed, passed, failed=0, skipped=0, parser):
        measurement = gate.parse_test_execution(output)
        self.assertTrue(measurement.measured)
        self.assertEqual(measurement.executed, executed)
        self.assertEqual(measurement.passed, passed)
        self.assertEqual(measurement.failed, failed)
        self.assertEqual(measurement.skipped, skipped)
        self.assertEqual(measurement.parser, parser)

    def test_unittest_summary(self):
        self.assert_counts(
            "Ran 17 tests in 0.123s\n\nOK (skipped=2)\n",
            executed=17,
            passed=15,
            skipped=2,
            parser="unittest",
        )

    def test_pytest_summary(self):
        self.assert_counts(
            "================ 12 passed, 2 skipped, 1 xfailed in 1.2s ================",
            executed=15,
            passed=12,
            skipped=3,
            parser="pytest",
        )

    def test_vitest_summary(self):
        self.assert_counts(
            "Tests  2 failed | 10 passed | 1 skipped (13)",
            executed=13,
            passed=10,
            failed=2,
            skipped=1,
            parser="vitest",
        )

    def test_jest_summary(self):
        self.assert_counts(
            "Tests:       1 skipped, 4 passed, 5 total",
            executed=5,
            passed=4,
            skipped=1,
            parser="jest",
        )

    def test_cargo_sums_test_binaries(self):
        self.assert_counts(
            "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured\n"
            "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured\n",
            executed=6,
            passed=5,
            skipped=1,
            parser="cargo",
        )

    def test_go_json_counts_terminal_test_events_once(self):
        self.assert_counts(
            "noise\n"
            "[]\n"
            '{"Action":"run","Package":"example","Test":"TestOne"}\n'
            '{"Action":"pass","Package":"example","Test":"TestOne"}\n'
            '{"Action":"pass","Package":"example","Test":"TestOne"}\n'
            '{"Action":"fail","Package":"example","Test":"TestTwo"}\n'
            '{"Action":"skip","Package":"example","Test":"TestThree"}\n'
            '{"Action":"pass","Package":"example"}\n',
            executed=3,
            passed=1,
            failed=1,
            skipped=1,
            parser="go-json",
        )

    def test_unknown_output_is_explicitly_unmeasured(self):
        measurement = gate.parse_test_execution("everything seems okay")
        self.assertFalse(measurement.measured)
        self.assertEqual(measurement.parser, "unknown")


class TestExecutionGateTests(unittest.TestCase):
    def result(self, stdout, returncode=0):
        return gate.CommandResult(["test"], returncode, stdout, 0.2)

    def test_success_with_no_count_is_blocked(self):
        result, measurement = gate.run_test_execution_gate(
            self.result("success"), {"min_executed": 1, "max_skipped": 0}
        )
        self.assertTrue(result.blocked)
        self.assertFalse(result.passed)
        self.assertFalse(measurement.measured)

    def test_thresholds_are_enforced(self):
        result, measurement = gate.run_test_execution_gate(
            self.result("Ran 3 tests in 0.1s\nOK (skipped=1)"),
            {"min_executed": 4, "max_skipped": 0},
        )
        self.assertFalse(result.passed)
        self.assertEqual(measurement.executed, 3)
        self.assertEqual(len(result.details), 2)

    def test_project_execution_environment_disables_common_restores(self):
        environment = gate.project_execution_env()
        self.assertEqual(environment["UV_NO_SYNC"], "1")
        self.assertEqual(environment["UV_OFFLINE"], "1")
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["BUNDLE_FROZEN"], "true")
        self.assertEqual(environment["DOTNET_NOLOGO"], "1")

    def test_baseline_uses_no_restore_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = gate.default_config()
            config["test"]["command"] = ["python", "-m", "unittest"]
            expected = self.result("Ran 1 test in 0.1s\nOK")
            with mock.patch.object(gate, "run_command", return_value=expected) as run:
                gate.run_test_baseline(root, config)
            self.assertEqual(run.call_args.args[3]["UV_NO_SYNC"], "1")

    def test_dependency_failure_is_classified_as_blocked(self):
        baseline = self.result("ModuleNotFoundError: No module named 'example'", 1)
        result = gate.tests_only_gate(["python", "-m", "unittest"], baseline)
        self.assertTrue(result.blocked)
        self.assertIn("dependencies", result.summary.lower())

    def test_dependency_restore_side_effect_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = gate.default_config()
            config["test"]["command"] = ["test"]

            def restore(*_args, **_kwargs):
                marker = root / "node_modules/.package-lock.json"
                marker.parent.mkdir()
                marker.write_text("{}", encoding="utf-8")
                return self.result("Ran 1 test in 0.1s\nOK")

            with mock.patch.object(gate, "run_command", side_effect=restore):
                _, baseline = gate.run_test_baseline(root, config)
            self.assertEqual(
                baseline.dependency_changes, ["node_modules/.package-lock.json"]
            )
            result = gate.tests_only_gate(["test"], baseline)
            self.assertFalse(result.passed)
            self.assertIn("dependency state", result.summary)


if __name__ == "__main__":
    unittest.main()
