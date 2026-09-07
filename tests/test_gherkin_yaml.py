import contextlib
import importlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/code-discipline/scripts/gherkin_yaml.py"
)
sys.path.insert(0, str(SCRIPT.parent))
adapter = importlib.import_module("gherkin_yaml")
check = importlib.import_module("gherkin_check")

YAML = """feature: Todo list
scenarios:
  - name: Add a task
    steps:
      - given: an empty list
      - when: 'I add "Buy milk"'
      - then: 'I see "Buy milk"'
      - when: I reload the app
      - then: 'I still see "Buy milk"'
"""


class GherkinYamlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "tasks.feature.yaml"
        self.source.write_text(YAML)
        self.target = self.root / "tasks.feature"

    def render(self, source):
        self.source.write_text(source)
        return adapter.render_feature(self.source)

    def assertInvalid(self, source, message):
        with self.assertRaisesRegex(ValueError, message):
            self.render(source)

    def test_readable_yaml_preserves_order_and_compiles_with_official_parser(self):
        self.assertEqual(adapter.prepare_features([self.source]), [self.target])
        cases = check.feature_cases(self.target)
        self.assertEqual(len(cases), 1)
        self.assertEqual(
            [step.text for step in cases[0].steps],
            [
                "an empty list",
                'I add "Buy milk"',
                'I see "Buy milk"',
                "I reload the app",
                'I still see "Buy milk"',
            ],
        )
        self.assertEqual(adapter.generated_source(self.target), self.source)
        self.assertTrue(self.target.read_text().startswith(adapter.GENERATED_PREFIX))

    def test_background_tags_descriptions_and_outlines_compile_every_row(self):
        self.render(
            """feature: Todo list
description: |
  Save tasks across reloads.
  Scenario: this description cannot inject a scenario
tags: ['@todo', '@acceptance']
background:
  - given: an empty list
scenarios:
  - name: Add <task>
    tags: ['@persistence']
    steps:
      - when: I add <task>
      - then: I see <task>
      - and: the count is <count>
      - but: there are no errors
    examples:
      - task: Buy milk
        count: '1'
      - count: '1'
        task: Call Sam
"""
        )
        adapter.prepare_features([self.source])
        cases = check.feature_cases(self.target)
        self.assertEqual(len(cases), 2)
        self.assertEqual(len(cases[0].steps), 5)
        self.assertEqual(cases[1].steps[1].text, "I add Call Sam")
        self.assertIn("  # Scenario:", self.target.read_text())

    def test_examples_escape_native_table_characters_and_newline_values(self):
        self.render(
            """feature: Escaping
scenarios:
  - name: Store text
    steps:
      - given: text <value>
      - then: the value remains <value>
    examples:
      - value: 'left|right'
      - value: 'C:\\temp'
      - value: "first\\nsecond"
"""
        )
        adapter.prepare_features([self.source])
        cases = check.feature_cases(self.target)
        self.assertEqual(
            [case.steps[0].text for case in cases],
            ["text left|right", "text C:\\temp", "text first\nsecond"],
        )

    def test_missing_unknown_and_duplicate_keys_are_rejected(self):
        for source, message in (
            ("feature: Todo", "missing keys: scenarios"),
            (YAML + "extra: value\n", "unknown keys: extra"),
            (YAML + "feature: Duplicate\n", "duplicate YAML key: feature"),
            (
                YAML.replace("    steps:", "    extra: yes\n    steps:"),
                "unknown keys: extra",
            ),
            (
                YAML.replace("    steps:", "    name: Duplicate\n    steps:"),
                "duplicate YAML key: name",
            ),
            (
                YAML.replace("      - given:", "      - given: first\n        given:"),
                "duplicate YAML key: given",
            ),
            ("1: value", "mapping key"),
            ("? [a, b]\n: value", "mapping key"),
        ):
            with self.subTest(source=source):
                self.assertInvalid(source, message)

    def test_invalid_yaml_objects_and_scalar_injection_are_rejected(self):
        for source, message in (
            ("feature: [", "invalid YAML"),
            ("!!python/object:builtins.str {}", "invalid YAML"),
            ("[]", "expected a mapping"),
            ("", "expected a mapping"),
            (YAML.replace("Todo list", "123"), "nonempty text"),
            (YAML.replace("Todo list", "''"), "nonempty text"),
            (YAML.replace("Todo list", '"Todo\\nScenario: injected"'), "one line"),
            (YAML.replace("Add a task", '"bad\\rname"'), "one line"),
            (YAML.replace("an empty list", '"bad\\0step"'), "one line"),
            (YAML.replace("an empty list", '"bad\\nThen injected"'), "one line"),
            (YAML.replace("an empty list", "true"), "nonempty text"),
        ):
            with self.subTest(source=source):
                self.assertInvalid(source, message)

    def test_empty_or_malformed_scenarios_and_steps_are_rejected(self):
        for source, message in (
            ("feature: Todo\nscenarios: []", "nonempty list"),
            ("feature: Todo\nscenarios: text", "nonempty list"),
            ("feature: Todo\nscenarios: [null]", "expected a mapping"),
            ("feature: Todo\nscenarios: [{name: Test}]", "missing keys: steps"),
            ("feature: Todo\nscenarios: [{name: Test, steps: []}]", "nonempty list"),
            (
                "feature: Todo\nscenarios: [{name: Test, steps: [text]}]",
                "expected a mapping",
            ),
            ("feature: Todo\nscenarios: [{name: Test, steps: [{}]}]", "exactly one"),
            (YAML.replace("- given:", "- unknown:"), "exactly one"),
            (YAML.replace("- given:", "- when: first\n        given:"), "exactly one"),
        ):
            with self.subTest(source=source):
                self.assertInvalid(source, message)

    def test_optional_fields_require_valid_nonempty_values(self):
        for addition, message in (
            ("tags: []", "nonempty list"),
            ("tags: ['todo']", "tags must start"),
            ("tags: ['@bad tag']", "tags must start"),
            ("tags: ['@bad@tag']", "tags must start"),
            ("description: 123", "description: expected"),
            ("description: ''", "description: expected"),
            ("background: []", "nonempty list"),
        ):
            with self.subTest(addition=addition):
                self.assertInvalid(YAML + addition + "\n", message)

    def test_examples_reject_empty_different_columns_and_nonstring_values(self):
        base = "feature: Todo\nscenarios:\n  - name: Add <task>\n    steps: [{when: 'add <task>'}]\n    examples: "
        for examples, message in (
            ("[]", "nonempty list"),
            ("[text]", "expected a mapping"),
            ("[{}]", "at least one column"),
            ("[{task: one}, {other: two}]", "same columns"),
            ("[{task: 123}]", "quote numeric"),
            ("[{task: ''}]", "nonempty text"),
            ("[{task: true}]", "quote numeric"),
            ('[{task: "bad\\rvalue"}]', "carriage returns"),
            ('[{task: "bad\\0value"}]', "null bytes"),
        ):
            with self.subTest(examples=examples):
                self.assertInvalid(base + examples, message)

    def test_sidecars_regenerate_and_yaml_plus_native_inventory_deduplicates(self):
        self.assertEqual(
            adapter.prepare_features([self.target, self.source, self.source]),
            [self.target],
        )
        self.source.write_text(YAML.replace("Buy milk", "Call Sam"))
        self.assertEqual(adapter.prepare_features([self.target]), [self.target])
        self.assertIn("Call Sam", self.target.read_text())
        self.assertNotIn("Buy milk", self.target.read_text())
        self.assertEqual(
            adapter.prepare_features([self.source, self.target]), [self.target]
        )

    def test_native_features_pass_through_without_modification(self):
        native = "Feature: Native\n  Scenario: Check\n    Given native steps\n"
        self.target.write_text(native)
        self.assertEqual(adapter.prepare_features([self.target]), [self.target])
        self.assertEqual(self.target.read_text(), native)
        self.assertIsNone(adapter.generated_source(self.target))
        self.assertEqual(adapter.prepare_features([]), [])

    def test_short_yaml_extension_and_relative_paths(self):
        short = self.source.with_suffix(".yml")
        self.source.rename(short)
        self.assertTrue(adapter.is_yaml_feature(short))
        self.assertEqual(adapter.runner_path(short), self.target)
        self.assertEqual(adapter.runner_path(self.target), self.target)
        self.assertEqual(adapter.prepare_features([short]), [self.target])

    def test_authored_feature_is_never_overwritten(self):
        self.target.write_text("Feature: Authored\n")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            adapter.prepare_features([self.source])
        self.assertEqual(self.target.read_text(), "Feature: Authored\n")

    def test_generated_orphan_and_invalid_markers_are_rejected(self):
        adapter.prepare_features([self.source])
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "orphaned generated feature"):
            adapter.prepare_features([self.target])
        for name in ("../outside.feature.yaml", "tasks.yaml", "other.feature.yaml"):
            with self.subTest(name=name):
                self.target.write_text(adapter.GENERATED_PREFIX + name + "\n")
                with self.assertRaisesRegex(
                    ValueError, "invalid generated-source marker"
                ):
                    adapter.prepare_features([self.target])

    def test_yaml_extensions_cannot_collide_or_take_ownership(self):
        alternate = self.source.with_suffix(".yml")
        alternate.write_text(YAML)
        for inventory in ([self.source], [alternate], [self.source, alternate]):
            with self.subTest(inventory=inventory):
                with self.assertRaisesRegex(ValueError, "sources collide"):
                    adapter.prepare_features(inventory)
        alternate.unlink()
        self.target.write_text(adapter.GENERATED_PREFIX + alternate.name + "\n")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            adapter.prepare_features([self.source])
        with self.assertRaisesRegex(ValueError, "multiple YAML sources"):
            adapter.register_source({self.target: alternate}, self.source)

    def test_all_sources_are_validated_before_any_sidecar_is_written(self):
        invalid = self.root / "invalid.feature.yaml"
        invalid.write_text("feature: Missing scenarios")
        with self.assertRaisesRegex(ValueError, "missing keys"):
            adapter.prepare_features([self.source, invalid])
        self.assertFalse(self.target.exists())

    def test_source_and_output_io_failures_remain_actionable(self):
        missing = self.root / "missing.feature.yaml"
        with self.assertRaisesRegex(ValueError, "cannot read"):
            adapter.render_feature(missing)
        self.source.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "cannot read"):
            adapter.render_feature(self.source)
        self.source.write_text(YAML)
        with mock.patch.object(
            Path, "write_text", side_effect=PermissionError("read-only")
        ):
            with self.assertRaisesRegex(ValueError, "cannot write.*read-only"):
                adapter.prepare_features([self.source])
        with mock.patch.object(
            Path, "write_text", side_effect=UnicodeError("bad encoding")
        ):
            with self.assertRaisesRegex(ValueError, "cannot write.*bad encoding"):
                adapter.prepare_features([self.source])

    def test_wrong_extensions_and_missing_yaml_dependency_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "expected a .feature.yaml"):
            adapter.render_feature(self.target)
        with self.assertRaisesRegex(ValueError, "expected .feature.yaml"):
            adapter.prepare_features([self.root / "tasks.yaml"])
        with mock.patch.object(
            adapter.importlib, "import_module", side_effect=ImportError
        ):
            with self.assertRaisesRegex(ValueError, "requires PyYAML"):
                adapter.render_feature(self.source)

    def test_cli_prepares_features_and_reports_validation_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = adapter.main(
                ["--root", str(self.root), "--feature", self.source.name]
            )
        self.assertEqual(code, 0)
        self.assertIn("Prepared 1", output.getvalue())
        self.source.write_text("feature: Missing scenarios")
        with contextlib.redirect_stdout(output):
            code = adapter.main(
                ["--root", str(self.root), "--feature", self.source.name]
            )
        self.assertEqual(code, 1)
        self.assertIn("missing keys: scenarios", output.getvalue())

    def test_help_and_module_import_do_not_load_yaml(self):
        with mock.patch.object(
            adapter.importlib, "import_module", side_effect=AssertionError
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    adapter.main(["--help"])
            self.assertEqual(raised.exception.code, 0)
            runpy.run_path(str(SCRIPT), run_name="import_only")

    def test_script_entry_point_prepares_features(self):
        arguments = [
            str(SCRIPT),
            "--root",
            str(self.root),
            "--feature",
            self.source.name,
        ]
        with mock.patch.object(sys, "argv", arguments):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(self.target.is_file())


if __name__ == "__main__":
    unittest.main()
