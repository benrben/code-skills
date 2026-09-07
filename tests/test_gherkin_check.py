import contextlib
import copy
import importlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/code-discipline/scripts/gherkin_check.py"
)
sys.path.insert(0, str(SCRIPT.parent))
check = importlib.import_module("gherkin_check")

FEATURE = """Feature: Persist tasks
  Background:
    Given an empty list
  Scenario Outline: Save <task>
    When I add <task>
    Then the list contains <task>
    Examples:
      | task |
      | first |
      | second |
"""


def native_step(name, line, format_name):
    step = {"name": name, "result": {"status": "passed", "duration": 0.001}}
    if format_name == "behave-json":
        step["location"] = f"features/tasks.feature:{line}"
    else:
        step["line"] = line
    return step


def native_report(format_name="behave-json"):
    feature = {"name": "Persist tasks", "elements": []}
    if format_name == "behave-json":
        feature.update(location="features/tasks.feature:1", status="passed")
    else:
        feature.update(uri="features/tasks.feature", line=1)
    for line, task in ((9, "first"), (10, "second")):
        case = {
            "type": "scenario",
            "name": f"Save {task}",
            "steps": [
                native_step("an empty list", 3, format_name),
                native_step(f"I add {task}", 5, format_name),
                native_step(f"the list contains {task}", 6, format_name),
            ],
        }
        if format_name == "behave-json":
            case.update(location=f"features/tasks.feature:{line}", status="passed")
        else:
            case["line"] = line
        feature["elements"].append(case)
    return [feature]


class GherkinValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.feature = self.root / "features/tasks.feature"
        self.feature.parent.mkdir()
        self.feature.write_text(FEATURE)
        self.report = self.root / "report.json"

    def validate(self, data=None, format_name="behave-json"):
        self.report.write_text(
            json.dumps(native_report(format_name) if data is None else data)
        )
        return check.validate(self.root, self.report, format_name, [self.feature])

    def assertRejected(self, data, message="", format_name="behave-json"):
        result = self.validate(data, format_name)
        self.assertFalse(result.passed)
        self.assertIn(message, result.errors[0])

    def test_native_formats_enumerate_every_examples_row_and_background(self):
        for format_name in ("behave-json", "cucumber-json"):
            with self.subTest(format_name=format_name):
                result = self.validate(format_name=format_name)
                self.assertEqual(result, check.ValidationResult(True, 2, 6))

    def test_yaml_evidence_is_checked_against_the_current_authored_steps(self):
        self.feature.unlink()
        self.feature = self.feature.with_suffix(".feature.yaml")
        source = """feature: Persist tasks
scenarios:
  - name: Save
    steps:
      - when: I add first
      - then: the list contains first
"""
        self.feature.write_text(source)
        data = [
            {
                "uri": "features/tasks.feature",
                "line": 2,
                "elements": [
                    {
                        "type": "scenario",
                        "name": "Save",
                        "line": 4,
                        "steps": [
                            native_step("I add first", 5, "cucumber-json"),
                            native_step("the list contains first", 6, "cucumber-json"),
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(
            self.validate(data, "cucumber-json"), check.ValidationResult(True, 1, 2)
        )
        self.feature.write_text(source.replace("contains first", "contains second"))
        self.assertRejected(data, "executed steps", "cucumber-json")

    def test_missing_duplicate_and_unexpected_executions_are_rejected(self):
        for mutation, message in (
            (lambda cases: cases.pop(), "missing scenario"),
            (lambda cases: cases.append(copy.deepcopy(cases[0])), "duplicate scenario"),
            (
                lambda cases: cases[0].update(location="features/tasks.feature:2"),
                "unexpected scenario",
            ),
            (lambda cases: cases[0]["steps"].pop(), "executed steps"),
            (
                lambda cases: cases[0]["steps"][1].update(name="I add the wrong thing"),
                "executed steps",
            ),
            (lambda cases: cases[0]["steps"].reverse(), "executed steps"),
        ):
            with self.subTest(message=message):
                data = native_report()
                mutation(data[0]["elements"])
                self.assertRejected(data, message)

    def test_all_nonpassing_native_states_fail(self):
        for status in (
            "failed",
            "skipped",
            "undefined",
            "pending",
            "ambiguous",
            "hook_error",
            None,
            "PASSED",
        ):
            for position in ("feature", "scenario", "step"):
                with self.subTest(status=status, position=position):
                    data = native_report()
                    targets = {
                        "feature": data[0],
                        "scenario": data[0]["elements"][0],
                        "step": data[0]["elements"][0]["steps"][0]["result"],
                    }
                    targets[position]["status"] = status
                    self.assertRejected(data, "expected passed")

    def test_required_missing_results_and_behave_statuses_fail(self):
        for key, target in (
            ("status", lambda data: data[0]),
            ("status", lambda data: data[0]["elements"][0]),
            ("result", lambda data: data[0]["elements"][0]["steps"][0]),
        ):
            data = native_report()
            target(data).pop(key)
            self.assertRejected(data)

    def test_hooks_at_all_native_levels_must_pass(self):
        for status in ("passed", "failed", None):
            for target in (
                lambda data: data[0],
                lambda data: data[0]["elements"][0],
                lambda data: data[0]["elements"][0]["steps"][0],
            ):
                data = native_report("cucumber-json")
                target(data)["after"] = [{"result": {"status": status}}]
                self.assertEqual(
                    self.validate(data, "cucumber-json").passed, status == "passed"
                )

    def test_cucumber_hidden_hooks_do_not_count_as_steps(self):
        for status in ("passed", "failed"):
            data = native_report("cucumber-json")
            data[0]["elements"][0]["steps"].insert(
                0, {"hidden": True, "keyword": "Before", "result": {"status": status}}
            )
            result = self.validate(data, "cucumber-json")
            self.assertEqual(result.passed, status == "passed")
            if result.passed:
                self.assertEqual(result.step_count, 6)

    def test_background_declarations_and_separate_cucumber_executions(self):
        data = native_report()
        data[0]["elements"].insert(
            0, {"type": "background", "steps": [{"name": "an empty list"}]}
        )
        self.assertTrue(self.validate(data).passed)
        data = native_report("cucumber-json")
        elements = []
        for case in data[0]["elements"]:
            elements.extend(
                [{"type": "background", "steps": [case["steps"].pop(0)]}, case]
            )
        data[0]["elements"] = elements
        self.assertTrue(self.validate(data, "cucumber-json").passed)
        data[0]["elements"][0]["steps"][0]["result"]["status"] = "skipped"
        self.assertRejected(data, "expected passed", "cucumber-json")

    def test_wrong_source_locations_are_rejected(self):
        for target, value in (
            (lambda data: data[0]["elements"][0], "other.feature:9"),
            (lambda data: data[0]["elements"][0]["steps"][0], "other.feature:3"),
            (lambda data: data[0], "missing.feature:1"),
            (lambda data: data[0], "no-line"),
            (lambda data: data[0], "features/tasks.feature:zero"),
            (lambda data: data[0], "features/tasks.feature:0"),
            (lambda data: data[0], 42),
        ):
            data = native_report()
            target(data)["location"] = value
            self.assertRejected(data)

    def test_feature_inventory_and_native_structure_are_required(self):
        for data in (
            {},
            [None],
            [],
            [
                {
                    "location": "features/tasks.feature:1",
                    "status": "passed",
                    "elements": None,
                }
            ],
        ):
            self.assertRejected(data)
        data = native_report()
        data[0]["elements"][0]["type"] = "unknown"
        self.assertRejected(data, "unsupported report element")
        data = native_report()
        data.append(copy.deepcopy(data[0]))
        self.assertRejected(data, "duplicate feature report")
        data = native_report()
        data.append({"location": "other.feature:1", "status": "passed", "elements": []})
        self.assertRejected(data, "unexpected feature report")
        for value in (True, 0, "9"):
            data = native_report("cucumber-json")
            data[0]["elements"][0]["line"] = value
            self.assertRejected(data, "positive source line", "cucumber-json")

    def test_file_uris_are_normalized_and_remote_or_missing_uris_fail(self):
        data = native_report("cucumber-json")
        data[0]["uri"] = self.feature.as_uri()
        self.assertTrue(self.validate(data, "cucumber-json").passed)
        for uri in ("https://example.test/tasks.feature", "", 42):
            data[0]["uri"] = uri
            self.assertRejected(data, format_name="cucumber-json")

    def test_missing_invalid_empty_and_step_free_features_fail(self):
        for source in (
            "Feature: empty\n",
            "Feature: empty\n Scenario: no steps\n",
            "Feature: broken\n Not a scenario\n  Given misplaced\n",
        ):
            self.feature.write_text(source)
            self.assertFalse(self.validate().passed)
        self.feature.unlink()
        self.assertIn("No such file", self.validate().errors[0])
        self.feature.write_bytes(b"\xff")
        self.assertFalse(self.validate().passed)

    def test_explicit_empty_duplicate_inventory_and_unknown_format_fail(self):
        for features, format_name, message in (
            ([], "behave-json", "at least one"),
            ([self.feature, self.feature], "behave-json", "duplicate feature"),
            ([self.feature], "invented", "unsupported report format"),
        ):
            result = check.validate(self.root, self.report, format_name, features)
            self.assertFalse(result.passed)
            self.assertIn(message, result.errors[0])

    def test_bad_report_json_missing_file_and_dependency_errors_are_actionable(self):
        self.report.write_text("[broken")
        self.assertFalse(
            check.validate(self.root, self.report, "behave-json", [self.feature]).passed
        )
        self.report.unlink()
        self.assertFalse(
            check.validate(self.root, self.report, "behave-json", [self.feature]).passed
        )
        with mock.patch.object(
            check.importlib, "import_module", side_effect=ImportError("gherkin missing")
        ):
            result = check.validate(
                self.root, self.report, "behave-json", [self.feature]
            )
        self.assertIn("install gherkin-official", result.errors[0])

    def test_official_parser_diagnostics_are_preserved(self):
        self.feature.write_text("Not a Feature\n")
        self.assertIn("invalid Gherkin", self.validate().errors[0])

    def test_outline_without_examples_cannot_disappear_behind_other_passes(self):
        self.feature.write_text(
            FEATURE
            + "  Scenario Outline: unfinished <task>\n    Given an empty list\n    Examples:\n      | task |\n"
        )
        self.assertIn("no executable Examples rows", self.validate().errors[0])

    def test_multiple_step_arguments_are_malformed(self):
        data = native_report("cucumber-json")
        data[0]["elements"][0]["steps"][0]["arguments"] = [
            {"content": "one"},
            {"content": "two"},
        ]
        self.assertRejected(data, "at most one argument", "cucumber-json")

    def test_docstrings_and_tables_match_expanded_arguments_in_both_cucumber_shapes(
        self,
    ):
        self.feature.write_text('''Feature: Arguments
 Scenario: exact values
  Given table
   | task |
   | first |
  Then document
   """
   exact content
   """
''')
        data = [
            {
                "uri": "features/tasks.feature",
                "elements": [
                    {
                        "type": "scenario",
                        "line": 2,
                        "steps": [
                            native_step("table", 3, "cucumber-json"),
                            native_step("document", 6, "cucumber-json"),
                        ],
                    }
                ],
            }
        ]
        steps = data[0]["elements"][0]["steps"]
        steps[0]["arguments"] = [{"rows": [{"cells": ["task"]}, {"cells": ["first"]}]}]
        steps[1]["arguments"] = [{"content": "exact content", "line": 7}]
        self.assertTrue(self.validate(data, "cucumber-json").passed)
        steps[0]["rows"] = steps[0].pop("arguments")[0]["rows"]
        steps[1]["doc_string"] = {"value": steps[1].pop("arguments")[0]["content"]}
        self.assertTrue(self.validate(data, "cucumber-json").passed)
        steps[1]["doc_string"]["value"] = "different"
        self.assertRejected(data, "arguments differ", "cucumber-json")
        steps[0]["arguments"] = [{"unknown": 1}]
        self.assertRejected(data, "unsupported Cucumber step argument", "cucumber-json")

    def test_real_behave_runner_rule_background_outline_arguments_and_localization(
        self,
    ):
        self.feature.write_text('''# language: fr
Fonctionnalité: Données persistées
 Contexte:
  Soit une liste vide
 Règle: écriture
  Contexte:
   Soit un stockage disponible
  Plan du scénario: écrire <nom>
   Quand je stocke
    | nom |
    | <nom> |
   Alors le document vaut
    """
    <nom>
    """
   Exemples:
    | nom |
    | premier |
    | second |
''')
        steps = self.feature.parent / "steps"
        steps.mkdir()
        (steps / "tasks.py").write_text("""from behave import given, when, then
from pathlib import Path
@given("une liste vide")
def empty(context):
    context.target = Path("stored.txt")
@given("un stockage disponible")
def available(context):
    assert context.target.parent.is_dir()
@when("je stocke")
def store(context):
    context.target.write_text(context.table[0]["nom"])
@then("le document vaut")
def read(context):
    assert context.target.read_text() == context.text
""")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "behave",
                "--format",
                "json",
                "--outfile",
                str(self.report),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        validation = check.validate(
            self.root, self.report, "behave-json", [self.feature]
        )
        self.assertEqual(validation, check.ValidationResult(True, 2, 8))
        self.assertEqual((self.root / "stored.txt").read_text(), "second")
        data = json.loads(self.report.read_text())
        data[0]["elements"][2]["steps"][2]["table"]["rows"][0][0] = "wrong value"
        self.assertRejected(data, "arguments differ")

    def test_cli_returns_real_success_and_failure_codes(self):
        self.validate()
        args = [
            "--root",
            str(self.root),
            "--report",
            str(self.report),
            "--format",
            "behave-json",
            "--feature",
            str(self.feature),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main(args), 0)
        self.assertIn("2 scenarios, 6 steps", output.getvalue())
        self.report.write_text("[]")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main(args), 1)
        self.assertIn("Gherkin FAIL", output.getvalue())
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), *args]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
