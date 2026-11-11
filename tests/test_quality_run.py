from __future__ import annotations

import dataclasses
import sys
import sysconfig
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
for path in (SCRIPTS, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import diff_scope  # noqa: E402
import quality_config  # noqa: E402
import quality_run  # noqa: E402
import quality_run_tests  # noqa: E402
from gate_complexity_lizard import AnalysisError  # noqa: E402
from gate_coverage_recipes import CoverageTools, Recipe  # noqa: E402
from gate_types import CommandRun, GateOutcome, Status, ToolPython  # noqa: E402
from quality_commands import runner_for  # noqa: E402
from quality_model import GATE_ORDER, Analysis, GateRun  # noqa: E402
from quality_repo_fixture import (  # noqa: E402
    add_unused_function,
    make_python_repository,
)
from quality_tools import ToolSet  # noqa: E402

TOOL_PYTHON = ToolPython(
    sys.executable, {"PYTHONPATH": sysconfig.get_paths()["purelib"]}
)
TOOLS = ToolSet(TOOL_PYTHON, CoverageTools(tool_python=TOOL_PYTHON), ["tool note"])
TEST_COMMAND = [sys.executable, "-m", "unittest", "discover"]
GATE_POSITION = {key: index for index, key in enumerate(GATE_ORDER)}


def config(**sections: Any) -> dict[str, Any]:
    base = quality_config.default_config()
    base["test"]["command"] = TEST_COMMAND
    base.update(sections)
    return base


class RepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = make_python_repository(Path(directory.name).resolve() / "repo")
        self.thresholds = quality_config.default_thresholds()

    def analyze(
        self, settings: Mapping[str, Any] | None = None, **options: Any
    ) -> Analysis:
        arguments: dict[str, Any] = {
            "changes": diff_scope.repository_changes(),
            "selection": None,
            "fast": False,
            "allow_policy_change": False,
            "allow_test_removal": False,
            "previous_state": None,
            "tools": TOOLS,
            "runner": runner_for(self.root),
        }
        arguments.update(options)
        return quality_run.run_analysis(
            self.root, settings or config(), self.thresholds, **arguments
        )

    def statuses(self, analysis: Analysis) -> dict[str, str]:
        return {gate.key: gate.status for gate in analysis.gates}


class RunAnalysisTests(RepositoryTestCase):
    def test_full_run_passes_and_records_the_test_count(self) -> None:
        analysis = self.analyze()
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["project_checks"], "not_applicable")
        self.assertEqual(statuses["vulnerabilities"], "not_applicable")
        failing = {
            k: v for k, v in statuses.items() if v not in ("pass", "not_applicable")
        }
        self.assertEqual(failing, {})
        self.assertEqual(analysis.status, "pass")
        self.assertEqual(analysis.scope, "whole repository")
        self.assertEqual(analysis.version, quality_config.VERSION)
        self.assertEqual(
            analysis.baselines, {"tests_executed": 2, "tests_scope": "repository"}
        )
        self.assertEqual(analysis.notes, ["tool note"])
        self.assertTrue(all(gate.seconds is not None for gate in analysis.gates))

    def test_off_skipped_and_deferred_gates(self) -> None:
        settings = config(gates={"duplication": False, "mutation": True})
        analysis = self.analyze(settings, fast=True)
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["duplication"], "off")
        self.assertEqual(statuses["vulnerabilities"], "deferred")
        self.assertEqual(analysis.mode, "fast")
        self.assertEqual(analysis.status, "ready_for_full")
        self.assertIn("ignored removed gate mutation", analysis.notes)
        self.assertEqual(analysis.baselines, {})

    def test_selection_runs_tests_for_coverage(self) -> None:
        selection = quality_run.selected_gates(["branches"])
        analysis = self.analyze(selection=selection)
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["coverage"], "pass")
        self.assertEqual(statuses["tests"], "pass")
        self.assertEqual(statuses["secrets"], "skipped")
        self.assertEqual(analysis.mode, "partial")
        self.assertEqual(analysis.status, "selected_pass")

    def test_local_change_fails_coverage_and_dead_code(self) -> None:
        add_unused_function(self.root)
        changes = diff_scope.local_changes(self.root)
        analysis = self.analyze(changes=changes)
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["coverage"], "fail")
        self.assertEqual(statuses["dead_code"], "fail")
        self.assertEqual(statuses["complexity"], "pass")
        self.assertEqual(analysis.scope, "local changes")
        self.assertEqual(analysis.status, "fail")

    def test_test_count_drop_fails_until_approved(self) -> None:
        previous = {
            "schema_version": 2,
            "baselines": {"tests_executed": 3, "tests_scope": "repository"},
        }
        analysis = self.analyze(previous_state=previous)
        self.assertEqual(self.statuses(analysis)["tests"], "fail")
        self.assertEqual(analysis.baselines["tests_executed"], 3)
        approved = self.analyze(previous_state=previous, allow_test_removal=True)
        self.assertEqual(self.statuses(approved)["tests"], "pass")
        self.assertEqual(approved.baselines["tests_executed"], 2)

    def test_unknown_test_runner_blocks_coverage(self) -> None:
        settings = config()
        settings["test"]["command"] = [
            sys.executable,
            "-c",
            "print('Ran 1 test in 0.001s'); print(); print('OK')",
        ]
        with mock.patch.object(
            quality_run_tests.gate_coverage_recipes,
            "plan_coverage",
            return_value="no coverage recipe for this command",
        ):
            analysis = self.analyze(
                settings, selection=frozenset({"tests", "coverage"})
            )
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["tests"], "pass")
        self.assertEqual(statuses["coverage"], "blocked")

    def test_no_test_command_blocks_tests(self) -> None:
        settings = config()
        settings["test"]["command"] = None
        with mock.patch.object(
            quality_run_tests, "infer_test_command", return_value=None
        ):
            analysis = self.analyze(
                settings, selection=frozenset({"tests", "complexity"})
            )
        statuses = self.statuses(analysis)
        self.assertEqual(statuses["tests"], "blocked")
        self.assertEqual(statuses["complexity"], "pass")


