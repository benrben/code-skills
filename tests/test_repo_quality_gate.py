import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOOP_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "quality_loop.py"
sys.path.insert(0, str(ROOT))

import repo_quality_gate as gate  # noqa: E402


def load_quality_loop():
    spec = importlib.util.spec_from_file_location("quality_loop_test_module", LOOP_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load quality loop: {LOOP_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quality_loop = load_quality_loop()


class QualityGateUnitTests(unittest.TestCase):
    def test_bootstrap_auto_installs_json_schema_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "api.schema.json").write_text(
                json.dumps({"type": "object"}), encoding="utf-8"
            )
            config = gate.default_config()
            config["tools"]["cache_dir"] = str(root / "tool-cache")
            config["metrics"]["report"] = "metrics.json"
            install_result = gate.CommandResult(
                command=["python", "-m", "pip", "install", "jsonschema"],
                returncode=0,
                stdout="installed",
                duration_seconds=0.1,
            )

            with (
                mock.patch.object(gate, "_python_module_available", return_value=False),
                mock.patch.object(gate, "run_command", return_value=install_result),
            ):
                tools = gate.bootstrap_tools(root, config, [])

            self.assertEqual(len(tools.setup_results), 1)
            self.assertIn("jsonschema", tools.setup_results[0].command)
            self.assertEqual(
                gate.discover_contract_files(root, config["contracts"]),
                [root / "api.schema.json"],
            )

    def test_failing_report_has_master_and_focused_repair_prompts(self) -> None:
        function = gate.FunctionMetric(
            path="src/app.py",
            name="choose",
            start_line=3,
            end_line=8,
            complexity=7,
            covered_lines=4,
            total_lines=6,
            coverage_percent=66.67,
            craap_score=16.72,
            parser="python-ast",
        )
        mutation = gate.Mutation(
            mutant_id="mutant-1",
            path="src/app.py",
            line=5,
            column=12,
            original="==",
            replacement="!=",
            survived=True,
            timed_out=False,
            duration_seconds=0.1,
            output="tests still passed",
        )
        violation = gate.DependencyViolation(
            source="src/domain/service.py",
            source_module="domain",
            target="src/infrastructure/db.py",
            target_module="infrastructure",
            rule="domain may not depend on infrastructure",
            line=4,
        )
        report = gate.AnalysisReport(
            root="/tmp/example-repo",
            generated_at="2026-08-29T00:00:00Z",
            languages=["Python"],
            gates=[
                gate.GateResult(
                    "craap",
                    "CRAAP analysis",
                    False,
                    "One function failed.",
                    prompts=[("Fix choose", "Repair choose and add tests.")],
                ),
                gate.GateResult(
                    "mutation",
                    "Mutation testing",
                    False,
                    "One mutant survived.",
                    prompts=[("Kill mutant", "Add a test that kills mutant-1.")],
                ),
                gate.GateResult(
                    "dependencies",
                    "Module dependencies",
                    False,
                    "One dependency rule was violated.",
                    prompts=[("Repair boundary", "Invert the dependency.")],
                ),
            ],
            functions=[function],
            mutations=[mutation],
            dependency_violations=[violation],
            tool_setup=[],
            notes=[],
            rerun_command="python3 /plugin/quality_loop.py --root .",
        )

        combined = gate.master_fix_prompt(report)
        rendered = gate.html_report(report)

        self.assertIn("Fix every issue", rendered)
        self.assertIn("Copy complete prompt", rendered)
        self.assertIn("Prefer smaller tasks?", rendered)
        self.assertEqual(rendered.count("data-copy="), 4)
        self.assertIn("every production function has 100%", combined)
        self.assertIn("zero survive", combined)
        self.assertIn("zero ownership or direction-rule violations", combined)
        self.assertIn("Continue until the full gate exits 0", combined)
        self.assertIn("python3 /plugin/quality_loop.py --root .", combined)

    def test_package_scripts_feed_reusable_command_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "lint": "eslint .",
                            "format:check": "prettier --check .",
                            "typecheck": "tsc --noEmit",
                            "test:contracts": "node contract-test.js",
                            "dead-code": "knip",
                        }
                    }
                ),
                encoding="utf-8",
            )
            tools = gate.ToolContext(
                cache_dir=root,
                python=sys.executable,
                python_path=root,
            )

            with mock.patch.object(
                gate,
                "executable",
                side_effect=lambda _root, name: "/usr/bin/npm"
                if name == "npm"
                else None,
            ):
                format_commands = gate.infer_format_lint_commands(root, [], tools)
                type_commands = gate.infer_type_commands(root, [], tools)
                contract_commands = gate.infer_contract_commands(root)
                dead_commands = gate.infer_dead_code_commands(root, [], tools)

            self.assertEqual(
                [command.command[-1] for command in format_commands],
                ["lint", "format:check"],
            )
            self.assertEqual(type_commands[0].command[-1], "typecheck")
            self.assertEqual(contract_commands[0].command[-1], "test:contracts")
            self.assertEqual(dead_commands[0].command[-1], "dead-code")

    def test_optional_command_gate_is_explicitly_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = gate.run_command_check_gate(
                Path(temporary),
                "types",
                "Static type checking",
                {"enabled": "auto", "required": False, "commands": []},
                [],
                "Configure a type checker.",
            )

        self.assertTrue(result.passed)
        self.assertFalse(result.applicable)
        self.assertIn("Not applicable", result.summary)
        self.assertEqual(gate.gate_outcome(result), "NOT APPLICABLE")

    def test_fast_mode_deferred_gate_cannot_certify(self) -> None:
        deferred = gate.deferred_check(
            "mutation", "Mutation testing", "the full run owns this check."
        )
        report = gate.AnalysisReport(
            root="/repo",
            generated_at="now",
            languages=["Python"],
            gates=[
                gate.GateResult("quality", "Quality", True, "Measured clean."),
                deferred,
            ],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
            mode="fast",
        )

        self.assertEqual(gate.gate_outcome(deferred), "DEFERRED")
        self.assertFalse(report.passed)
        self.assertTrue(report.ready_for_full)
        self.assertIn("FULL RUN ONLY", gate.html_report(report))
        self.assertIn("READY FOR FULL RUN", gate.html_report(report))

    def test_metrics_run_diagnostically_when_baseline_tests_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def value():\n    return 1\n", encoding="utf-8")
            (root / "metrics.json").write_text(
                json.dumps(
                    {
                        "functions": [
                            {
                                "path": "app.py",
                                "name": "value",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = gate.default_config()
            config["test"]["command"] = [
                sys.executable,
                "-c",
                "raise SystemExit(1)",
            ]
            config["metrics"]["report"] = "metrics.json"

            report = gate.run(
                root,
                config,
                root / "report.html",
                cli_max_mutants=None,
                notes=[],
                fast=True,
            )
            quality = next(item for item in report.gates if item.key == "quality")

            self.assertFalse(quality.passed)
            self.assertIn("ran diagnostically", quality.summary)
            self.assertEqual(len(report.functions), 1)
            self.assertEqual(report.functions[0].coverage_percent, 100)
            self.assertEqual(quality.command_results[0].returncode, 1)

    def test_output_sensitive_formatter_command_fails_on_listed_files(self) -> None:
        command_result = gate.CommandResult(
            command=["gofmt", "-l", "app.go"],
            returncode=0,
            stdout="app.go\n",
            duration_seconds=0.1,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(gate, "run_command", return_value=command_result),
        ):
            result = gate.run_command_check_gate(
                Path(temporary),
                "format_lint",
                "Formatter & lint",
                {"enabled": "auto", "required": False, "commands": []},
                [gate.CheckCommand(command_result.command, fail_on_output=True)],
                "Configure formatting.",
            )

        self.assertFalse(result.passed)
        self.assertTrue(result.applicable)

    def test_flaky_gate_fails_on_inconsistent_exit_codes(self) -> None:
        baseline = gate.CommandResult(
            command=["test-command"],
            returncode=0,
            stdout="pass",
            duration_seconds=0.1,
        )
        passing = gate.CommandResult(
            command=["test-command"],
            returncode=0,
            stdout="pass",
            duration_seconds=0.1,
        )
        failing = gate.CommandResult(
            command=["test-command"],
            returncode=1,
            stdout="intermittent failure",
            duration_seconds=0.1,
        )
        config = gate.default_config()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(gate, "run_command", side_effect=[passing, failing]),
        ):
            result = gate.run_flaky_test_gate(
                Path(temporary), config, ["test-command"], baseline
            )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.command_results), 3)
        self.assertIn("inconsistent", result.summary)

    def test_bootstrap_auto_installs_lizard_for_typescript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.ts"
            source.write_text(
                "export function value() { return 1 }\n", encoding="utf-8"
            )
            config = gate.default_config()
            config["tools"]["cache_dir"] = str(root / "tool-cache")
            install_result = gate.CommandResult(
                command=["python", "-m", "pip", "install", "lizard"],
                returncode=0,
                stdout="installed",
                duration_seconds=0.1,
            )

            with (
                mock.patch.object(
                    gate, "_python_module_available", side_effect=[False, True]
                ),
                mock.patch.object(gate, "run_command", return_value=install_result),
            ):
                tools = gate.bootstrap_tools(root, config, [source])

            self.assertTrue(tools.lizard_available)
            self.assertEqual(tools.setup_results, [install_result])
            install_command = tools.setup_results[0].command
            self.assertIn("pip", install_command)
            self.assertIn("lizard", install_command)

    def test_bootstrap_installs_matching_vitest_coverage_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.ts"
            source.write_text("export const value = 1\n", encoding="utf-8")
            config = gate.default_config()
            config["tools"]["cache_dir"] = str(root / "tool-cache")

            def package_version(_root: Path, name: str) -> str | None:
                return "4.1.11" if name == "vitest" else None

            def successful_command(
                command: list[str], *_args: object, **_kwargs: object
            ) -> gate.CommandResult:
                return gate.CommandResult(
                    command=list(command),
                    returncode=0,
                    stdout="installed",
                    duration_seconds=0.1,
                )

            with (
                mock.patch.object(gate, "_python_module_available", return_value=True),
                mock.patch.object(
                    gate, "package_dependencies", return_value={"vitest": "4.1.11"}
                ),
                mock.patch.object(
                    gate, "node_package_version", side_effect=package_version
                ),
                mock.patch.object(
                    gate,
                    "executable",
                    side_effect=lambda _root, name: name,
                ),
                mock.patch.object(gate, "run_command", side_effect=successful_command),
            ):
                tools = gate.bootstrap_tools(root, config, [source])

            coverage_installs = [
                result
                for result in tools.setup_results
                if any("@vitest/coverage-v8" in item for item in result.command)
            ]
            self.assertEqual(len(coverage_installs), 1)
            command = coverage_installs[0].command
            self.assertIn("--no-save", command)
            self.assertIn("--package-lock=false", command)
            self.assertIn("@vitest/coverage-v8@4.1.11", command)

    def test_bootstrap_auto_installs_native_vitest_mutation_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.ts"
            source.write_text("export const value = 1\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {"test": "vitest run"},
                        "devDependencies": {
                            "vitest": "4.1.11",
                            "@vitest/coverage-v8": "4.1.11",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = gate.default_config()
            config["metrics"]["report"] = "metrics.json"

            def package_version(_root: Path, name: str) -> str | None:
                return "4.1.11" if name in {"vitest", "@vitest/coverage-v8"} else None

            install = gate.CommandResult(
                command=["npm", "install"],
                returncode=0,
                stdout="installed",
                duration_seconds=0.1,
            )
            with (
                mock.patch.object(gate, "node_package_version", side_effect=package_version),
                mock.patch.object(gate, "executable", return_value="npm"),
                mock.patch.object(gate, "run_command", return_value=install) as runner,
            ):
                tools = gate.bootstrap_tools(root, config, [source])

            install_command = runner.call_args.args[0]
            self.assertIn("@stryker-mutator/core@9.6.1", install_command)
            self.assertIn("@stryker-mutator/vitest-runner@9.6.1", install_command)
            self.assertIn("--no-save", install_command)
            self.assertEqual(tools.stryker_command, [str(root / "node_modules/.bin/stryker")])

    def test_stryker_config_is_incremental_and_safe_inside_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/app.ts"
            source.parent.mkdir()
            source.write_text("export const value = 1\n", encoding="utf-8")
            report = root / "artifacts/report.json"
            incremental = root / "cache/incremental.json"

            config = gate.stryker_config(
                root,
                [source],
                report,
                incremental,
                workers=4,
            )

            self.assertEqual(config["testRunner"], "vitest")
            self.assertTrue(config["inPlace"])
            self.assertTrue(config["incremental"])
            self.assertEqual(config["incrementalFile"], str(incremental))
            self.assertEqual(config["mutate"], ["src/app.ts"])
            self.assertEqual(config["concurrency"], 4)
            self.assertTrue(config["tempDirName"].startswith("node_modules/"))
            self.assertEqual(config["reporters"], ["json"])

    def test_stryker_report_requires_an_assertion_kill_for_every_mutant(self) -> None:
        report = {
            "files": {
                "src/app.ts": {
                    "source": "export const selected = value < 3;\n",
                    "mutants": [
                        {
                            "id": "killed",
                            "mutatorName": "EqualityOperator",
                            "replacement": ">",
                            "status": "Killed",
                            "location": {
                                "start": {"line": 1, "column": 30},
                                "end": {"line": 1, "column": 31},
                            },
                        },
                        {
                            "id": "survived",
                            "mutatorName": "EqualityOperator",
                            "replacement": ">=",
                            "status": "Survived",
                            "location": {
                                "start": {"line": 1, "column": 30},
                                "end": {"line": 1, "column": 31},
                            },
                        },
                        {
                            "id": "uncovered",
                            "mutatorName": "EqualityOperator",
                            "replacement": "<=",
                            "status": "NoCoverage",
                            "location": {
                                "start": {"line": 1, "column": 30},
                                "end": {"line": 1, "column": 31},
                            },
                        },
                        {
                            "id": "timeout",
                            "mutatorName": "EqualityOperator",
                            "replacement": "==",
                            "status": "Timeout",
                            "statusReason": "hit limit",
                            "location": {
                                "start": {"line": 1, "column": 30},
                                "end": {"line": 1, "column": 31},
                            },
                        },
                        {
                            "id": "excluded",
                            "mutatorName": "StringLiteral",
                            "replacement": '""',
                            "status": "Ignored",
                            "location": {
                                "start": {"line": 1, "column": 13},
                                "end": {"line": 1, "column": 21},
                            },
                        },
                        {
                            "id": "suppressed-operator",
                            "mutatorName": "EqualityOperator",
                            "replacement": "!=",
                            "status": "Ignored",
                            "location": {
                                "start": {"line": 1, "column": 30},
                                "end": {"line": 1, "column": 31},
                            },
                        },
                    ],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mutation.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            mutations = gate.parse_stryker_report(path)

        self.assertEqual([mutation.original for mutation in mutations], ["<"] * 5)
        self.assertEqual(
            [mutation.status for mutation in mutations],
            ["Killed", "Survived", "NoCoverage", "Timeout", "Ignored"],
        )
        self.assertEqual(
            [mutation.survived for mutation in mutations],
            [False, True, True, True, True],
        )
        self.assertTrue(mutations[-2].timed_out)

    def test_exact_stryker_proof_cache_requires_matching_content_fingerprint(self) -> None:
        report = {
            "files": {
                "src/app.ts": {
                    "source": "export const value = 1;\n",
                    "mutants": [
                        {
                            "id": "one",
                            "mutatorName": "EqualityOperator",
                            "replacement": "2",
                            "status": "Killed",
                            "location": {
                                "start": {"line": 1, "column": 21},
                                "end": {"line": 1, "column": 22},
                            },
                        }
                    ],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incremental = root / "incremental.json"
            metadata = root / "proof.json"
            incremental.write_text(json.dumps(report), encoding="utf-8")
            metadata.write_text(
                json.dumps({"fingerprint": "matching", "complete": True}),
                encoding="utf-8",
            )

            cached = gate.load_stryker_proof_cache(
                incremental, metadata, "matching"
            )
            stale = gate.load_stryker_proof_cache(incremental, metadata, "changed")

        self.assertIsNotNone(cached)
        self.assertEqual(len(cached or []), 1)
        self.assertIsNone(stale)

    def test_mutation_proof_fingerprint_changes_with_tests_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/app.ts"
            test = root / "tests/app.test.ts"
            source.parent.mkdir()
            test.parent.mkdir()
            source.write_text("export const value = 1;\n", encoding="utf-8")
            test.write_text("expect(value).toBe(1);\n", encoding="utf-8")
            config = gate.default_config()["mutation"]

            original = gate.mutation_proof_fingerprint(root, [source], config)
            test.write_text("expect(value).toBe(2);\n", encoding="utf-8")
            after_test = gate.mutation_proof_fingerprint(root, [source], config)
            source.write_text("export const value = 2;\n", encoding="utf-8")
            after_source = gate.mutation_proof_fingerprint(root, [source], config)

        self.assertNotEqual(original, after_test)
        self.assertNotEqual(after_test, after_source)

    def test_command_substitution_preserves_unrelated_braces(self) -> None:
        command = gate.command_list(
            ["tool", "--output={report}", "print({'value': 1})"],
            {"report": "/tmp/report.json"},
        )
        self.assertEqual(
            command,
            ["tool", "--output=/tmp/report.json", "print({'value': 1})"],
        )

    def test_craap_uses_standard_formula(self) -> None:
        self.assertEqual(gate.craap_score(6, 100), 6)
        self.assertEqual(gate.craap_score(4, 0), 20)
        self.assertAlmostEqual(gate.craap_score(4, 50), 6)

    def test_lizard_adapter_normalizes_multilanguage_functions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools_dir = root / "tools"
            workspace = root / "workspace"
            tools_dir.mkdir()
            workspace.mkdir()
            source = root / "app.ts"
            source.write_text("const pick = (x) => x ? 1 : 0\n", encoding="utf-8")
            (tools_dir / "lizard.py").write_text(
                """\
from types import SimpleNamespace
def analyze_file(filename):
    function = SimpleNamespace(name='pick', start_line=1, end_line=1, cyclomatic_complexity=2)
    return SimpleNamespace(function_list=[function])
""",
                encoding="utf-8",
            )
            tools = gate.ToolContext(
                cache_dir=root,
                python=sys.executable,
                python_path=tools_dir,
                lizard_available=True,
            )

            parsed, result = gate.analyze_with_lizard([source], root, workspace, tools)

            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(parsed["app.ts"], [("pick", 1, 1, 2, "lizard")])

    def test_python_complexity_excludes_nested_function_branches(self) -> None:
        source = """\
def outer(value):
    def inner(flag):
        if flag:
            return 1
        return 0
    if value and value > 1:
        return inner(value)
    return 0
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_text(source, encoding="utf-8")
            functions = gate.parse_python_functions(path)

        by_name = {name: complexity for name, _, _, complexity, _ in functions}
        self.assertEqual(by_name["outer"], 3)
        self.assertEqual(by_name["outer.inner"], 2)

    def test_import_scanner_keeps_import_strings_and_ignores_comments(self) -> None:
        source = """\
// import fake from './ignored.js'
import real from './real.js'
const helper = require('./helper.js')
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.js"
            path.write_text(source, encoding="utf-8")
            imports = gate.import_specs(path)

        self.assertIn(("./real.js", 2), imports)
        self.assertIn(("./helper.js", 3), imports)
        self.assertNotIn(("./ignored.js", 1), imports)

    def test_mutation_gate_kills_operator_and_restores_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            original = "def is_one(value):\n    return value == 1\n"
            source.write_text(original, encoding="utf-8")
            original_mtime = source.stat().st_mtime_ns
            (root / "check.py").write_text(
                "from app import is_one\nassert is_one(1)\nassert not is_one(2)\n",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["test"]["command"] = [sys.executable, "check.py"]
            config["mutation"]["operators"] = {"==": "!="}

            result, mutations = gate.run_mutation_gate(
                root, config, [source], cli_max_mutants=None
            )

            self.assertTrue(result.passed)
            self.assertEqual(len(mutations), 1)
            self.assertFalse(mutations[0].survived)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual(source.stat().st_mtime_ns, original_mtime)

    def test_single_mutation_worker_uses_an_isolated_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            original = "def is_one(value):\n    return value == 1\n"
            source.write_text(original, encoding="utf-8")
            config = gate.default_config()
            config["test"]["command"] = ["test-command"]
            config["mutation"]["operators"] = {"==": "!="}
            mutation_roots: list[Path] = []

            def fake_run_command(
                command: list[str],
                execution_root: Path,
                _timeout: int,
                _extra_env: dict[str, str] | None = None,
            ) -> gate.CommandResult:
                if execution_root == root:
                    self.assertEqual(source.read_text(encoding="utf-8"), original)
                    return gate.CommandResult(command, 0, "baseline", 0.01)
                mutation_roots.append(execution_root)
                self.assertEqual(source.read_text(encoding="utf-8"), original)
                self.assertNotEqual(
                    (execution_root / "app.py").read_text(encoding="utf-8"),
                    original,
                )
                return gate.CommandResult(command, 1, "killed", 0.01)

            with mock.patch.object(
                gate, "run_command", side_effect=fake_run_command
            ):
                result, mutations = gate.run_mutation_gate(
                    root,
                    config,
                    [source],
                    cli_max_mutants=None,
                    cli_mutation_workers=1,
                )

            self.assertTrue(result.passed)
            self.assertEqual(len(mutations), 1)
            self.assertEqual(len(mutation_roots), 1)
            self.assertNotEqual(mutation_roots[0], root)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_mutation_gate_runs_parallel_workers_in_isolated_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            original = "def choose(value):\n    return value == 1 or value < 3\n"
            source.write_text(original, encoding="utf-8")
            config = gate.default_config()
            config["test"]["command"] = ["test-command"]
            config["mutation"]["operators"] = {"==": "!=", "<": ">"}
            barrier = threading.Barrier(2)
            lock = threading.Lock()
            active = 0
            maximum_active = 0
            worker_roots: set[Path] = set()

            def fake_run_command(
                command: list[str],
                execution_root: Path,
                _timeout: int,
                extra_env: dict[str, str] | None = None,
            ) -> gate.CommandResult:
                nonlocal active, maximum_active
                if execution_root == root:
                    return gate.CommandResult(command, 0, "baseline", 0.01)
                self.assertIsNotNone(extra_env)
                self.assertNotEqual(
                    (execution_root / "app.py").read_text(encoding="utf-8"),
                    original,
                )
                with lock:
                    worker_roots.add(execution_root)
                    active += 1
                    maximum_active = max(maximum_active, active)
                barrier.wait(timeout=5)
                time.sleep(0.05)
                with lock:
                    active -= 1
                return gate.CommandResult(command, 1, "killed", 0.05)

            with (
                mock.patch.object(gate.os, "cpu_count", return_value=8),
                mock.patch.object(gate, "run_command", side_effect=fake_run_command),
            ):
                result, mutations = gate.run_mutation_gate(
                    root,
                    config,
                    [source],
                    cli_max_mutants=None,
                    cli_mutation_workers=2,
                )

            self.assertTrue(result.passed)
            self.assertEqual(len(mutations), 2)
            self.assertEqual(maximum_active, 2)
            self.assertEqual(len(worker_roots), 2)
            self.assertTrue(all(path != root for path in worker_roots))
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertIn("using 2 workers", result.summary)

    def test_parallel_mutation_gate_runs_real_tests_in_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            original = "def selected(value):\n    return value == 1 or value < 0\n"
            source.write_text(original, encoding="utf-8")
            (root / "check.py").write_text(
                "from app import selected\n"
                "assert selected(1)\n"
                "assert selected(-1)\n"
                "assert not selected(2)\n",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["test"]["command"] = [sys.executable, "check.py"]
            config["mutation"]["operators"] = {"==": "!=", "<": ">"}

            result, mutations = gate.run_mutation_gate(
                root,
                config,
                [source],
                cli_max_mutants=None,
                cli_mutation_workers=2,
            )

            self.assertTrue(result.passed)
            self.assertEqual(len(mutations), 2)
            self.assertTrue(all(not mutation.survived for mutation in mutations))
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_native_vitest_mutation_reuses_completed_proof_without_a_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/app.ts"
            source.parent.mkdir()
            original = "export const selected = value == 1;\n"
            source.write_text(original, encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"devDependencies": {"vitest": "4.1.11"}}),
                encoding="utf-8",
            )
            tools = gate.ToolContext(
                cache_dir=root / "tool-cache",
                python=sys.executable,
                python_path=root / "python-tools",
                stryker_command=[str(root / "node_modules/.bin/stryker")],
            )
            config = gate.default_config()
            config["test"]["command"] = ["npm", "test"]
            baseline = gate.CommandResult(["npm", "test"], 0, "green", 0.1)
            runner_calls = 0

            def fake_stryker(
                command: list[str],
                execution_root: Path,
                _timeout: int,
                _extra_env: dict[str, str] | None = None,
            ) -> gate.CommandResult:
                nonlocal runner_calls
                runner_calls += 1
                self.assertNotEqual(execution_root, root)
                stryker_options = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                self.assertTrue(stryker_options["inPlace"])
                report = {
                    "files": {
                        "src/app.ts": {
                            "source": original,
                            "mutants": [
                                {
                                    "id": "one",
                                    "mutatorName": "EqualityOperator",
                                    "replacement": "!=",
                                    "status": "Killed",
                                    "location": {
                                        "start": {"line": 1, "column": 30},
                                        "end": {"line": 1, "column": 32},
                                    },
                                }
                            ],
                        }
                    }
                }
                Path(stryker_options["jsonReporter"]["fileName"]).write_text(
                    json.dumps(report), encoding="utf-8"
                )
                return gate.CommandResult(command, 0, "cold native run", 0.2)

            with mock.patch.object(gate, "run_command", side_effect=fake_stryker):
                cold, cold_mutations = gate.run_mutation_gate(
                    root,
                    config,
                    [source],
                    cli_max_mutants=None,
                    test_baseline=baseline,
                    cli_mutation_workers="auto",
                    tools=tools,
                )
                cached, cached_mutations = gate.run_mutation_gate(
                    root,
                    config,
                    [source],
                    cli_max_mutants=None,
                    test_baseline=baseline,
                    cli_mutation_workers="auto",
                    tools=tools,
                )

            self.assertTrue(cold.passed)
            self.assertTrue(cached.passed)
            self.assertEqual(runner_calls, 1)
            self.assertEqual(len(cold_mutations), 1)
            self.assertEqual(len(cached_mutations), 1)
            self.assertIn("reused exact proof", cached.summary)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_mutation_worker_validation(self) -> None:
        with mock.patch.object(gate.os, "cpu_count", return_value=12):
            self.assertEqual(gate.resolve_mutation_workers("auto", 4), 1)
            self.assertEqual(gate.resolve_mutation_workers("auto", 20), 2)
            self.assertEqual(gate.resolve_mutation_workers("auto", 40), 4)
        self.assertEqual(gate.resolve_mutation_workers("8", 3), 3)
        for invalid in (0, "0", "many", True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    gate.resolve_mutation_workers(invalid, 10)

    def test_quality_loop_rejects_a_concurrent_run_for_the_same_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                with quality_loop.repository_run_lock(root):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(LOOP_SCRIPT),
                            "--root",
                            str(root),
                            "--fast",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                        env=os.environ.copy(),
                    )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("already running", completed.stderr)

    def test_quality_loop_interrupt_stops_the_complete_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            child_pid_path = root / "child.pid"
            (root / "check.py").write_text(
                "from pathlib import Path\n"
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            (root / ".quality-gate.json").write_text(
                json.dumps(
                    {
                        "test": {
                            "command": [sys.executable, "check.py"],
                            "timeout_seconds": 60,
                        }
                    }
                ),
                encoding="utf-8",
            )
            wrapper = (
                "import os, signal, sys; "
                "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                "os.execv(sys.executable, [sys.executable, sys.argv[1], "
                "'--root', sys.argv[2], '--fast', '--no-install'])"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(LOOP_SCRIPT), str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            child_pid: int | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not child_pid_path.exists():
                    time.sleep(0.05)
                self.assertTrue(child_pid_path.exists(), "test command did not start")
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                os.kill(process.pid, signal.SIGINT)
                process.communicate(timeout=8)

                self.assertEqual(process.returncode, 130)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_dependency_gate_rejects_forbidden_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            domain = root / "src" / "domain"
            infrastructure = root / "src" / "infrastructure"
            domain.mkdir(parents=True)
            infrastructure.mkdir(parents=True)
            source = domain / "service.py"
            target = infrastructure / "db.py"
            source.write_text(
                "from src.infrastructure.db import save\n", encoding="utf-8"
            )
            target.write_text("def save():\n    pass\n", encoding="utf-8")
            rules = {
                "modules": [
                    {"name": "domain", "paths": ["src/domain/**"]},
                    {
                        "name": "infrastructure",
                        "paths": ["src/infrastructure/**"],
                    },
                ],
                "allow": {"domain": [], "infrastructure": ["domain"]},
                "deny": [{"from": "domain", "to": "infrastructure"}],
            }
            (root / ".quality-dependencies.json").write_text(
                json.dumps(rules), encoding="utf-8"
            )
            config = gate.default_config()

            result, violations = gate.run_dependency_gate(
                root, config, [source, target]
            )

            self.assertFalse(result.passed)
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0].source_module, "domain")
            self.assertEqual(violations[0].target_module, "infrastructure")


class QualityGateEndToEndTests(unittest.TestCase):
    def test_agent_loop_emits_pass_fail_and_error_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            source = root / "src" / "app.py"
            source.write_text(
                "def is_one(value):\n    return value == 1\n", encoding="utf-8"
            )
            (root / "check.py").write_text(
                "from src.app import is_one\nassert is_one(1)\nassert not is_one(2)\n",
                encoding="utf-8",
            )
            (root / "metrics.json").write_text(
                json.dumps(
                    {
                        "functions": [
                            {
                                "path": "src/app.py",
                                "name": "is_one",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / ".quality-dependencies.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "modules": [{"name": "core", "paths": ["src/**"]}],
                        "allow": {"core": []},
                        "deny": [],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / ".quality-gate.json"
            config_path.write_text(
                json.dumps(
                    gate.deep_merge(
                        gate.default_config(),
                        {
                            "source": {"include": ["src/**"], "exclude": []},
                            "test": {"command": [sys.executable, "check.py"]},
                            "format_lint": {
                                "commands": [
                                    [sys.executable, "-c", "print('format clean')"]
                                ]
                            },
                            "types": {
                                "commands": [
                                    [sys.executable, "-c", "print('types clean')"]
                                ]
                            },
                            "contracts": {
                                "commands": [
                                    [sys.executable, "-c", "print('contracts valid')"]
                                ]
                            },
                            "metrics": {"report": "metrics.json"},
                            "dead_code": {
                                "commands": [
                                    [sys.executable, "-c", "print('no dead code')"]
                                ]
                            },
                            "mutation": {
                                "test_command": [sys.executable, "check.py"],
                                "operators": {"==": "!="},
                            },
                        },
                    )
                ),
                encoding="utf-8",
            )
            artifacts = root / "artifacts"
            command = [
                sys.executable,
                str(LOOP_SCRIPT),
                "--root",
                str(root),
                "--artifact-dir",
                str(artifacts),
                "--no-install",
                "--mutation-workers",
                "auto",
            ]

            passing = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            passing_state = json.loads(
                (artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
            self.assertEqual(passing_state["status"], "pass")
            self.assertEqual(len(passing_state["gates"]), 8)
            self.assertEqual(passing_state["counts"]["checks_applicable"], 8)
            self.assertEqual(passing_state["counts"]["checks_passing"], 8)
            self.assertIsNone(passing_state["fix_prompt"])
            self.assertIn("--mutation-workers auto", passing_state["rerun_command"])

            fast_html = root / "fast-report.html"
            fast = subprocess.run(
                [
                    sys.executable,
                    str(LOOP_SCRIPT),
                    "--root",
                    str(root),
                    "--html",
                    str(fast_html),
                    "--no-install",
                    "--fast",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            fast_state = json.loads(
                (root / "quality-gate-state.json").read_text(encoding="utf-8")
            )
            fast_statuses = {
                item["key"]: item["status"] for item in fast_state["gates"]
            }
            fast_test_commands = [
                command
                for result in fast_state["gates"]
                for command in result["commands"]
                if command["command"][-1] == "check.py"
            ]

            self.assertEqual(fast.returncode, 1, fast.stdout + fast.stderr)
            self.assertEqual(fast_state["status"], "ready_for_full")
            self.assertEqual(fast_state["mode"], "fast")
            self.assertFalse(fast_state["certified"])
            self.assertTrue(fast_state["ready_for_full"])
            self.assertEqual(fast_state["counts"]["checks_deferred"], 2)
            self.assertEqual(fast_statuses["flaky"], "deferred")
            self.assertEqual(fast_statuses["mutation"], "deferred")
            self.assertEqual(len(fast_test_commands), 1)
            self.assertIn("--fast", fast_state["rerun_command"])
            self.assertNotIn("--fast", fast_state["full_rerun_command"])
            self.assertTrue(fast_html.exists())
            self.assertIn("FAST", fast_html.read_text(encoding="utf-8"))

            rules_path.unlink()
            failing = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            failing_state = json.loads(
                (artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(failing.returncode, 1, failing.stdout + failing.stderr)
            self.assertEqual(failing_state["status"], "fail")
            self.assertIsNotNone(failing_state["fix_prompt"])
            self.assertIn(str(LOOP_SCRIPT), failing_state["rerun_command"])
            self.assertEqual(failing_state["counts"]["dependency_violations"], 0)
            dependency_gate = next(
                item for item in failing_state["gates"] if item["key"] == "dependencies"
            )
            self.assertEqual(dependency_gate["status"], "fail")

            config_path.write_text("{invalid", encoding="utf-8")
            broken = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            broken_state = json.loads(
                (artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(broken.returncode, 2, broken.stdout + broken.stderr)
            self.assertEqual(broken_state["status"], "error")
            self.assertTrue(broken_state["error"])

    def test_complete_generic_adapter_run_passes_and_writes_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            source = root / "src" / "app.py"
            source.write_text(
                "def is_one(value):\n    return value == 1\n", encoding="utf-8"
            )
            (root / "check.py").write_text(
                "from src.app import is_one\nassert is_one(1)\nassert not is_one(2)\n",
                encoding="utf-8",
            )
            metrics = {
                "functions": [
                    {
                        "path": "src/app.py",
                        "name": "is_one",
                        "start_line": 1,
                        "end_line": 2,
                        "complexity": 1,
                        "covered_lines": 2,
                        "total_lines": 2,
                        "coverage_percent": 100,
                    }
                ]
            }
            (root / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
            dependencies = {
                "modules": [{"name": "core", "paths": ["src/**"]}],
                "allow": {"core": []},
                "deny": [],
            }
            (root / ".quality-dependencies.json").write_text(
                json.dumps(dependencies), encoding="utf-8"
            )
            config = gate.default_config()
            passing_command = [sys.executable, "check.py"]
            config = gate.deep_merge(
                config,
                {
                    "source": {"include": ["src/**"], "exclude": []},
                    "test": {"command": passing_command},
                    "format_lint": {
                        "commands": [[sys.executable, "-c", "print('format clean')"]]
                    },
                    "types": {
                        "commands": [[sys.executable, "-c", "print('types clean')"]]
                    },
                    "contracts": {
                        "commands": [[sys.executable, "-c", "print('contracts valid')"]]
                    },
                    "metrics": {"report": "metrics.json"},
                    "dead_code": {
                        "commands": [[sys.executable, "-c", "print('no dead code')"]]
                    },
                    "mutation": {
                        "test_command": passing_command,
                        "operators": {"==": "!="},
                    },
                },
            )
            report_path = root / "report.html"

            report = gate.run(root, config, report_path, cli_max_mutants=None, notes=[])

            self.assertTrue(report.passed)
            self.assertTrue(report_path.exists())
            rendered = report_path.read_text(encoding="utf-8")
            self.assertIn("Can I ship this?", rendered)
            self.assertIn("READY TO SHIP", rendered)
            self.assertEqual(rendered.count('<article class="gate '), 8)
            self.assertEqual(rendered.count("NOT NEEDED"), 0)
            self.assertEqual(rendered.count("data-copy="), 0)
            self.assertNotIn("Gherkin", rendered)
            self.assertNotIn("Executable UI", rendered)
            self.assertIn("All 1 mutants were killed", rendered)


if __name__ == "__main__":
    unittest.main()
