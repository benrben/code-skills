import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "skills/code-discipline/scripts/repo_quality_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "portable_gate_integration", GATE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()
vulnerability_scanner = sys.modules[gate.DependencyInventory.__module__]


class PortableGateIntegrationTests(unittest.TestCase):
    def thresholds(self):
        return {
            "max_cognitive_complexity": 2,
            "max_nesting_depth": 1,
            "max_duplication_percent": 5,
            "max_dependency_cycles": 0,
            "max_secret_findings": 0,
        }

    def test_source_only_gates_find_portable_violations_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = "ghp_" + "a" * 36
            first = root / "a.py"
            second = root / "b.py"
            first.write_text(
                "import b\n"
                "def choose(a, b):\n"
                "    if a:\n"
                "        if b:\n"
                "            return 1\n"
                "        return 2\n"
                "    return 0\n"
                f"TOKEN = '{token}'\n",
                encoding="utf-8",
            )
            second.write_text(
                "import a\n"
                "def choose(a, b):\n"
                "    if a:\n"
                "        if b:\n"
                "            return 1\n"
                "        return 2\n"
                "    return 0\n",
                encoding="utf-8",
            )

            results, report = gate.run_portable_source_gates(
                root, [first, second], self.thresholds(), include_history=False
            )

            by_key = {result.key: result for result in results}
            self.assertFalse(by_key["cognitive"].passed)
            self.assertFalse(by_key["duplication"].passed)
            self.assertFalse(by_key["dependency_health"].passed)
            self.assertFalse(by_key["secrets"].passed)
            self.assertNotIn(token, repr(results))
            self.assertEqual(len(report.dependencies.cycles), 1)
            self.assertEqual(len(report.secrets), 1)

    def test_unsupported_complexity_is_explicit_while_generic_checks_still_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "main.go"
            source.write_text("package main\nfunc main() {}\n", encoding="utf-8")

            results, _ = gate.run_portable_source_gates(
                root, [source], self.thresholds(), include_history=False
            )

            by_key = {result.key: result for result in results}
            self.assertTrue(by_key["cognitive"].unsupported)
            self.assertTrue(by_key["duplication"].passed)
            self.assertTrue(by_key["dependency_health"].passed)
            self.assertTrue(by_key["secrets"].passed)
            self.assertFalse(by_key["hotspots"].applicable)

    def test_git_numstat_history_is_aggregated_per_file_and_commit(self):
        output = (
            "commit:one\n3\t1\ta.py\n2\t0\tb.py\n"
            "commit:two\n5\t4\ta.py\n-\t-\timage.bin\n"
        )
        history = gate.parse_git_history(output, {"a.py", "b.py"})
        by_path = {item.path: item for item in history}
        self.assertEqual(
            (
                by_path["a.py"].commit_count,
                by_path["a.py"].lines_added,
                by_path["a.py"].lines_deleted,
            ),
            (2, 8, 5),
        )
        self.assertEqual(by_path["b.py"].commit_count, 1)

    def test_vulnerability_gate_distinguishes_findings_and_network_blockers(self):
        package = vulnerability_scanner.LockedPackage(
            "npm", "example", "1.0.0", "package-lock.json"
        )
        inventory = gate.DependencyInventory((package,), ("package-lock.json",), ())
        finding = gate.VulnerabilityFinding("GHSA-example", package)
        with (
            mock.patch.object(gate, "discover_locked_packages", return_value=inventory),
            mock.patch.object(gate, "query_osv", return_value=(finding,)),
        ):
            result, measured_inventory, findings = gate.run_vulnerability_gate(
                Path("."), {"max_known_vulnerabilities": 0}
            )
        self.assertFalse(result.passed)
        self.assertEqual(findings, (finding,))
        self.assertEqual(measured_inventory, inventory)

        with (
            mock.patch.object(gate, "discover_locked_packages", return_value=inventory),
            mock.patch.object(gate, "query_osv", side_effect=TimeoutError("offline")),
        ):
            blocked, _, _ = gate.run_vulnerability_gate(
                Path("."), {"max_known_vulnerabilities": 0}
            )
        self.assertTrue(blocked.blocked)


if __name__ == "__main__":
    unittest.main()