class LizardFailureTests(RepositoryTestCase):
    def test_dead_code_blocks_when_lizard_cannot_run(self) -> None:
        failure = AnalysisError(None, "lizard exited with 1: Traceback\nmore")
        with mock.patch.object(
            quality_run.gate_complexity_lizard,
            "analyze_functions",
            return_value=([], [], [failure]),
        ):
            analysis = self.analyze(selection=frozenset({"dead_code"}))
        dead_code = analysis.gates[GATE_POSITION["dead_code"]]
        self.assertEqual(dead_code.status, "blocked")
        self.assertEqual(
            dead_code.summary,
            "Lizard could not list the functions: lizard exited with 1: Traceback",
        )


class SelectionAndBaselineTests(unittest.TestCase):
    def test_selected_gates(self) -> None:
        self.assertIsNone(quality_run.selected_gates([]))
        self.assertEqual(
            quality_run.selected_gates(["lint", "types"]), {"project_checks"}
        )
        self.assertEqual(quality_run.selected_gates(["crap"]), {"complexity", "tests"})
        self.assertEqual(
            {key for _, key, _ in quality_run.CHECK_FLAGS},
            set(quality_run.GATE_EVALUATORS),
        )

    def test_previous_test_count(self) -> None:
        count = quality_run.previous_test_count
        state = {
            "schema_version": 2,
            "baselines": {"tests_executed": 4, "tests_scope": "commit"},
        }
        self.assertEqual(count(state, "commit"), 4)
        self.assertIsNone(count(state, "repository"))
        self.assertIsNone(count(None, "commit"))
        self.assertIsNone(count({**state, "schema_version": 1}, "commit"))
        self.assertIsNone(count({"schema_version": 2, "baselines": []}, "commit"))
        broken = {
            "schema_version": 2,
            "baselines": {"tests_executed": "4", "tests_scope": "commit"},
        }
        self.assertIsNone(count(broken, "commit"))

    def analysis(
        self, mode: str, status: Status, measurements: dict[str, Any]
    ) -> Analysis:
        gate = GateRun.from_outcome(
            GateOutcome("tests", "Tests", "pass", "", measurements=measurements), 1.0
        )
        gate = dataclasses.replace(gate, status=status)
        other = GateRun.not_run("secrets", "skipped", "")
        return Analysis("/r", "now", "whole repository", mode, [other, gate])

    def test_updated_baselines(self) -> None:
        update = quality_run.updated_baselines
        previous = {
            "schema_version": 2,
            "baselines": {"tests_executed": 5, "tests_scope": "repository", "x": 1},
        }
        measured = {"executed": 3, "measured": True, "previous_executed": 5}
        failed = self.analysis("full", "fail", measured)
        self.assertEqual(update(previous, failed, "repository")["tests_executed"], 5)
        passed = self.analysis("full", "pass", measured)
        self.assertEqual(
            update(previous, passed, "commit"),
            {"tests_executed": 3, "tests_scope": "commit", "x": 1},
        )
        partial = self.analysis("partial", "pass", measured)
        self.assertEqual(update(previous, partial, "repository")["tests_executed"], 5)
        unmeasured = self.analysis("full", "pass", {"executed": 0, "measured": False})
        self.assertEqual(update(None, unmeasured, "repository"), {})
        blocked = self.analysis("full", "blocked", measured)
        self.assertEqual(update(None, blocked, "repository"), {})

    def test_scope_description(self) -> None:
        self.assertEqual(
            quality_run.scope_description("repository"), "whole repository"
        )
        self.assertEqual(
            quality_run.scope_description("local_changes"), "local changes"
        )
        self.assertEqual(
            quality_run.scope_description("commit", "HEAD~1"), "commit HEAD~1"
        )
        self.assertEqual(quality_run.scope_description("other"), "other")


