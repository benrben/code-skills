import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOOP_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "quality_loop.py"
sys.path.insert(0, str(ROOT))

import repo_quality_gate as gate  # noqa: E402


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

            self.assertEqual(len(tools.setup_results), 1)
            command = tools.setup_results[0].command
            self.assertIn("--no-save", command)
            self.assertIn("--package-lock=false", command)
            self.assertIn("@vitest/coverage-v8@4.1.11", command)

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
