import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "skills/code-discipline/scripts/repo_quality_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("portability_gate_test", GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


class NoProjectRestoreTests(unittest.TestCase):
    def inferred_command(self, filename, contents, executable):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / filename).write_text(contents, encoding="utf-8")
            with mock.patch.object(
                gate, "executable", side_effect=lambda _root, name: executable.get(name)
            ):
                return gate.infer_test_command(root)

    def test_cargo_is_locked_and_offline(self):
        command = self.inferred_command(
            "Cargo.toml",
            "[package]\nname='sample'\nversion='1.0.0'\n",
            {"cargo": "/bin/cargo"},
        )
        self.assertEqual(command, ["/bin/cargo", "test", "--all", "--frozen"])

    def test_maven_is_offline(self):
        command = self.inferred_command("pom.xml", "<project/>", {"mvn": "/bin/mvn"})
        self.assertEqual(command, ["/bin/mvn", "-o", "test"])

    def test_gradle_wrapper_is_offline(self):
        command = self.inferred_command("gradlew", "#!/bin/sh\n", {})
        self.assertEqual(command, [str(Path(command[0])), "--offline", "test"])

    def test_dotnet_does_not_restore(self):
        command = self.inferred_command("sample.sln", "", {"dotnet": "/bin/dotnet"})
        self.assertEqual(command, ["/bin/dotnet", "test", "--no-restore"])

    def test_go_emits_machine_readable_native_test_counts(self):
        command = self.inferred_command(
            "go.mod", "module example.com/sample\n", {"go": "/bin/go"}
        )
        self.assertEqual(command, ["/bin/go", "test", "-json", "./..."])

    def test_bootstrap_never_runs_project_package_installers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"devDependencies": {"vitest": "1.0.0"}}), encoding="utf-8"
            )
            source = root / "app.ts"
            source.write_text(
                "export function value() { return 1 }\n", encoding="utf-8"
            )
            config = gate.default_config()
            with (
                mock.patch.object(gate, "run_command") as run,
                mock.patch.object(gate, "executable", return_value="/bin/npm"),
            ):
                gate.bootstrap_tools(root, config, [source])
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(
                any(command[:2] == ["/bin/npm", "install"] for command in commands),
                commands,
            )


class EvidenceStatusTests(unittest.TestCase):
    def test_blocked_and_unsupported_are_distinct_failures(self):
        blocked = gate.GateResult(
            "tests", "Tests", False, "project dependencies are missing", blocked=True
        )
        unsupported = gate.GateResult(
            "analysis", "Analysis", False, "language parser missing", unsupported=True
        )
        self.assertEqual(gate.gate_status(blocked), "blocked")
        self.assertEqual(gate.gate_outcome(blocked), "BLOCKED")
        self.assertEqual(gate.gate_status(unsupported), "unsupported")
        self.assertEqual(gate.gate_outcome(unsupported), "UNSUPPORTED")

    def test_needs_context_is_distinct_and_does_not_pass(self):
        result = gate.GateResult(
            "architecture",
            "Architecture",
            False,
            "the intended boundaries are unknown",
            needs_context=True,
        )
        self.assertEqual(gate.gate_status(result), "needs_context")
        self.assertEqual(gate.gate_outcome(result), "NEEDS CONTEXT")
        report = gate.AnalysisReport(
            root=".",
            generated_at="now",
            languages=[],
            gates=[result],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
        )
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
