import ast
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skills/code-discipline/scripts"
GATE_PATH = SCRIPT_DIR / "repo_quality_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "standalone_portability_gate", GATE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


class StandalonePortabilityTests(unittest.TestCase):
    def test_all_helper_modules_bundle_into_a_runnable_single_file(self):
        payload = gate.bundle_standalone_charts(
            GATE_PATH.read_bytes(), (SCRIPT_DIR / "quality_charts.py").read_bytes()
        )
        payload = gate.bundle_standalone_portable_analysis(
            payload, (SCRIPT_DIR / "portable_analysis.py").read_bytes()
        )
        payload = gate.bundle_standalone_portable_graph(
            payload, (SCRIPT_DIR / "portable_graph.py").read_bytes()
        )
        payload = gate.bundle_standalone_vulnerability_analysis(
            payload, (SCRIPT_DIR / "portable_vulnerabilities.py").read_bytes()
        )
        payload = gate.bundle_standalone_project_quality(
            payload, (SCRIPT_DIR / "project_quality.py").read_bytes()
        )
        payload = gate.bundle_standalone_gherkin(
            payload, (SCRIPT_DIR / "gherkin_check.py").read_bytes()
        )
        payload = gate.bundle_standalone_gherkin_yaml(
            payload, (SCRIPT_DIR / "gherkin_yaml.py").read_bytes()
        )
        source = payload.decode("utf-8")
        tree = ast.parse(source)
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("quality_charts", imported_modules)
        self.assertNotIn("portable_analysis", imported_modules)
        self.assertNotIn("portable_graph", imported_modules)
        self.assertNotIn("portable_vulnerabilities", imported_modules)
        self.assertNotIn("project_quality", imported_modules)
        self.assertNotIn("gherkin_check", imported_modules)
        self.assertNotIn("gherkin_yaml", imported_modules)

        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "repo_quality_gate.py"
            script.write_bytes(payload)
            result = subprocess.run(
                [sys.executable, str(script), "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            source = Path(temporary) / "example.feature.yaml"
            source.write_text(
                "feature: Example\nscenarios:\n  - name: One\n    steps:\n      - then: the command succeeds\n"
            )
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--root",
                    temporary,
                    "--prepare-gherkin",
                    "--gherkin-feature",
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            self.assertIn(
                "Then the command succeeds", source.with_suffix("").read_text()
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(gate.VERSION, result.stdout)


if __name__ == "__main__":
    unittest.main()