class FakeRunner:
    def __init__(self, fail: Sequence[str] = (), on_run: Any = None) -> None:
        self.fail = set(fail)
        self.on_run = on_run
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: Sequence[str],
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        self.commands.append(tuple(command))
        if command[0] in self.fail:
            return CommandRun(tuple(command), 1, "step output\nstep failed\n", 0.1)
        if self.on_run:
            self.on_run(command)
        return CommandRun(tuple(command), 0, "Ran 1 test in 0.1s\n\nOK\n", 0.1)


class TestRunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.report = self.root / "out" / "coverage.json"

    def request(self, runner: FakeRunner) -> quality_run_tests.TestRunRequest:
        return quality_run_tests.TestRunRequest(
            self.root,
            {"test": {"command": ["tests"]}},
            quality_config.default_thresholds(),
            TOOLS,
            runner,
            self.root / "out",
            ["app.py"],
        )

    def recipe(
        self,
        setup: tuple[tuple[str, ...], ...] = (),
        finalize: tuple[tuple[str, ...], ...] = (),
    ) -> Recipe:
        return Recipe(
            ("tests",),
            {},
            self.report,
            "coverage-json",
            setup,
            finalize,
            prerequisite="needs x",
        )

    def write_report(self, command: Sequence[str]) -> None:
        if command[0] == "tests":
            self.report.write_text('{"meta": {}, "files": {}}', encoding="utf-8")

    def test_setup_failure_runs_the_plain_command(self) -> None:
        runner = FakeRunner(fail=["setup"])
        result = quality_run_tests._run_with_recipe(
            self.request(runner), self.recipe(setup=(("setup",),))
        )
        self.assertIn("coverage setup failed: setup: step failed", result.problem)
        self.assertEqual(result.outcome.status, "pass")
        self.assertEqual(runner.commands, [("setup",), ("tests",)])

    def test_missing_report_is_the_problem(self) -> None:
        runner = FakeRunner(fail=["finalize"])
        result = quality_run_tests._run_with_recipe(
            self.request(runner), self.recipe(finalize=(("finalize",),))
        )
        self.assertIn("coverage.json", result.problem)
        self.assertIsNone(result.data)
        self.assertEqual(result.prerequisite, "needs x")

    def test_finalize_failure_is_reported_when_the_report_exists(self) -> None:
        runner = FakeRunner(fail=["finalize"], on_run=self.write_report)
        result = quality_run_tests._run_with_recipe(
            self.request(runner), self.recipe(finalize=(("finalize",),))
        )
        self.assertIn("coverage report step failed", result.problem)
        self.assertEqual(result.data, {})
        self.assertTrue(result.report_fresh)

    def test_dependency_changes_fail_the_tests_gate(self) -> None:
        def restore(command: Sequence[str]) -> None:
            (self.root / "requirements.txt").write_text("requests\n", encoding="utf-8")

        result = quality_run_tests._run_with_recipe(
            self.request(FakeRunner(on_run=restore)), self.recipe()
        )
        self.assertEqual(result.outcome.status, "fail")
        self.assertIn("requirements.txt", result.outcome.summary)
        self.assertEqual(result.outcome.items[-1].value, "requirements.txt")

    def test_failure_text_without_output(self) -> None:
        run = CommandRun(("x",), 3, "", 0.1)
        self.assertEqual(quality_run_tests._failure_text(run), "x: exit 3")

    def test_failure_text_names_the_script_an_interpreter_ran(self) -> None:
        command = ("/opt/py/bin/python3", "/tmp/tools/collect.py", "--root", "/r")
        run = CommandRun(command, 2, "scanning\nllvm-cov is not on PATH\n", 0.1)
        self.assertEqual(
            quality_run_tests._failure_text(run), "collect.py: llvm-cov is not on PATH"
        )
        self.assertEqual(quality_run_tests._step_name(()), "step")

    def test_blocked_prerequisite(self) -> None:
        reason = quality_run_tests.blocked_prerequisite
        self.assertEqual(
            reason("ModuleNotFoundError: x"), "project dependencies are missing"
        )
        self.assertIn("runtime", reason("sh: jest: command not found") or "")
        self.assertIn("network", reason("not available in offline mode") or "")
        self.assertIsNone(reason("AssertionError"))

    def test_dependency_state_skips_dependency_trees(self) -> None:
        (self.root / "node_modules" / "left-pad").mkdir(parents=True)
        (self.root / "node_modules" / ".package-lock.json").write_text("{}")
        (self.root / "node_modules" / "left-pad" / "package.json").write_text("{}")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "package.json").write_text("{}")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "requirements-dev.txt").write_text("x")
        (self.root / "package.json").write_text("{}")
        state = quality_run_tests.project_dependency_state(self.root)
        self.assertEqual(
            sorted(state),
            [
                "node_modules/.package-lock.json",
                "package.json",
                "sub/requirements-dev.txt",
            ],
        )
        self.assertEqual(
            quality_run_tests.changed_dependency_state(
                {"a": "1", "b": "2"}, {"a": "1", "c": "3"}
            ),
            ["b", "c"],
        )


if __name__ == "__main__":
    unittest.main()
