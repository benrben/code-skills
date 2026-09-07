import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gherkin_fixture import acceptance_config
from tests.test_repo_quality_gate import gate, quality_loop


class GherkinGateTests(unittest.TestCase):
    def test_required_gate_cannot_be_absent_or_disabled_and_fast_defers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = gate.default_config()
            tools = gate.ToolContext(root, sys.executable, root)

            def run(fast, selected):
                return gate.gherkin_gate_for_run(
                    root, config, fast, gate.gate_selection(selected), tools
                )

            self.assertTrue(run(False, ["lint"]).skipped)
            self.assertTrue(run(True, []).deferred)
            self.assertFalse(run(False, []).passed)
            self.assertFalse(run(True, ["gherkin"]).passed)
            config["gherkin"]["enabled"] = False
            self.assertIn("cannot be disabled", run(False, []).summary)
            config["gherkin"]["enabled"] = True
            (root / "empty.feature").write_text("Feature: Empty\n")
            self.assertIn("Configure", run(False, []).summary)

    def test_native_runner_passes_then_failure_stale_and_missing_cases_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public.py"
            public.write_text(
                "from pathlib import Path\nPath('saved.txt').write_text('hello')\nassert Path('saved.txt').read_text() == 'hello'\n"
            )
            section = acceptance_config(root, [sys.executable, "public.py"])
            features = list(root.rglob("*.feature"))
            tools = gate.ToolContext(root, sys.executable, root)
            first = gate.run_gherkin_gate(root, section, features, tools)
            self.assertTrue(first.passed, first.summary)
            self.assertIn("1 scenarios", first.summary)
            self.assertEqual((root / "saved.txt").read_text(), "hello")
            public.write_text("raise RuntimeError('cannot save')\n")
            failed = gate.run_gherkin_gate(root, section, features, tools)
            self.assertFalse(failed.passed)
            self.assertIn("runner failed", failed.summary)
            stale = {**section, "command": [sys.executable, "-c", "print('no report')"]}
            self.assertIn(
                "fresh", gate.run_gherkin_gate(root, stale, features, tools).summary
            )
            public.write_text("print('ok')\n")
            section["command"] += ["--tags", "@does_not_exist"]
            omitted = gate.run_gherkin_gate(root, section, features, tools)
            self.assertFalse(omitted.passed)
            self.assertEqual(omitted.command_results[0].returncode, 0)

    def test_changed_features_and_nonzero_exit_cannot_reuse_successful_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feature = root / "example.feature"
            feature.write_text("Feature: Before\n")
            tools = gate.ToolContext(root, sys.executable, root)
            section = {"command": ["native-runner"], "report": "report.json"}

            def write_report(*args):
                (root / "report.json").write_text("[]")
                feature.write_text("Feature: Changed\n")
                return gate.CommandResult(["native-runner"], 0, "", 0.1)

            with mock.patch.object(gate, "run_command", side_effect=write_report):
                result = gate.run_gherkin_gate(root, section, [feature], tools)
            self.assertFalse(result.passed)
            self.assertIn("changed", result.summary)
            with mock.patch.object(
                gate,
                "run_command",
                return_value=gate.CommandResult(
                    ["native-runner"], 9, "failed hook", 0.1
                ),
            ):
                result = gate.run_gherkin_gate(root, section, [feature], tools)
            self.assertFalse(result.passed)
            self.assertEqual(result.command_results[0].returncode, 9)

    def test_yaml_runs_real_behave_and_revalidates_the_authored_specification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('saved.txt').write_text('saved')",
            ]
            section = acceptance_config(root, command)
            native = root / "tests/features/public_command.feature"
            native.unlink()
            source = native.with_suffix(".feature.yaml")
            specification = """feature: Public command
scenarios:
  - name: Execute <attempt>
    steps:
      - when: I execute the public command
      - then: the command succeeds
    examples:
      - attempt: first
      - attempt: second
"""
            source.write_text(specification)
            tools = gate.ToolContext(root, sys.executable, root)
            result = gate.run_gherkin_gate(root, section, [source], tools)
            self.assertTrue(result.passed, result.summary + str(result.details))
            self.assertIn("2 scenarios", result.summary)
            self.assertEqual((root / "saved.txt").read_text(), "saved")
            self.assertEqual(source.read_text(), specification)
            self.assertEqual(gate.gherkin_source_files(root), [source])
            # Source YAML, rather than a previously generated sibling, is authoritative.
            source.write_text(
                specification.replace("the command succeeds", "the new result appears")
            )
            stale = gate.validate_gherkin_run(
                root,
                section,
                [source],
                root / section["report"],
                tools,
                result.command_results[1],
            )
            self.assertFalse(stale.passed)
            # Invalid YAML must prevent the configured application command from running.
            source.write_text("feature: broken\nscenarios: []\n")
            (root / "saved.txt").unlink()
            invalid = gate.run_gherkin_gate(root, section, [source], tools)
            self.assertFalse(invalid.passed)
            self.assertFalse((root / "saved.txt").exists())

    def test_yaml_alias_collisions_cannot_replace_existing_expected_cases(self):
        checker = sys.modules["gherkin_check"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "duplicate feature paths"):
                checker.expected_cases(
                    root, [Path("same.feature.yaml"), Path("same.feature.yml")]
                )

    def test_crap_name_and_legacy_threshold_alias_preserve_owned_value(self):
        self.assertTrue(quality_loop.parse_args(["--crap"]).crap)
        self.assertTrue(quality_loop.parse_args(["--craap"]).crap)
        self.assertEqual(
            quality_loop.selection_names(quality_loop.parse_args(["--craap"])),
            ("crap",),
        )
        self.assertEqual(
            gate.normalize_crap_config({"metrics": {"craap_limit": 4}}),
            {"metrics": {"crap_limit": 4}},
        )
        self.assertEqual(
            gate.normalize_crap_config(
                {"metrics": {"craap_limit": 4, "crap_limit": 4}}
            ),
            {"metrics": {"crap_limit": 4}},
        )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            gate.normalize_crap_config({"metrics": {"craap_limit": 3, "crap_limit": 4}})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "thresholds.json"
            thresholds = gate.default_thresholds()
            thresholds["metrics"]["craap_limit"] = thresholds["metrics"].pop(
                "crap_limit"
            )
            path.write_text(json.dumps(thresholds))
            self.assertEqual(gate.load_thresholds(path)[0]["metrics"]["crap_limit"], 6)

    def test_validation_cli_and_standalone_bundle_fail_closed(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                gate.main(
                    [
                        "--validate-gherkin-report",
                        "missing.json",
                        "--gherkin-feature",
                        "missing.feature",
                    ]
                ),
                1,
            )
        self.assertIn("FAIL", output.getvalue())
        self.assertEqual(
            gate.without_fast_flag(
                "quality --root . --local-changes --gherkin --crap --fast"
            ),
            "quality --root . --local-changes",
        )
        with self.assertRaisesRegex(ValueError, "no Gherkin import"):
            gate.bundle_standalone_gherkin(b"VERSION='1'", b"pass")
        with self.assertRaises(SyntaxError):
            gate.bundle_standalone_gherkin(Path(gate.__file__).read_bytes(), b"def (")


if __name__ == "__main__":
    unittest.main()
