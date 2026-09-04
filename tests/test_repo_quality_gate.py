import contextlib
import errno
import http.server
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CORE_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "repo_quality_gate.py"
LOOP_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "quality_loop.py"
REPORT_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "quality_report.py"
INSTALL_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "install.py"


def quality_file(root: Path, name: str) -> Path:
    directory = root / ".quality"
    directory.mkdir(exist_ok=True)
    return directory / name


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = load_script("quality_gate_test_module", CORE_SCRIPT)
quality_loop = load_script("quality_loop_test_module", LOOP_SCRIPT)
quality_report = load_script("quality_report_test_module", REPORT_SCRIPT)
ITEMS_SCRIPT = REPORT_SCRIPT.with_name("quality_items.py")
quality_items = load_script("quality_items_test_module", ITEMS_SCRIPT)
installer = load_script("skill_installer_test_module", INSTALL_SCRIPT)
EMPTY_FAILURES: dict[str, list] = {
    "checks": [],
    "functions": [],
    "files": [],
    "surviving_mutants": [],
    "dependencies": [],
    "tool_setup": [],
}


class QualityGateUnitTests(unittest.TestCase):
    def test_installer_falls_back_to_curl_when_python_https_fails(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=b"skill payload", stderr=b""
        )
        with (
            mock.patch.object(
                installer, "urlopen", side_effect=installer.URLError("certificate")
            ),
            mock.patch.object(installer.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as run,
        ):
            payload = installer.download_file("https://example.test/SKILL.md", 100)

        self.assertEqual(payload, b"skill payload")
        self.assertIn("--max-filesize", run.call_args.args[0])

    def test_installer_resolves_mutable_refs_before_downloading_skill(self) -> None:
        commit = "a" * 40
        with mock.patch.object(
            installer,
            "download_file",
            return_value=commit.encode(),
        ) as download:
            resolved = installer.resolve_reference("refs/heads/main")

        self.assertEqual(resolved, commit)
        self.assertIn("refs%2Fheads%2Fmain", download.call_args.args[0])
        self.assertEqual(
            download.call_args.args[2], {"Accept": "application/vnd.github.sha"}
        )

    def test_skill_package_is_complete_and_runs_without_source_checkout(self) -> None:
        skill_source = ROOT / "skills" / "code-discipline"
        for relative in installer.SKILL_FILES:
            with self.subTest(relative=relative):
                self.assertTrue((skill_source / relative).is_file())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_copy = root / "code-discipline"
            repository = root / "repository"
            shutil.copytree(skill_source, skill_copy)
            repository.mkdir()

            for script in ("repo_quality_gate.py", "quality_loop.py", "install.py"):
                completed = subprocess.run(
                    [sys.executable, str(skill_copy / "scripts" / script), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            initialized = subprocess.run(
                [
                    sys.executable,
                    str(skill_copy / "scripts" / "repo_quality_gate.py"),
                    "--root",
                    str(repository),
                    "--init",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertTrue((repository / ".quality" / "quality-gate.json").is_file())
            thresholds = json.loads(
                (repository / ".quality" / "quality-thresholds.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(thresholds["file_loc"]["max_lines"], 600)

    def test_installer_atomically_installs_the_complete_skill(self) -> None:
        skill_source = ROOT / "skills" / "code-discipline"
        payloads = {
            relative: (skill_source / relative).read_bytes()
            for relative in installer.SKILL_FILES
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, scope_root = installer.repo_target(root)
            repository_config = root / ".quality" / "quality-gate.json"
            repository_config.parent.mkdir()
            repository_config.write_text('{"owned": true}\n', encoding="utf-8")

            versions = installer.install_skill(target, payloads, update=False)
            link = installer.claude_link_for(target, scope_root)
            installer.ensure_claude_link(link, target)

            self.assertEqual(len(versions), 2)
            self.assertTrue(installer.managed_skill(target))
            self.assertEqual(link.resolve(), target.resolve())
            self.assertEqual(
                repository_config.read_text(encoding="utf-8"), '{"owned": true}\n'
            )
            for relative in installer.SKILL_FILES:
                with self.subTest(relative=relative):
                    self.assertEqual(
                        (target / relative).read_bytes(), payloads[relative]
                    )

    def test_installer_replaces_a_managed_symlink_not_its_source(self) -> None:
        skill_source = ROOT / "skills" / "code-discipline"
        payloads = {
            relative: (skill_source / relative).read_bytes()
            for relative in installer.SKILL_FILES
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared-skill"
            target = root / ".agents" / "skills" / "code-discipline"
            shutil.copytree(skill_source, shared)
            marker = shared / "source-marker"
            marker.write_text("preserve\n", encoding="utf-8")
            target.parent.mkdir(parents=True)
            target.symlink_to(shared, target_is_directory=True)

            installer.install_skill(target, payloads, update=True)

            self.assertFalse(target.is_symlink())
            self.assertTrue(installer.managed_skill(target))
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_installer_refuses_to_replace_an_unmanaged_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "code-discipline"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                installer.install_skill(target, {}, update=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_standalone_update_installs_release_and_preserves_repository_goals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "repo_quality_gate.py"
            bundled_thresholds = root / "quality-thresholds.json"
            repository_thresholds = root / ".quality" / "quality-thresholds.json"
            repository_thresholds.parent.mkdir()
            runner.write_text("old runner\n", encoding="utf-8")
            bundled_thresholds.write_text("{}\n", encoding="utf-8")
            repository_thresholds.write_text(
                '{"repository_owned": true}\n', encoding="utf-8"
            )
            remote_runner = CORE_SCRIPT.read_bytes()
            remote_thresholds = (
                ROOT / "skills" / "code-discipline" / "quality-thresholds.json"
            ).read_bytes()

            version = gate.install_standalone_release(
                runner,
                bundled_thresholds,
                remote_runner,
                remote_thresholds,
            )

            self.assertEqual(version, gate.VERSION)
            self.assertEqual(runner.read_bytes(), remote_runner)
            self.assertEqual(bundled_thresholds.read_bytes(), remote_thresholds)
            self.assertEqual(
                repository_thresholds.read_text(encoding="utf-8"),
                '{"repository_owned": true}\n',
            )

    def test_invalid_standalone_update_does_not_replace_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "repo_quality_gate.py"
            bundled_thresholds = root / "quality-thresholds.json"
            runner.write_text("old runner\n", encoding="utf-8")
            bundled_thresholds.write_text("old thresholds\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "downloaded runner"):
                gate.install_standalone_release(
                    runner,
                    bundled_thresholds,
                    b"this is not valid Python\n",
                    b"{}\n",
                )

            self.assertEqual(runner.read_text(encoding="utf-8"), "old runner\n")
            self.assertEqual(
                bundled_thresholds.read_text(encoding="utf-8"),
                "old thresholds\n",
            )

    def test_bundled_thresholds_are_the_runtime_defaults(self) -> None:
        thresholds_path = (
            ROOT / "skills" / "code-discipline" / "quality-thresholds.json"
        )

        thresholds, notes = gate.load_thresholds(thresholds_path)
        config = gate.default_config(thresholds)

        self.assertEqual(thresholds["file_loc"]["max_lines"], 600)
        self.assertEqual(config["file_loc"]["max_lines"], 600)
        self.assertEqual(config["metrics"]["coverage_limit"], 100)
        self.assertEqual(config["metrics"]["complexity_limit"], 6)
        self.assertEqual(config["metrics"]["craap_limit"], 6)
        self.assertEqual(config["flaky_tests"]["runs"], 3)
        self.assertIn(str(thresholds_path), notes[0])

    def test_default_repository_outputs_share_the_quality_folder(self) -> None:
        root = Path("/repository")
        args = quality_loop.parse_args([])
        artifact_dir, html_path, state_path = quality_loop.resolve_artifacts(args, root)

        self.assertEqual(gate.CONFIG_NAME, ".quality/quality-gate.json")
        self.assertEqual(gate.THRESHOLDS_NAME, ".quality/quality-thresholds.json")
        self.assertEqual(gate.DEPENDENCIES_NAME, ".quality/quality-dependencies.json")
        self.assertEqual(gate.DEFAULT_REPORT, ".quality/quality-gate-report.html")
        self.assertEqual(artifact_dir, root / ".quality")
        self.assertEqual(html_path, root / ".quality" / "quality-gate-report.html")
        self.assertEqual(state_path, root / ".quality" / "quality-gate-state.json")

    def test_file_loc_gate_enforces_configured_physical_line_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            short = root / "short.py"
            long = root / "long.py"
            short.write_text("first\nsecond\n", encoding="utf-8")
            long.write_text(
                "\n".join(f"line {index}" for index in range(4)), encoding="utf-8"
            )

            result, files = gate.run_file_loc_gate(
                root,
                [short, long],
                {"max_lines": 3},
            )

            self.assertFalse(result.passed)
            self.assertEqual(
                [(item.path, item.lines, item.limit, item.passed) for item in files],
                [("long.py", 4, 3, False), ("short.py", 2, 3, True)],
            )
            self.assertIn("long.py: 4 lines", result.details)

    def test_root_level_javascript_test_files_are_not_production_loc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            production = root / "index.js"
            production.write_text("export default 1;\n", encoding="utf-8")
            (root / "test.js").write_text(
                "test('value', () => {});\n", encoding="utf-8"
            )
            (root / "test-d.ts").write_text(
                "expectType<number>(1);\n", encoding="utf-8"
            )
            (root / "index.test-d.ts").write_text(
                "expectType<number>(1);\n", encoding="utf-8"
            )

            discovered = gate.discover_source_files(
                root, gate.default_config()["source"]
            )

            self.assertEqual(discovered, [production])

    def test_custom_threshold_file_controls_complexity_and_file_loc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thresholds_path = quality_file(root, "quality-thresholds.json")
            custom = gate.default_thresholds()
            custom["metrics"]["complexity_limit"] = 2
            custom["file_loc"]["max_lines"] = 25
            thresholds_path.write_text(json.dumps(custom), encoding="utf-8")
            config_path = quality_file(root, "quality-gate.json")
            config_path.write_text(
                json.dumps(
                    {
                        "metrics": {"coverage_limit": 1},
                        "file_loc": {"max_lines": 9999},
                    }
                ),
                encoding="utf-8",
            )

            thresholds, _ = gate.load_thresholds(thresholds_path)
            config, _ = gate.load_config(config_path, thresholds)
            function = gate.FunctionMetric(
                path="app.py",
                name="branchy",
                start_line=1,
                end_line=5,
                complexity=3,
                covered_lines=5,
                total_lines=5,
                coverage_percent=100,
                craap_score=3,
                parser="python-ast",
                coverage_limit=config["metrics"]["coverage_limit"],
                complexity_limit=config["metrics"]["complexity_limit"],
                craap_limit=config["metrics"]["craap_limit"],
            )

            self.assertEqual(config["file_loc"]["max_lines"], 25)
            self.assertEqual(config["metrics"]["coverage_limit"], 100)
            self.assertFalse(function.passed)

    def test_init_writes_separate_gate_and_threshold_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CORE_SCRIPT),
                    "--root",
                    str(root),
                    "--init",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            config = json.loads(
                (root / ".quality" / "quality-gate.json").read_text(encoding="utf-8")
            )
            thresholds = json.loads(
                (root / ".quality" / "quality-thresholds.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertEqual(thresholds["file_loc"]["max_lines"], 600)
            self.assertNotIn("coverage_limit", config["metrics"])
            self.assertNotIn("runs", config["flaky_tests"])
            self.assertNotIn("file_loc", config)

    def test_git_scopes_select_commit_and_local_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()

            def git(*arguments: str) -> None:
                subprocess.run(
                    ["git", *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                )

            git("init", "-q")
            git("config", "user.name", "Quality Gate Test")
            git("config", "user.email", "quality-gate@example.test")
            committed = root / "src" / "committed.py"
            staged = root / "src" / "staged.py"
            unstaged = root / "src" / "unstaged.py"
            committed.write_text("def committed():\n    return 1\n", encoding="utf-8")
            staged.write_text("def staged():\n    return 1\n", encoding="utf-8")
            unstaged.write_text("def unstaged():\n    return 1\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "base")

            committed.write_text("def committed():\n    return 2\n", encoding="utf-8")
            git("add", str(committed.relative_to(root)))
            git("commit", "-qm", "change committed source")
            staged.write_text("def staged():\n    return 2\n", encoding="utf-8")
            git("add", str(staged.relative_to(root)))
            unstaged.write_text("def unstaged():\n    return 2\n", encoding="utf-8")
            untracked = root / "src" / "untracked.py"
            untracked.write_text("def untracked():\n    return 3\n", encoding="utf-8")

            commit_scope = gate.commit_scope(root, "HEAD")
            root_commit_scope = gate.commit_scope(root, "HEAD~1")
            local_scope = gate.local_changes_scope(root)
            source_config = gate.default_config()["source"]

            self.assertEqual(commit_scope.kind, "commit")
            self.assertEqual(commit_scope.reference, "HEAD")
            self.assertEqual(commit_scope.paths, ("src/committed.py",))
            self.assertEqual(
                gate.discover_source_files(root, source_config, commit_scope),
                [committed],
            )
            self.assertEqual(
                root_commit_scope.paths,
                ("src/committed.py", "src/staged.py", "src/unstaged.py"),
            )
            self.assertEqual(local_scope.kind, "local_changes")
            self.assertEqual(
                local_scope.paths,
                ("src/staged.py", "src/unstaged.py", "src/untracked.py"),
            )
            self.assertEqual(
                gate.discover_source_files(root, source_config, local_scope),
                [staged, unstaged, untracked],
            )

    def test_incremental_run_filters_metrics_to_changed_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changed = root / "changed.py"
            unchanged = root / "unchanged.py"
            changed.write_text("def changed():\n    return 1\n", encoding="utf-8")
            unchanged.write_text("def unchanged():\n    return 2\n", encoding="utf-8")
            (root / "metrics.json").write_text(
                json.dumps(
                    {
                        "functions": [
                            {
                                "path": "changed.py",
                                "name": "changed",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            },
                            {
                                "path": "unchanged.py",
                                "name": "unchanged",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = gate.default_config()
            config["test"]["command"] = [sys.executable, "-c", "pass"]
            config["metrics"]["report"] = "metrics.json"
            scope = gate.GateScope("local_changes", ("changed.py",))

            report = gate.run(
                root,
                config,
                root / "report.html",
                cli_max_mutants=None,
                notes=[],
                fast=True,
                scope=scope,
            )

            self.assertEqual([item.path for item in report.functions], ["changed.py"])
            self.assertEqual(report.scope, scope)
            self.assertIn("local changes", " ".join(report.notes).lower())

    def test_empty_incremental_source_scope_skips_file_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "metrics-ran"
            config = gate.default_config()
            config["test"]["command"] = [sys.executable, "-c", "pass"]
            config["metrics"]["command"] = [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ]
            config["metrics"]["report"] = "metrics.json"

            report = gate.run(
                root,
                config,
                root / "report.html",
                cli_max_mutants=None,
                notes=[],
                fast=True,
                scope=gate.GateScope("local_changes"),
            )
            quality = next(item for item in report.gates if item.key == "quality")

            self.assertFalse(marker.exists())
            self.assertTrue(quality.passed)
            self.assertIn("No changed production source files", quality.summary)
            self.assertEqual(report.functions, [])

    def test_incremental_mutation_scope_allows_no_supported_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def value():\n    return 1\n", encoding="utf-8")
            config = gate.default_config()
            config["test"]["command"] = [sys.executable, "-c", "pass"]

            result, mutations = gate.run_mutation_gate(
                root,
                config,
                [source],
                cli_max_mutants=None,
                scope=gate.GateScope("local_changes", ("app.py",)),
            )

            self.assertTrue(result.passed)
            self.assertFalse(result.applicable)
            self.assertIn("selected local changes", result.summary)
            self.assertEqual(mutations, [])

    def test_incremental_scope_cli_modes_are_mutually_exclusive(self) -> None:
        core_commit = gate.parse_args(["--commit"])
        core_local = gate.parse_args(["--local-changes"])
        wrapper_commit = quality_loop.parse_args(["--commit", "HEAD~1"])
        wrapper_local = quality_loop.parse_args(["--local-changes"])

        self.assertEqual(core_commit.commit, "HEAD")
        self.assertFalse(core_commit.local_changes)
        self.assertIsNone(core_local.commit)
        self.assertTrue(core_local.local_changes)
        self.assertEqual(wrapper_commit.commit, "HEAD~1")
        self.assertFalse(wrapper_commit.local_changes)
        self.assertIsNone(wrapper_local.commit)
        self.assertTrue(wrapper_local.local_changes)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate.parse_args(["--commit", "--local-changes"])
            with self.assertRaises(SystemExit):
                quality_loop.parse_args(["--commit", "--local-changes"])

    def test_readme_keeps_the_primary_workflow_short_and_discoverable(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        core_help = subprocess.run(
            [sys.executable, str(CORE_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout

        self.assertLessEqual(len(readme.splitlines()), 150)
        for option in ("--init", "--local-changes", "--fast", "--html"):
            with self.subTest(option=option):
                self.assertIn(option, readme)
        for command_fragment in (
            "contents/skills/code-discipline/scripts/install.py?ref=main",
            "| python3 - --repo --root .",
            "| python3 - --global",
            "scripts/install.py --update-current",
            '"$HOME/.agents/skills/code-discipline/scripts/install.py" --update-current',
            ".agents/skills/code-discipline/scripts/quality_loop.py --root .",
            "Every run writes `.quality/quality-gate-report.html`",
        ):
            with self.subTest(command_fragment=command_fragment):
                self.assertIn(command_fragment, readme)
        self.assertNotIn("git submodule", readme)
        self.assertNotIn("git clone", readme)
        self.assertNotIn(".code-skills", readme)
        self.assertIn("repository-setup.md", readme)
        self.assertIn("--update-from-github [REF]", core_help)

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

    def test_failing_report_has_one_repair_action_and_optional_setup_prompts(
        self,
    ) -> None:
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
                gate.GateResult(
                    "types",
                    "Static type checking",
                    True,
                    "Not applicable: no type checker was detected.",
                    ["Configure the repository's complete static type checker."],
                    applicable=False,
                ),
                gate.GateResult(
                    "dead_code",
                    "Dead code",
                    True,
                    "Not applicable: no unused-code detector was detected.",
                    ["Configure a high-confidence unused-code detector."],
                    applicable=False,
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

        self.assertEqual(rendered.count("Copy repair prompt"), 1)
        self.assertNotIn("Copy complete prompt", rendered)
        self.assertNotIn("Prefer smaller tasks?", rendered)
        self.assertEqual(rendered.count("Copy install prompt"), 2)
        self.assertEqual(rendered.count("Copy all install prompts"), 1)
        self.assertIn("Add optional checks", rendered)
        self.assertIn("Static type checking", rendered)
        self.assertIn("Dead code", rendered)
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
        self.assertIn(
            '<span class="check-status">DEFERRED</span>', gate.html_report(report)
        )
        self.assertIn("READY FOR FULL RUN", gate.html_report(report))

    def test_metrics_run_diagnostically_when_baseline_tests_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def value():\n    return 1\n", encoding="utf-8")
            metrics_payload = json.dumps(
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
            )
            config = gate.default_config()
            config["test"]["command"] = [
                sys.executable,
                "-c",
                "raise SystemExit(1)",
            ]
            config["metrics"]["command"] = [
                sys.executable,
                "-c",
                (
                    "import pathlib, sys; "
                    "pathlib.Path(sys.argv[1]).write_text(sys.argv[2]); "
                    "raise SystemExit(1)"
                ),
                "{report}",
                metrics_payload,
            ]
            config["metrics"]["report"] = "{report}"

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
            self.assertIn("report was parsed diagnostically", quality.summary)
            self.assertEqual(len(report.functions), 1)
            self.assertEqual(report.functions[0].coverage_percent, 100)
            self.assertEqual(quality.command_results[0].returncode, 1)

    def test_failed_coverage_command_preserves_its_diagnostic_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def value():\n    return 1\n", encoding="utf-8")
            coverage_payload = json.dumps(
                {
                    "files": {
                        str(source): {
                            "executed_lines": [1, 2],
                            "missing_lines": [],
                        }
                    }
                }
            )
            config = gate.default_config()
            config["test"]["command"] = [
                sys.executable,
                "-c",
                "raise SystemExit(1)",
            ]
            config["metrics"]["coverage_commands"] = [
                [sys.executable, "-c", "raise SystemExit(1)"],
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib, sys; "
                        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
                    ),
                    "{report}",
                    coverage_payload,
                ],
            ]
            config["metrics"]["coverage_report"] = "{report}"
            config["metrics"]["coverage_format"] = "coverage-json"

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
            self.assertIn("report was parsed diagnostically", quality.summary)
            self.assertEqual(len(report.functions), 1)
            self.assertEqual(report.functions[0].coverage_percent, 100)
            self.assertIn(1, [result.returncode for result in quality.command_results])

    def test_failed_coverage_command_rejects_a_stale_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text("def value():\n    return 1\n", encoding="utf-8")
            coverage_path = root / "coverage.json"
            coverage_path.write_text(
                json.dumps(
                    {
                        "files": {
                            str(source): {
                                "executed_lines": [1, 2],
                                "missing_lines": [],
                            }
                        }
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
            config["metrics"]["coverage_commands"] = [
                [sys.executable, "-c", "raise SystemExit(1)"]
            ]
            config["metrics"]["coverage_report"] = str(coverage_path)
            config["metrics"]["coverage_format"] = "coverage-json"

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
            self.assertIn("did not produce a fresh report", quality.summary)
            self.assertEqual(report.functions, [])

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
                mock.patch.object(
                    gate, "node_package_version", side_effect=package_version
                ),
                mock.patch.object(gate, "executable", return_value="npm"),
                mock.patch.object(gate, "run_command", return_value=install) as runner,
            ):
                tools = gate.bootstrap_tools(root, config, [source])

            install_command = runner.call_args.args[0]
            self.assertIn("@stryker-mutator/core@9.6.1", install_command)
            self.assertIn("@stryker-mutator/vitest-runner@9.6.1", install_command)
            self.assertIn("--no-save", install_command)
            self.assertEqual(
                tools.stryker_command, [str(root / "node_modules/.bin/stryker")]
            )

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

    def test_stryker_config_uses_a_dedicated_vitest_unit_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/app.ts"
            source.parent.mkdir()
            source.write_text("export const value = 1\n", encoding="utf-8")
            (root / "tests/unit").mkdir(parents=True)
            (root / "vitest.mutation.config.ts").write_text(
                "export default {}\n", encoding="utf-8"
            )

            config = gate.stryker_config(
                root,
                [source],
                root / "mutation.json",
                root / "incremental.json",
                workers=6,
                mutation_config={
                    "test_files": ["tests/unit/**/*.test.ts"],
                    "vitest_dir": "tests/unit",
                    "vitest_related": True,
                },
            )

            self.assertEqual(config["testFiles"], ["tests/unit/**/*.test.ts"])
            self.assertEqual(
                config["vitest"],
                {
                    "related": True,
                    "configFile": "vitest.mutation.config.ts",
                    "dir": "tests/unit",
                },
            )
            self.assertEqual(config["concurrency"], 6)

    def test_stryker_config_rejects_dedicated_paths_outside_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "repo"
            source = root / "src/app.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 1\n", encoding="utf-8")
            outside = parent / "outside-vitest.ts"
            outside.write_text("export default {}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must stay inside"):
                gate.stryker_config(
                    root,
                    [source],
                    root / "mutation.json",
                    root / "incremental.json",
                    workers=2,
                    mutation_config={"vitest_config": str(outside)},
                )

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
                            "static": True,
                            "duration": 1.25,
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
        self.assertTrue(mutations[0].static)
        self.assertEqual(mutations[0].duration_seconds, 1.25)

        baseline = gate.CommandResult(["npm", "test"], 0, "green", 0.1)
        native_run = gate.CommandResult(["stryker", "run"], 1, "done", 12.5)
        result, _ = gate.finish_stryker_mutation_gate(
            mutations,
            baseline,
            native_run,
            workers=6,
            cache_summary="; initialized cache",
        )
        self.assertIn("1 static mutant", result.summary)
        self.assertIn("12.50s", result.summary)

    def test_exact_stryker_proof_cache_requires_matching_content_fingerprint(
        self,
    ) -> None:
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

            cached = gate.load_stryker_proof_cache(incremental, metadata, "matching")
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

    def test_stryker_environment_fingerprint_changes_with_dedicated_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config/mutation-vitest.ts"
            config_path.parent.mkdir()
            config_path.write_text("export default { test: {} }\n", encoding="utf-8")
            mutation_config = {"vitest_config": "config/mutation-vitest.ts"}

            original = gate.stryker_environment_fingerprint(root, mutation_config)
            config_path.write_text(
                "export default { test: { environment: 'node' } }\n",
                encoding="utf-8",
            )
            changed = gate.stryker_environment_fingerprint(root, mutation_config)

        self.assertNotEqual(original, changed)

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

    def test_craap_state_keeps_enough_precision_to_explain_failure(self) -> None:
        score = gate.craap_score(6, 99.9)
        function = gate.FunctionMetric(
            path="app.py",
            name="almost_covered",
            start_line=1,
            end_line=2,
            complexity=6,
            covered_lines=999,
            total_lines=1000,
            coverage_percent=99.9,
            craap_score=score,
            parser="python-ast",
        )

        self.assertGreater(score, 6)
        self.assertFalse(function.passed)
        self.assertGreater(gate.function_measurement(function)["craap_score"], 6)

    def test_normalized_adapter_cannot_override_craap_formula(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "metrics.json"
            report.write_text(
                json.dumps(
                    {
                        "functions": [
                            {
                                "path": "app.py",
                                "name": "choose",
                                "complexity": 4,
                                "coverage_percent": 50,
                                "craap_score": 999,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            functions = gate.load_normalized_metrics(report, root)

        self.assertEqual(functions[0].craap_score, 6)

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

    def test_python_complexity_counts_lambda_and_comprehension_paths(self) -> None:
        source = """\
def mapped(values):
    return list(map(lambda value: value if value and value > 1 else 0, values))

def flattened(rows):
    return [value for row in rows for value in row if value > 0 if value % 2]
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_text(source, encoding="utf-8")
            functions = gate.parse_python_functions(path)

        by_name = {name: complexity for name, _, _, complexity, _ in functions}
        self.assertEqual(by_name["mapped"], 3)
        self.assertEqual(by_name["flattened"], 5)

    def test_python_match_does_not_count_default_and_counts_or_patterns(self) -> None:
        source = """\
def default_only(value):
    match value:
        case _:
            return 0

def alternatives(value):
    match value:
        case 1 | 2:
            return 1
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.py"
            path.write_text(source, encoding="utf-8")
            functions = gate.parse_python_functions(path)

        by_name = {name: complexity for name, _, _, complexity, _ in functions}
        self.assertEqual(by_name["default_only"], 1)
        self.assertEqual(by_name["alternatives"], 3)

    def test_nested_function_lines_belong_only_to_nested_coverage(self) -> None:
        source = """\
def outer(value):
    def inner(flag):
        if flag:
            return 1
    if value:
        return inner(value)
    return 0
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.py"
            path.write_text(source, encoding="utf-8")
            coverage = {
                "sample.py": {
                    1: 1,
                    2: 1,
                    3: 0,
                    4: 0,
                    5: 1,
                    6: 1,
                    7: 1,
                }
            }

            functions = gate.build_function_metrics([path], root, coverage)

        by_name = {function.name: function for function in functions}
        self.assertEqual(by_name["outer"].coverage_percent, 100)
        self.assertEqual(by_name["outer"].total_lines, 4)
        self.assertAlmostEqual(by_name["outer.inner"].coverage_percent, 100 / 3)
        self.assertEqual(by_name["outer.inner"].total_lines, 3)

    def test_python_stubs_skip_coverage_but_concrete_pass_does_not(self) -> None:
        source = """\
from abc import abstractmethod
from typing import overload

class Service:
    @abstractmethod
    def required(self):
        ...

@overload
def convert(value: int) -> str: ...

def no_op():
    pass
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sample.py"
            path.write_text(source, encoding="utf-8")
            coverage = {"sample.py": {6: 1, 7: 0, 10: 1, 12: 1, 13: 0}}

            functions = gate.build_function_metrics([path], root, coverage)

        by_name = {function.name: function for function in functions}
        self.assertFalse(by_name["Service.required"].coverage_measured)
        self.assertTrue(by_name["Service.required"].passed)
        self.assertFalse(by_name["convert"].coverage_measured)
        self.assertTrue(by_name["convert"].passed)
        self.assertTrue(by_name["no_op"].coverage_measured)
        self.assertFalse(by_name["no_op"].passed)

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

            with mock.patch.object(gate, "run_command", side_effect=fake_run_command):
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

    def test_native_vitest_mutation_reuses_completed_proof_without_a_runner(
        self,
    ) -> None:
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
                stryker_options = json.loads(
                    Path(command[-1]).read_text(encoding="utf-8")
                )
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
            self.assertEqual(gate.resolve_stryker_workers("auto"), 11)
        with mock.patch.object(gate.os, "cpu_count", return_value=4):
            self.assertEqual(gate.resolve_stryker_workers("auto"), 4)
        with mock.patch.object(gate.os, "cpu_count", return_value=12):
            self.assertEqual(gate.resolve_mutation_workers("auto", 4), 1)
            self.assertEqual(gate.resolve_mutation_workers("auto", 20), 2)
            self.assertEqual(gate.resolve_mutation_workers("auto", 40), 4)
        self.assertEqual(gate.resolve_mutation_workers("8", 3), 3)
        for invalid in (0, "0", "many", True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    gate.resolve_mutation_workers(invalid, 10)

    def test_quality_loop_rejects_a_concurrent_run_for_the_same_repository(
        self,
    ) -> None:
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
            (quality_file(root, "quality-gate.json")).write_text(
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
            (quality_file(root, "quality-dependencies.json")).write_text(
                json.dumps(rules), encoding="utf-8"
            )
            config = gate.default_config()

            result, violations = gate.run_dependency_gate(
                root, config, [source, target]
            )
            scoped_result, scoped_violations = gate.run_dependency_gate(
                root,
                config,
                [source],
                resolution_files=[source, target],
            )

            self.assertFalse(result.passed)
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0].source_module, "domain")
            self.assertEqual(violations[0].target_module, "infrastructure")
            self.assertFalse(scoped_result.passed)
            self.assertEqual(len(scoped_violations), 1)
            self.assertEqual(scoped_violations[0].source, "src/domain/service.py")

    def test_installer_validates_refs_urls_and_download_failures(self) -> None:
        self.assertEqual(installer.validate_ref("refs/tags/v1.0.0"), "refs/tags/v1.0.0")
        self.assertIn(
            "refs/tags/v1.0.0", installer.raw_url("refs/tags/v1.0.0", "SKILL.md")
        )
        for invalid in ("", "/main", "refs/../main", "main branch"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(installer.argparse.ArgumentTypeError):
                    installer.validate_ref(invalid)

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"ok"
        with mock.patch.object(installer, "urlopen", return_value=response):
            self.assertEqual(installer.download_file("https://example.test", 2), b"ok")
        response.read.assert_called_once_with(3)

        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not download"):
                installer.download_file("https://example.test")

        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value="curl"),
            mock.patch.object(installer.subprocess, "run", side_effect=OSError("curl")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python HTTPS or curl"):
                installer.download_file("https://example.test")

        failed = subprocess.CompletedProcess(["curl"], 22, b"", b"denied")
        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value="curl"),
            mock.patch.object(installer.subprocess, "run", return_value=failed),
        ):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                installer.download_file("https://example.test")

        response.read.return_value = b"too large"
        with mock.patch.object(installer, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                installer.download_file("https://example.test", 2)

    def test_installer_rejects_invalid_metadata_and_payload_sets(self) -> None:
        with mock.patch.object(installer, "download_file", return_value=b"\xff"):
            with self.assertRaisesRegex(RuntimeError, "invalid ref metadata"):
                installer.resolve_reference("main")
        with mock.patch.object(installer, "download_file", return_value=b"short"):
            with self.assertRaisesRegex(RuntimeError, "did not resolve"):
                installer.resolve_reference("main")

        with mock.patch.object(
            installer, "download_file", return_value=b"payload"
        ) as download:
            payloads = installer.download_skill("a" * 40)
        self.assertEqual(set(payloads), set(installer.SKILL_FILES))
        self.assertEqual(download.call_count, len(installer.SKILL_FILES))

        with self.assertRaisesRegex(RuntimeError, "missing"):
            installer.validate_skill_payloads({})
        unknown_payloads = {relative: b"" for relative in installer.SKILL_FILES}
        unknown_payloads["unknown"] = b""
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            installer.validate_skill_payloads(unknown_payloads)
        installer.validate_skill_payloads(
            {relative: b"" for relative in installer.SKILL_FILES}
        )

    def test_installer_validates_each_staged_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text("invalid", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not code-discipline"):
                installer.validate_skill_marker(root)
            (root / "SKILL.md").write_text(
                "---\nname: code-discipline\n---\n", encoding="utf-8"
            )
            installer.validate_skill_marker(root)

            (root / "quality-thresholds.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "thresholds"):
                installer.validate_staged_thresholds(root)
            (root / "quality-thresholds.json").write_text(
                '{"schema_version":2}', encoding="utf-8"
            )
            installer.validate_staged_thresholds(root)

            for relative in installer.PYTHON_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pass\n", encoding="utf-8")
            (root / installer.PYTHON_FILES[0]).write_text("if", encoding="utf-8")
            with self.assertRaises(SyntaxError):
                installer.validate_staged_python(root)
            (root / installer.PYTHON_FILES[0]).write_text("pass\n", encoding="utf-8")
            installer.validate_staged_python(root)

            failed = subprocess.CompletedProcess([], 1, "", "broken")
            with mock.patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "failed validation"):
                    installer.staged_versions(root)
            passed = subprocess.CompletedProcess([], 0, "1.0\n", "")
            with mock.patch.object(installer.subprocess, "run", return_value=passed):
                self.assertEqual(installer.staged_versions(root), ("1.0", "1.0"))

    def test_installer_restores_backup_after_partial_swap_failure(self) -> None:
        payloads = {
            relative: (ROOT / "skills" / "code-discipline" / relative).read_bytes()
            for relative in installer.SKILL_FILES
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "code-discipline"
            shutil.copytree(ROOT / "skills" / "code-discipline", target)
            marker = target / "marker"
            marker.write_text("original", encoding="utf-8")
            real_replace = installer.os.replace
            calls = 0

            def fail_second_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("staged replace failed")
                return real_replace(source, destination)

            with mock.patch.object(
                installer.os, "replace", side_effect=fail_second_replace
            ):
                with self.assertRaisesRegex(OSError, "staged replace failed"):
                    installer.install_skill(target, payloads, update=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_installer_path_link_destination_and_output_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            self.assertFalse(installer.managed_skill(missing))
            installer.remove_path(missing)
            directory = root / "directory"
            directory.mkdir()
            installer.remove_path(directory)
            self.assertFalse(directory.exists())

            managed = root / "managed"
            managed.mkdir()
            (managed / "SKILL.md").write_text(
                "---\nname: code-discipline\n---\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                installer.validate_install_target(managed, update=False)

            link = root / "link"
            installer.ensure_claude_link(link, managed)
            installer.ensure_claude_link(link, managed)
            installer.validate_claude_link(link, managed)
            conflict = root / "conflict"
            conflict.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "refusing"):
                installer.validate_claude_link(conflict, managed)

            update_args = SimpleNamespace(update_current=True, repo=False, root=".")
            self.assertTrue(installer.install_destination(update_args)[1])
            repo_args = SimpleNamespace(update_current=False, repo=True, root=str(root))
            self.assertEqual(
                installer.install_destination(repo_args)[0],
                installer.repo_target(root)[0],
            )
            global_args = SimpleNamespace(update_current=False, repo=False, root=".")
            with mock.patch.object(installer.Path, "home", return_value=root):
                self.assertEqual(
                    installer.install_destination(global_args)[0],
                    installer.global_target()[0],
                )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                installer.print_install_result(managed, False, link, "4", "3")
                installer.print_install_result(managed, True, None, "4", "3")
            self.assertIn("Installed", output.getvalue())
            self.assertIn("Updated", output.getvalue())
            self.assertIn("Claude link", output.getvalue())

    def test_installer_main_reports_success_and_failure(self) -> None:
        result = (Path("target"), False, None, "4", "3")
        payloads = {installer.INSTALLER_FILE: b"fresh"}
        with (
            mock.patch.object(installer, "resolve_reference", return_value="a" * 40),
            mock.patch.object(installer, "download_skill", return_value=payloads),
            mock.patch.object(
                installer, "install_from_args", return_value=result
            ) as install,
        ):
            self.assertEqual(installer.main(["--repo"]), 0)
        install.assert_called_once()
        self.assertIs(install.call_args.args[1], payloads)
        with (
            mock.patch.object(installer, "resolve_reference", return_value="a" * 40),
            mock.patch.object(installer, "download_skill", return_value=payloads),
            mock.patch.object(
                installer, "install_from_args", side_effect=RuntimeError("broken")
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(installer.main(["--repo"]), 2)
        self.assertIn("broken", stderr.getvalue())

    def test_installer_update_hands_off_to_the_downloaded_installer(self) -> None:
        commit = "b" * 40
        current = installer.current_installer().read_bytes()
        same = {installer.INSTALLER_FILE: current}
        newer = {installer.INSTALLER_FILE: current + b"\n# newer\n"}
        update = SimpleNamespace(update_current=True)
        fresh = SimpleNamespace(update_current=False)
        self.assertFalse(installer.handoff_needed(fresh, newer))
        self.assertFalse(installer.handoff_needed(update, same))
        self.assertTrue(installer.handoff_needed(update, newer))
        with mock.patch.dict(os.environ, {installer.HANDOFF_ENV: "1"}):
            self.assertFalse(installer.handoff_needed(update, newer))

        with mock.patch.dict(os.environ, {installer.UPDATE_TARGET_ENV: "/handed"}):
            self.assertEqual(installer.update_target(), Path("/handed"))
        with mock.patch.dict(os.environ, {installer.UPDATE_TARGET_ENV: ""}):
            self.assertEqual(
                installer.update_target(), installer.current_installer().parent.parent
            )

        with (
            mock.patch.object(installer, "resolve_reference", return_value=commit),
            mock.patch.object(installer, "download_skill", return_value=newer),
            mock.patch.object(
                installer, "run_downloaded_installer", return_value=7
            ) as handoff,
            mock.patch.object(installer, "install_from_args") as install,
        ):
            self.assertEqual(installer.main(["--update-current"]), 7)
        install.assert_not_called()
        handoff.assert_called_once_with(
            newer[installer.INSTALLER_FILE], installer.update_target(), commit
        )

    def test_installer_runs_the_downloaded_installer_with_the_update_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            script = (
                "import json, os, sys\n"
                f"json.dump({{'argv': sys.argv[1:], 'handoff': "
                f"os.environ.get({installer.HANDOFF_ENV!r}), 'target': "
                f"os.environ.get({installer.UPDATE_TARGET_ENV!r})}}, "
                f"open({str(report)!r}, 'w'))\n"
                "sys.exit(3)\n"
            ).encode()
            code = installer.run_downloaded_installer(
                script, Path(temporary) / "skill", "c" * 40
            )
            self.assertEqual(code, 3)
            recorded = json.loads(report.read_text())
        self.assertEqual(recorded["argv"], ["--update-current", "--ref", "c" * 40])
        self.assertEqual(recorded["handoff"], "1")
        self.assertEqual(recorded["target"], str(Path(temporary) / "skill"))
        with self.assertRaises(SyntaxError):
            installer.run_downloaded_installer(b"def (", Path(temporary), "c" * 40)

    def test_quality_loop_platform_lock_and_owner_paths(self) -> None:
        with tempfile.TemporaryFile(mode="w+") as handle:
            locking = mock.Mock()
            windows_module = SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2)
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows_module}),
            ):
                self.assertTrue(quality_loop.try_lock(handle))
                handle.seek(0, os.SEEK_END)
                self.assertEqual(handle.tell(), 1)
                quality_loop.unlock(handle)
            self.assertEqual(locking.call_count, 2)

            denied = OSError(errno.EACCES, "locked")
            windows_module.locking = mock.Mock(side_effect=denied)
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows_module}),
            ):
                self.assertFalse(quality_loop.try_lock(handle))

            unexpected = OSError(errno.EINVAL, "invalid")
            windows_module.locking = mock.Mock(side_effect=unexpected)
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows_module}),
            ):
                with self.assertRaises(OSError):
                    quality_loop.try_lock(handle)

            with mock.patch("fcntl.flock", side_effect=BlockingIOError):
                self.assertFalse(quality_loop.try_lock(handle))

            handle.seek(0)
            handle.truncate()
            self.assertEqual(
                quality_loop.lock_owner(handle), "owner details unavailable"
            )
            handle.seek(0)
            handle.write("not-json")
            handle.flush()
            self.assertEqual(quality_loop.lock_owner(handle), "not-json")
            handle.seek(0)
            handle.truncate()
            json.dump({"pid": 7, "started_at": "now"}, handle)
            handle.flush()
            self.assertEqual(quality_loop.lock_owner(handle), "PID 7, started now")

    def test_quality_loop_rerun_and_path_helpers_cover_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            command = ["python"]
            quality_loop.append_rerun_configuration(
                command,
                root,
                root / "config.json",
                True,
                root / "thresholds.json",
                True,
                root / "artifacts",
                False,
                root / "report.html",
                True,
                root / "gate.py",
                True,
            )
            artifact_command = ["python"]
            quality_loop.append_rerun_configuration(
                artifact_command,
                root,
                root / "config.json",
                False,
                root / "thresholds.json",
                False,
                root / "artifacts",
                True,
                root / "report.html",
                False,
                root / "gate.py",
                False,
            )
            quality_loop.append_rerun_execution(
                command, ["--commit", "HEAD"], True, True, "auto"
            )
            quality_loop.append_rerun_execution(
                artifact_command, [], False, False, None
            )
            self.assertIn("--html", command)
            self.assertIn("--artifact-dir", artifact_command)
            self.assertIn("--mutation-workers", command)

            self.assertEqual(
                quality_loop.resolve_from_root(None, root, root / "default"),
                root / "default",
            )
            self.assertEqual(
                quality_loop.resolve_from_root("relative", root, root / "default"),
                root / "relative",
            )
            self.assertEqual(
                quality_loop.resolve_from_root(str(root), root, root / "default"), root
            )
            self.assertEqual(quality_loop.display_path(root / "a", root), "a")
            self.assertEqual(
                quality_loop.display_path(Path("/outside"), root), "/outside"
            )

            html_args = SimpleNamespace(html="custom.html", artifact_dir=None)
            artifact_args = SimpleNamespace(html=None, artifact_dir="out")
            self.assertEqual(
                quality_loop.resolve_artifacts(html_args, root)[1],
                root / "custom.html",
            )
            self.assertEqual(
                quality_loop.resolve_artifacts(artifact_args, root)[0], root / "out"
            )

    def test_quality_loop_serializes_every_state_variant(self) -> None:
        command = SimpleNamespace(
            command=["check"],
            returncode=1,
            timed_out=False,
            duration_seconds=1.2345,
        )
        gates = [
            SimpleNamespace(
                key="deferred", deferred=True, applicable=True, passed=False
            ),
            SimpleNamespace(key="na", deferred=False, applicable=False, passed=True),
            SimpleNamespace(key="pass", deferred=False, applicable=True, passed=True),
            SimpleNamespace(
                key="quality",
                title="Quality",
                deferred=False,
                applicable=True,
                passed=False,
                summary="failed",
                details=["detail"],
                command_results=[command],
            ),
        ]
        for item in gates[:3]:
            item.title = item.key
            item.summary = item.key
            item.details = []
            item.command_results = []
        self.assertEqual(
            [quality_loop.gate_status(item) for item in gates],
            ["deferred", "not_applicable", "pass", "fail"],
        )
        self.assertEqual(quality_loop.command_state(command)["duration_seconds"], 1.234)

        function = SimpleNamespace(
            path="a.py",
            name="f",
            start_line=1,
            covered_lines=0,
            total_lines=1,
            coverage_percent=0.0,
            complexity=1,
            craap_score=2.0,
            passed=False,
        )
        file_metric = SimpleNamespace(path="a.py", lines=2, limit=1, passed=False)
        mutation = SimpleNamespace(
            mutant_id="m",
            path="a.py",
            line=1,
            column=1,
            original="==",
            replacement="!=",
            status="",
            survived=True,
            static=True,
        )
        violation = SimpleNamespace(
            source="a.py",
            line=1,
            source_module="a",
            target="b.py",
            target_module="b",
            rule="deny",
        )
        setup = SimpleNamespace(command=["setup"], returncode=1, stdout="x" * 5000)
        scope = SimpleNamespace(
            incremental=False, kind="repository", reference=None, paths=()
        )
        analysis = SimpleNamespace(
            functions=[function],
            files=[file_metric],
            mutations=[mutation],
            dependency_violations=[violation],
            tool_setup=[setup],
            gates=gates,
            passed=False,
            ready_for_full=False,
            scope=scope,
            mode="full",
            root="/repo",
            generated_at="now",
            rerun_command="run --fast",
            thresholds={},
        )
        fake_gate = SimpleNamespace(
            without_fast_flag=lambda value: value.replace(" --fast", ""),
            master_fix_prompt=lambda value: "fix",
        )
        state = quality_loop.analysis_state(
            fake_gate, analysis, Path("report"), Path("state"), 1
        )
        self.assertEqual(state["status"], "fail")
        self.assertEqual(state["counts"]["checks_executed"], 3)
        self.assertEqual(state["counts"]["checks_applicable"], 2)
        self.assertEqual(state["counts"]["mutants_static"], 1)
        self.assertEqual(state["failures"]["tool_setup"][0]["output"], "x" * 4000)
        self.assertEqual(state["fix_prompt"], "fix")
        self.assertEqual(quality_loop.state_status(analysis, "broken"), "error")
        analysis.passed = True
        self.assertEqual(quality_loop.state_status(analysis, None), "pass")
        self.assertIsNone(quality_loop.state_fix_prompt(fake_gate, analysis))
        analysis.passed = False
        analysis.ready_for_full = True
        self.assertEqual(quality_loop.state_status(analysis, None), "ready_for_full")

    def test_quality_loop_scope_loading_execution_and_output_helpers(self) -> None:
        commit_args = SimpleNamespace(commit="HEAD", local_changes=False)
        local_args = SimpleNamespace(commit=None, local_changes=True)
        repo_args = SimpleNamespace(commit=None, local_changes=False)
        self.assertEqual(
            quality_loop.scope_cli_arguments(commit_args), ["--commit", "HEAD"]
        )
        self.assertEqual(
            quality_loop.scope_cli_arguments(local_args), ["--local-changes"]
        )
        self.assertEqual(quality_loop.scope_cli_arguments(repo_args), [])
        self.assertEqual(quality_loop.requested_scope(commit_args, gate).kind, "commit")
        self.assertEqual(
            quality_loop.requested_scope(local_args, gate).kind, "local_changes"
        )
        self.assertEqual(
            quality_loop.requested_scope(repo_args, gate).kind, "repository"
        )

        with mock.patch.object(quality_loop, "load_gate", return_value=gate):
            self.assertIs(quality_loop.load_gate_safely(CORE_SCRIPT), gate)
        with (
            mock.patch.object(
                quality_loop, "load_gate", side_effect=RuntimeError("bad")
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertIsNone(quality_loop.load_gate_safely(CORE_SCRIPT))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                commit=None,
                local_changes=False,
                no_install=True,
                max_mutants=None,
                fast=False,
                mutation_workers="auto",
            )
            analysis = SimpleNamespace(passed=True, rerun_command=None)
            with (
                mock.patch.object(
                    gate, "sync_thresholds_file", return_value=["synced"]
                ),
                mock.patch.object(
                    gate, "load_thresholds", return_value=({}, ["loaded"])
                ),
                mock.patch.object(
                    gate,
                    "load_config",
                    return_value=({"tools": {"auto_install": True}}, []),
                ),
                mock.patch.object(gate, "run", return_value=analysis) as run,
            ):
                result, exit_code, error = quality_loop.execute_analysis(
                    args,
                    gate,
                    root,
                    root / "config.json",
                    root / "thresholds.json",
                    root / "report.html",
                    "rerun",
                )
            self.assertIs(result, analysis)
            self.assertEqual(exit_code, 0)
            self.assertIsNone(error)
            self.assertEqual(analysis.rerun_command, "rerun")
            self.assertEqual(run.call_args.args[4], ["synced", "loaded"])

            args.no_install = False
            with (
                mock.patch.object(
                    gate, "sync_thresholds_file", return_value=["synced"]
                ),
                mock.patch.object(
                    gate, "load_thresholds", return_value=({}, ["loaded"])
                ),
                mock.patch.object(
                    gate,
                    "load_config",
                    return_value=({"tools": {"auto_install": True}}, []),
                ),
                mock.patch.object(gate, "run", return_value=analysis),
            ):
                _, exit_code, error = quality_loop.execute_analysis(
                    args,
                    gate,
                    root,
                    root / "config.json",
                    root / "thresholds.json",
                    root / "report.html",
                    "rerun",
                )
            self.assertEqual(exit_code, 0)
            self.assertIsNone(error)

            with (
                mock.patch.object(gate, "sync_thresholds_file", return_value=[]),
                mock.patch.object(
                    gate, "load_thresholds", side_effect=ValueError("bad")
                ),
            ):
                result, exit_code, error = quality_loop.execute_analysis(
                    args,
                    gate,
                    root,
                    root / "config.json",
                    root / "thresholds.json",
                    root / "report.html",
                    "rerun",
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(error, "bad")
            self.assertEqual(result.mode, "full")

        output = io.StringIO()
        summary_gate = SimpleNamespace(
            gate_outcome=lambda item: "PASS", gate_status=lambda item: "pass"
        )
        summary_analysis = SimpleNamespace(
            gates=[SimpleNamespace(title="One", summary="ok", details=[])],
            mode="full",
            selection=(),
            scope=SimpleNamespace(description="the entire repository"),
            thresholds={},
            functions=[],
        )
        summary_state = {
            "status": "pass",
            "fix_prompt": "fix",
            "rerun_command": "run",
            "full_rerun_command": "run",
            "failures": EMPTY_FAILURES,
        }
        with contextlib.redirect_stdout(output):
            quality_report.print_report(
                summary_gate,
                summary_analysis,
                summary_state,
                Path("state"),
                Path("report"),
                True,
            )
        self.assertIn("QUALITY_LOOP=PASS", output.getvalue())
        self.assertIn("Ship report is green", output.getvalue())

    def test_quality_loop_atomic_write_cleanup_and_main_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "state.json"
            quality_loop.write_json_atomic(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            with mock.patch.object(
                quality_loop.os, "replace", side_effect=OSError("replace")
            ):
                with self.assertRaisesRegex(OSError, "replace"):
                    quality_loop.write_json_atomic(path, {"ok": False})
            self.assertEqual(list(root.glob("*.tmp")), [])

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    quality_loop.main(["--root", str(root / "missing")]), 2
                )
            self.assertIn("does not exist", stderr.getvalue())

    def test_installer_remaining_restore_link_and_orchestration_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            backup = root / "backup"
            installer.restore_install_backup(target, backup, False)

            target.mkdir()
            backup.mkdir()
            (backup / "marker").write_text("restored", encoding="utf-8")
            installer.restore_install_backup(target, backup, True)
            self.assertEqual(
                (target / "marker").read_text(encoding="utf-8"), "restored"
            )

            missing_link = root / "missing-link"
            installer.validate_claude_link(missing_link, target)

            linked_result = ("4", "3")
            repo_args = SimpleNamespace(
                update_current=False, repo=True, root=str(root), ref="main"
            )
            with (
                mock.patch.object(
                    installer,
                    "install_destination",
                    return_value=(target, False, missing_link),
                ),
                mock.patch.object(installer, "validate_claude_link") as validate,
                mock.patch.object(
                    installer, "install_skill", return_value=linked_result
                ) as install,
                mock.patch.object(installer, "ensure_claude_link") as ensure,
            ):
                result = installer.install_from_args(repo_args, {})
            self.assertEqual(result, (target, False, missing_link, "4", "3"))
            validate.assert_called_once_with(missing_link, target)
            install.assert_called_once_with(target, {}, False)
            ensure.assert_called_once_with(missing_link, target)

            update_args = SimpleNamespace(
                update_current=True, repo=False, root=".", ref="main"
            )
            with (
                mock.patch.object(
                    installer,
                    "install_destination",
                    return_value=(target, True, None),
                ),
                mock.patch.object(
                    installer, "install_skill", return_value=linked_result
                ),
            ):
                self.assertEqual(
                    installer.install_from_args(update_args, {}),
                    (target, True, None, "4", "3"),
                )

    def test_quality_loop_remaining_loader_scope_and_runner_paths(self) -> None:
        with mock.patch.object(
            quality_loop.importlib.util, "spec_from_file_location", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot load"):
                quality_loop.load_gate(Path("missing.py"))

        fake_gate = SimpleNamespace(
            commit_scope=lambda root, reference: ("commit", root, reference),
            local_changes_scope=lambda root: ("local", root),
            repository_scope=lambda: ("repository",),
        )
        root = Path("/repo")
        self.assertEqual(
            quality_loop.selected_scope(
                SimpleNamespace(commit="HEAD", local_changes=False), fake_gate, root
            )[0],
            "commit",
        )
        self.assertEqual(
            quality_loop.selected_scope(
                SimpleNamespace(commit=None, local_changes=True), fake_gate, root
            )[0],
            "local",
        )
        self.assertEqual(
            quality_loop.selected_scope(
                SimpleNamespace(commit=None, local_changes=False), fake_gate, root
            )[0],
            "repository",
        )

        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            args = quality_loop.parse_args(["--root", str(run_root)])
            with mock.patch.object(quality_loop, "load_gate_safely", return_value=None):
                self.assertEqual(quality_loop.run_locked(args, run_root), 2)

            analysis = SimpleNamespace(gates=[])
            fake_loaded_gate = SimpleNamespace(
                bundled_thresholds_path=lambda: run_root / "bundled.json",
                html_report=lambda value: "<html></html>",
            )
            quality_directory = run_root / ".quality"
            quality_directory.mkdir(exist_ok=True)
            (quality_directory / "quality-thresholds.json").write_text(
                "{}", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    quality_loop, "load_gate_safely", return_value=fake_loaded_gate
                ),
                mock.patch.object(
                    quality_loop,
                    "execute_analysis",
                    return_value=(analysis, 1, None),
                ),
                mock.patch.object(
                    quality_loop, "analysis_state", return_value={"status": "fail"}
                ),
                mock.patch.object(quality_loop, "write_json_atomic"),
                mock.patch.object(quality_loop, "print_report"),
            ):
                self.assertEqual(quality_loop.run_locked(args, run_root), 1)


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
            rules_path = quality_file(root, "quality-dependencies.json")
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
            config_path = quality_file(root, "quality-gate.json")
            config_path.write_text(
                json.dumps(
                    gate.deep_merge(
                        gate.default_config(),
                        {
                            "source": {"include": ["src/**"], "exclude": []},
                            "test": {"command": [sys.executable, "check.py"]},
                            "smoke": {
                                "commands": [
                                    [sys.executable, "-c", "print('app answers')"]
                                ]
                            },
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
            passing_html = (artifacts / "quality-gate-report.html").read_text(
                encoding="utf-8"
            )
            self.assertEqual(passing_state["status"], "pass")
            self.assertEqual(len(passing_state["gates"]), 15)
            self.assertEqual(passing_state["counts"]["checks_applicable"], 13)
            self.assertEqual(passing_state["counts"]["checks_passing"], 13)
            self.assertEqual(passing_state["counts"]["mutants_static"], 0)
            self.assertEqual(passing_state["counts"]["files_total"], 1)
            self.assertEqual(passing_state["counts"]["files_failing_loc"], 0)
            self.assertEqual(passing_state["thresholds"]["file_loc"]["max_lines"], 600)
            self.assertEqual(
                passing_state["metrics"]["files"],
                [
                    {
                        "path": "src/app.py",
                        "lines": 2,
                        "limit": 600,
                        "passed": True,
                    }
                ],
            )
            self.assertTrue(passing_state["metrics"]["certified"])
            self.assertEqual(
                passing_state["scope"],
                {
                    "kind": "repository",
                    "reference": None,
                    "changed_files": [],
                },
            )
            self.assertIsNone(passing_state["fix_prompt"])
            self.assertIn("--mutation-workers auto", passing_state["rerun_command"])
            self.assertEqual(passing_html.count('<details class="check-row '), 15)
            self.assertNotIn("9 total ·", passing_html)

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
            self.assertEqual(fast_state["counts"]["checks_deferred"], 3)
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

            (root / "check.py").write_text(
                "raise AssertionError('intentional baseline failure')\n",
                encoding="utf-8",
            )
            red_tests = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            red_tests_state = json.loads(
                (artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(
                red_tests.returncode, 1, red_tests.stdout + red_tests.stderr
            )
            self.assertFalse(red_tests_state["metrics"]["certified"])
            self.assertEqual(
                red_tests_state["metrics"]["functions"],
                [
                    {
                        "path": "src/app.py",
                        "name": "is_one",
                        "line": 1,
                        "covered_lines": 2,
                        "total_lines": 2,
                        "coverage_percent": 100.0,
                        "coverage_measured": True,
                        "covered_branches": 0,
                        "total_branches": 0,
                        "branch_coverage_percent": 100.0,
                        "branch_coverage_measured": True,
                        "complexity": 1,
                        "craap_score": 1.0,
                        "passed": True,
                    }
                ],
            )

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

    def test_agent_loop_local_changes_scope_is_persisted_and_rerunnable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            root.mkdir()
            (root / "src").mkdir()

            def git(*arguments: str) -> None:
                subprocess.run(
                    ["git", *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                )

            git("init", "-q")
            git("config", "user.name", "Quality Gate Test")
            git("config", "user.email", "quality-gate@example.test")
            changed = root / "src" / "changed.py"
            unchanged = root / "src" / "unchanged.py"
            changed.write_text("def changed():\n    return 1\n", encoding="utf-8")
            unchanged.write_text("def unchanged():\n    return 2\n", encoding="utf-8")
            (root / "check.py").write_text("pass\n", encoding="utf-8")
            (root / "metrics.json").write_text(
                json.dumps(
                    {
                        "functions": [
                            {
                                "path": "src/changed.py",
                                "name": "changed",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            },
                            {
                                "path": "src/unchanged.py",
                                "name": "unchanged",
                                "start_line": 1,
                                "end_line": 2,
                                "complexity": 1,
                                "covered_lines": 2,
                                "total_lines": 2,
                                "coverage_percent": 100,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (quality_file(root, "quality-dependencies.json")).write_text(
                json.dumps(
                    {
                        "modules": [{"name": "core", "paths": ["src/**"]}],
                        "allow": {"core": []},
                        "deny": [],
                    }
                ),
                encoding="utf-8",
            )
            (quality_file(root, "quality-gate.json")).write_text(
                json.dumps(
                    {
                        "source": {"include": ["src/**"], "exclude": []},
                        "test": {"command": [sys.executable, "check.py"]},
                        "smoke": {
                            "commands": [[sys.executable, "-c", "print('app answers')"]]
                        },
                        "metrics": {"report": "metrics.json"},
                    }
                ),
                encoding="utf-8",
            )
            git("add", ".")
            git("commit", "-qm", "base")
            changed.write_text("def changed():\n    return 3\n", encoding="utf-8")
            artifacts = workspace / "artifacts"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LOOP_SCRIPT),
                    "--root",
                    str(root),
                    "--artifact-dir",
                    str(artifacts),
                    "--local-changes",
                    "--fast",
                    "--no-install",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            state = json.loads(
                (artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(
                completed.returncode, 1, completed.stdout + completed.stderr
            )
            self.assertEqual(
                state["scope"],
                {
                    "kind": "local_changes",
                    "reference": None,
                    "changed_files": ["src/changed.py"],
                },
            )
            self.assertEqual(
                [item["path"] for item in state["metrics"]["functions"]],
                ["src/changed.py"],
            )
            self.assertIn("--local-changes", state["rerun_command"])
            self.assertIn("--local-changes", state["full_rerun_command"])
            self.assertFalse(state["certified"])
            self.assertFalse(state["scope_certified"])
            self.assertIn(
                "Fast quality check for local changes",
                (artifacts / "quality-gate-report.html").read_text(encoding="utf-8"),
            )

            full_artifacts = workspace / "full-artifacts"
            full = subprocess.run(
                [
                    sys.executable,
                    str(LOOP_SCRIPT),
                    "--root",
                    str(root),
                    "--artifact-dir",
                    str(full_artifacts),
                    "--local-changes",
                    "--no-install",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            full_state = json.loads(
                (full_artifacts / "quality-gate-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
            self.assertEqual(full_state["status"], "pass")
            self.assertFalse(full_state["certified"])
            self.assertTrue(full_state["scope_certified"])
            self.assertIn(
                "LOCAL CHANGES PASSED",
                (full_artifacts / "quality-gate-report.html").read_text(
                    encoding="utf-8"
                ),
            )

    def test_agent_loop_writes_report_in_quality_folder_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "repository"
            cache = workspace / "cache"
            root.mkdir()
            environment = os.environ.copy()
            environment["XDG_CACHE_HOME"] = str(cache)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LOOP_SCRIPT),
                    "--root",
                    str(root),
                    "--fast",
                    "--no-install",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=environment,
            )
            html = root / ".quality" / "quality-gate-report.html"
            state = root / ".quality" / "quality-gate-state.json"

            self.assertIn(
                completed.returncode, (1, 2), completed.stdout + completed.stderr
            )
            self.assertTrue(html.is_file())
            self.assertTrue(state.is_file())
            self.assertIn(f"HTML={html.resolve()}", completed.stdout)

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
            (quality_file(root, "quality-dependencies.json")).write_text(
                json.dumps(dependencies), encoding="utf-8"
            )
            config = gate.default_config()
            passing_command = [sys.executable, "check.py"]
            config = gate.deep_merge(
                config,
                {
                    "source": {"include": ["src/**"], "exclude": []},
                    "test": {"command": passing_command},
                    "smoke": {
                        "commands": [[sys.executable, "-c", "print('app answers')"]]
                    },
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
            self.assertEqual(rendered.count('<details class="check-row '), 15)
            self.assertNotIn("9 total ·", rendered)
            self.assertIn("Code metrics", rendered)
            self.assertIn("<strong>1.00</strong><span>Average CRAAP", rendered)
            self.assertIn("<strong>2</strong><span>Mean file LOC", rendered)
            self.assertNotIn("<h2>File size</h2>", rendered)
            self.assertEqual(rendered.count("<th>Physical LOC</th>"), 1)
            self.assertEqual(rendered.count("<th>CRAAP</th>"), 1)
            self.assertEqual(rendered.count("<th>Static</th>"), 1)
            self.assertNotIn("Mutations + flaky tests", rendered)
            self.assertNotIn("<span>Evidence</span>", rendered)
            self.assertIn("<span>Run details</span>", rendered)
            self.assertEqual(rendered.count("data-copy="), 3)
            self.assertNotIn("Gherkin", rendered)
            self.assertNotIn("Executable UI", rendered)
            self.assertIn("All 1 mutants were killed", rendered)


if __name__ == "__main__":
    unittest.main()


def fixture_repository(root: Path, mutation_enabled: bool = True) -> Path:
    """A one-function Python repository whose every gate command is a stub."""
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
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
    quality_file(root, "quality-dependencies.json").write_text(
        json.dumps(
            {
                "modules": [{"name": "core", "paths": ["src/**"]}],
                "allow": {"core": []},
                "deny": [],
            }
        ),
        encoding="utf-8",
    )
    stub = lambda text: [[sys.executable, "-c", f"print({text!r})"]]  # noqa: E731
    config_path = quality_file(root, "quality-gate.json")
    config_path.write_text(
        json.dumps(
            gate.deep_merge(
                gate.default_config(),
                {
                    "source": {"include": ["src/**"], "exclude": []},
                    "test": {"command": [sys.executable, "check.py"]},
                    "smoke": {"commands": stub("app answers")},
                    "format_lint": {"commands": stub("format clean")},
                    "types": {"commands": stub("types clean")},
                    "contracts": {"commands": stub("contracts valid")},
                    "metrics": {"report": "metrics.json"},
                    "dead_code": {"commands": stub("no dead code")},
                    "mutation": {
                        "enabled": mutation_enabled,
                        "test_command": [sys.executable, "check.py"],
                        "operators": {"==": "!="},
                    },
                    "flaky_tests": {"enabled": mutation_enabled},
                },
            )
        ),
        encoding="utf-8",
    )
    return config_path


def run_loop(root: Path, *flags: str) -> tuple[subprocess.CompletedProcess, dict]:
    artifacts = root / "artifacts"
    completed = subprocess.run(
        [
            sys.executable,
            str(LOOP_SCRIPT),
            "--root",
            str(root),
            "--artifact-dir",
            str(artifacts),
            "--no-install",
            *flags,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    state = json.loads((artifacts / "quality-gate-state.json").read_text("utf-8"))
    return completed, state


class CheckSelectionTests(unittest.TestCase):
    def test_selection_helpers_map_flags_to_gates_and_focus(self) -> None:
        self.assertIsNone(gate.selected_gate_keys([]))
        self.assertEqual(
            gate.selected_gate_keys(["lint", "coverage", "dead-code"]),
            frozenset({"format_lint", "quality", "dead_code"}),
        )
        self.assertEqual(gate.metrics_focus(["coverage", "complexity"]), "craap")
        self.assertEqual(gate.metrics_focus(["tests", "coverage"]), "coverage")
        self.assertEqual(gate.metrics_focus(["complexity", "lint"]), "complexity")
        self.assertEqual(gate.metrics_focus(["tests"]), "tests")
        self.assertIsNone(gate.metrics_focus(["lint"]))
        everything = gate.gate_selection([])
        self.assertFalse(everything.partial)
        self.assertTrue(everything.wants("mutation"))
        partial = gate.gate_selection(["lint", "flaky"])
        self.assertTrue(partial.partial)
        self.assertTrue(partial.wants("format_lint"))
        self.assertFalse(partial.wants("types"))
        self.assertTrue(partial.forces("flaky"))
        self.assertFalse(partial.forces("mutation"))
        self.assertEqual(gate.run_mode(True, everything), "fast")
        self.assertEqual(gate.run_mode(False, everything), "full")
        self.assertEqual(gate.run_mode(True, partial), "partial")

    def test_selection_decides_whether_the_test_baseline_runs(self) -> None:
        self.assertTrue(gate.selection_needs_test_baseline(gate.gate_selection([])))
        self.assertTrue(
            gate.selection_needs_test_baseline(gate.gate_selection(["coverage"]))
        )
        self.assertTrue(
            gate.selection_needs_test_baseline(gate.gate_selection(["mutation"]))
        )
        self.assertFalse(
            gate.selection_needs_test_baseline(gate.gate_selection(["complexity"]))
        )
        self.assertFalse(
            gate.selection_needs_test_baseline(gate.gate_selection(["lint"]))
        )

    def test_skipped_off_and_tests_only_gates(self) -> None:
        skipped = gate.skipped_check("types", "Static type checking")
        self.assertTrue(skipped.passed)
        self.assertTrue(skipped.skipped)
        self.assertEqual(gate.gate_outcome(skipped), "SKIPPED")
        self.assertEqual(gate.gate_status(skipped), "skipped")
        off = gate.off_check("mutation", "Mutation testing", "mutation")
        self.assertTrue(off.off)
        self.assertEqual(gate.gate_outcome(off), "OFF")
        self.assertEqual(gate.gate_status(off), "off")
        self.assertIn("--mutation", off.summary)
        enabled = gate.with_gate_enabled({"mutation": {"enabled": False}}, "mutation")
        self.assertTrue(enabled["mutation"]["enabled"])
        self.assertFalse(gate.tests_only_gate(None, None).passed)
        failed = gate.CommandResult(["t"], 1, "boom", 0.5)
        result = gate.tests_only_gate(["t"], failed)
        self.assertFalse(result.passed)
        self.assertEqual(result.details, ["boom"])
        passed = gate.tests_only_gate(["t"], gate.CommandResult(["t"], 0, "", 0.25))
        self.assertTrue(passed.passed)
        self.assertIn("0.2s", passed.summary)

    def test_static_complexity_gate_needs_no_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "app.py"
            source.write_text(
                "def simple(a):\n    return a\n\n\n"
                "def branchy(a, b, c, d, e, f, g):\n"
                "    if a:\n        return 1\n    if b:\n        return 2\n"
                "    if c:\n        return 3\n    if d:\n        return 4\n"
                "    if e:\n        return 5\n    if f:\n        return 6\n"
                "    return g\n",
                encoding="utf-8",
            )
            tools = gate.ToolContext(root, sys.executable, root / "pythonpath")
            config = {"metrics": {"complexity_limit": 6}}
            result, functions = gate.run_complexity_gate(
                root, config, [source], root, tools
            )
            self.assertFalse(result.passed)
            self.assertEqual(result.title, "Complexity")
            self.assertEqual([f.name for f in functions], ["simple", "branchy"])
            self.assertFalse(functions[1].coverage_measured)
            self.assertFalse(functions[1].passed)
            self.assertTrue(functions[0].passed)
            self.assertIn("branchy: complexity 7", result.details[0])
            self.assertFalse(
                gate.function_measurement(functions[1])["coverage_measured"]
            )
            passing, _ = gate.run_complexity_gate(
                root, {"metrics": {"complexity_limit": 8}}, [source], root, tools
            )
            self.assertTrue(passing.passed)
            empty, _ = gate.run_complexity_gate(root, config, [], root, tools)
            self.assertFalse(empty.passed)

    def test_flaky_and_mutation_gates_honour_selection_and_off_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = gate.deep_merge(
                gate.default_config(),
                {"flaky_tests": {"enabled": False}, "mutation": {"enabled": False}},
            )
            everything = gate.gate_selection([])
            self.assertTrue(
                gate.flaky_gate_for_run(root, config, None, None, False, everything).off
            )
            self.assertTrue(
                gate.flaky_gate_for_run(
                    root, config, None, None, False, gate.gate_selection(["lint"])
                ).skipped
            )
            self.assertTrue(
                gate.flaky_gate_for_run(
                    root, config, None, None, True, everything
                ).deferred
            )
            forced = gate.flaky_gate_for_run(
                root, config, None, None, False, gate.gate_selection(["flaky"])
            )
            self.assertFalse(forced.applicable)
            self.assertIn("no complete test command", forced.summary)
            tools = gate.ToolContext(root, sys.executable, root / "pythonpath")
            scope = gate.repository_scope()
            off, mutations = gate.mutation_gate_for_run(
                root, config, [], None, None, None, tools, scope, False, everything
            )
            self.assertTrue(off.off)
            self.assertEqual(mutations, [])
            skipped, _ = gate.mutation_gate_for_run(
                root,
                config,
                [],
                None,
                None,
                None,
                tools,
                scope,
                False,
                gate.gate_selection(["lint"]),
            )
            self.assertTrue(skipped.skipped)
            deferred, _ = gate.mutation_gate_for_run(
                root, config, [], None, None, None, tools, scope, True, everything
            )
            self.assertTrue(deferred.deferred)
            incremental, _ = gate.mutation_gate_for_run(
                root,
                config,
                [],
                None,
                None,
                None,
                tools,
                gate.GateScope("local_changes"),
                False,
                everything,
            )
            self.assertFalse(incremental.applicable)
            forced_mutation, _ = gate.mutation_gate_for_run(
                root,
                config,
                [root / "missing.py"],
                None,
                None,
                None,
                tools,
                scope,
                False,
                gate.gate_selection(["mutation"]),
            )
            self.assertFalse(forced_mutation.passed)
            self.assertIn("No full-suite test command", forced_mutation.summary)
            dependency, _ = gate.dependency_gate_for_run(
                root, config, [], root, [], scope, gate.gate_selection(["lint"])
            )
            self.assertTrue(dependency.skipped)
            not_applicable, _ = gate.dependency_gate_for_run(
                root, config, [], root, [], gate.GateScope("commit"), everything
            )
            self.assertFalse(not_applicable.applicable)
            loc, files = gate.run_file_loc_for_selection(
                root, [], config["file_loc"], scope, gate.gate_selection(["lint"])
            )
            self.assertTrue(loc.skipped)
            self.assertEqual(files, [])
            loc_scope, _ = gate.run_file_loc_for_selection(
                root, [], config["file_loc"], gate.GateScope("commit"), everything
            )
            self.assertFalse(loc_scope.applicable)

    def test_quality_gate_dispatches_on_focus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = gate.ToolContext(root, sys.executable, root / "pythonpath")
            config = gate.default_config()
            scope = gate.repository_scope()
            skipped, functions = gate.run_quality_gate(
                root,
                config,
                [],
                root,
                tools,
                scope,
                gate.gate_selection(["lint"]),
                None,
                None,
            )
            self.assertTrue(skipped.skipped)
            self.assertEqual(functions, [])
            tests_only, _ = gate.run_quality_gate(
                root,
                config,
                [],
                root,
                tools,
                scope,
                gate.gate_selection(["tests"]),
                ["t"],
                gate.CommandResult(["t"], 0, "", 0.1),
            )
            self.assertEqual(tests_only.title, "Tests")
            self.assertTrue(tests_only.passed)
            incremental, _ = gate.run_quality_gate(
                root,
                config,
                [],
                root,
                tools,
                gate.GateScope("local_changes"),
                gate.gate_selection([]),
                ["t"],
                gate.CommandResult(["t"], 0, "", 0.1),
            )
            self.assertTrue(incremental.passed)
            source = root / "app.py"
            source.write_text("def f(a):\n    return a\n", encoding="utf-8")
            complexity, _ = gate.run_quality_gate(
                root,
                config,
                [source],
                root,
                tools,
                scope,
                gate.gate_selection(["complexity"]),
                None,
                None,
            )
            self.assertEqual(complexity.title, "Complexity")
            self.assertTrue(complexity.passed)

    def test_partial_state_status_and_prompt_notes(self) -> None:
        partial = SimpleNamespace(
            passed=False, mode="partial", selected_passed=True, ready_for_full=False
        )
        self.assertTrue(gate.partial_run_passed(partial))
        self.assertEqual(gate.state_status(partial, None), "pass")
        self.assertIsNone(gate.state_fix_prompt(SimpleNamespace(), partial))
        partial.selected_passed = False
        self.assertFalse(gate.partial_run_passed(partial))
        self.assertEqual(gate.state_status(partial, None), "fail")
        full = SimpleNamespace(
            passed=False, mode="full", selected_passed=True, ready_for_full=False
        )
        self.assertFalse(gate.partial_run_passed(full))
        report = gate.AnalysisReport(
            root="/repo",
            generated_at="now",
            languages=[],
            gates=[gate.off_check("mutation", "Mutation testing", "mutation")],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
            mode="partial",
            selection=("lint",),
        )
        self.assertEqual(
            gate.off_note(report, "mutation"), " (off: run only when the user asks)"
        )
        self.assertEqual(gate.off_note(report, "flaky"), "")
        self.assertTrue(report.selected_passed)
        self.assertFalse(report.passed)
        prompt = gate.master_fix_prompt(report)
        self.assertIn("partial run (--lint)", prompt)
        self.assertIn("zero survive (off: run only when the user asks)", prompt)
        self.assertIn("SELECTED CHECKS PASSED", gate.html_report(report))
        self.assertIn("OFF", gate.html_report(report))
        report.gates = [gate.skipped_check("types", "Static type checking")]
        self.assertFalse(report.selected_passed)
        self.assertIn("SELECTED CHECKS NEED WORK", gate.html_report(report))

    def test_init_is_workspace_aware_and_turns_slow_gates_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "mono",
                        "workspaces": {"packages": ["packages/*"]},
                        "devDependencies": {"vitest": "2.1.9"},
                    }
                ),
                encoding="utf-8",
            )
            for name, manifest in (
                ("shared", {"name": "@m/shared", "scripts": {"test": "vitest run"}}),
                ("server", {"name": "@m/server", "dependencies": {"@m/shared": "*"}}),
                ("docs", {"name": "@m/docs"}),
            ):
                (root / "packages" / name).mkdir(parents=True)
                (root / "packages" / name / "package.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
            (root / "packages" / "server" / "vitest.config.ts").write_text("", "utf-8")
            (root / "packages" / "stray").mkdir()
            self.assertEqual(
                [path.name for path in gate.npm_workspace_dirs(root)],
                ["docs", "server", "shared"],
            )
            self.assertTrue(gate.workspace_uses_vitest(root / "packages" / "server"))
            self.assertTrue(gate.workspace_uses_vitest(root / "packages" / "shared"))
            self.assertFalse(gate.workspace_uses_vitest(root / "packages" / "docs"))
            self.assertEqual(gate.npm_workspace_dirs(root / "packages"), [])
            with mock.patch.object(gate, "executable", return_value="npm"):
                self.assertEqual(
                    gate.infer_test_command(root),
                    ["npm", "test", "--workspaces", "--if-present"],
                )
            coverage = gate.workspace_coverage_template(root)
            self.assertEqual(coverage["coverage_format"], "lcov")
            self.assertEqual(len(coverage["coverage_commands"]), 3)
            self.assertEqual(
                coverage["coverage_commands"][0][3:5], ["--root", "packages/server"]
            )
            self.assertEqual(coverage["coverage_commands"][-1][2], "--merge-lcov")
            self.assertEqual(gate.workspace_coverage_template(root / "packages"), {})
            rules = gate.dependency_rules_template(root)
            self.assertEqual(
                rules["allow"],
                {
                    "packages/docs": [],
                    "packages/server": ["packages/shared"],
                    "packages/shared": [],
                },
            )
            self.assertEqual(
                gate.dependency_rules_template(root / "packages")["modules"],
                [{"name": "repository", "paths": ["**"]}],
            )
            completed = subprocess.run(
                [sys.executable, str(CORE_SCRIPT), "--root", str(root), "--init"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            config = json.loads(
                (root / ".quality" / "quality-gate.json").read_text("utf-8")
            )
            self.assertFalse(config["mutation"]["enabled"])
            self.assertFalse(config["flaky_tests"]["enabled"])
            self.assertEqual(
                config["metrics"]["coverage_report"], ".quality/coverage/lcov.info"
            )
            self.assertTrue((root / ".quality" / "quality-dependencies.json").is_file())
            self.assertIn("generated skeleton", completed.stdout)
            self.assertIn("Next: python3", completed.stdout)
            self.assertFalse(
                gate.write_initial_dependencies(
                    root, root / ".quality" / "quality-dependencies.json"
                )
            )
            self.assertEqual(gate.script_reference(root, root / "x.py"), "x.py")
            self.assertTrue(
                Path(gate.script_reference(root, CORE_SCRIPT)).is_absolute()
            )

    def test_merge_lcov_prefixes_relative_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.info").write_text(
                "TN:\nSF:src/a.ts\nDA:1,1\nend_of_record\n", "utf-8"
            )
            (root / "b.info").write_text("SF:/abs/b.ts\nend_of_record\n", "utf-8")
            output = root / "out" / "lcov.info"
            self.assertEqual(gate.prefixed_lcov_line("SF:x.ts", ""), "SF:x.ts")
            self.assertEqual(gate.prefixed_lcov_line("SF:x.ts", "pkg/"), "SF:pkg/x.ts")
            self.assertEqual(
                gate.merge_lcov_files(
                    output, [f"packages/a={root / 'a.info'}", f"b={root / 'b.info'}"]
                ),
                2,
            )
            self.assertEqual(
                output.read_text("utf-8"),
                "TN:\nSF:packages/a/src/a.ts\nDA:1,1\nend_of_record\nSF:/abs/b.ts\nend_of_record\n",
            )
            with self.assertRaises(ValueError):
                gate.merge_lcov_files(output, ["no-separator"])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(gate.merge_lcov_command(output, ["x=missing.info"]), 2)
            self.assertIn("error:", stderr.getvalue())
            self.assertEqual(
                gate.main(["--merge-lcov", str(output), f"a={root / 'a.info'}"]), 0
            )

    def test_loop_runs_selected_checks_only_and_reports_coverage_today(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_repository(root, mutation_enabled=False)
            partial, state = run_loop(root, "--lint", "--types")
            self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
            self.assertEqual(state["mode"], "partial")
            self.assertEqual(state["selection"], ["lint", "types"])
            self.assertEqual(state["status"], "pass")
            self.assertFalse(state["certified"])
            statuses = {item["key"]: item["status"] for item in state["gates"]}
            self.assertEqual(statuses["format_lint"], "pass")
            self.assertEqual(statuses["quality"], "skipped")
            self.assertEqual(state["counts"]["checks_skipped"], 13)
            self.assertIn("mode: partial (--lint --types)", partial.stdout)
            self.assertIn("Coverage today: not measured yet", partial.stdout)
            self.assertIn("does not certify", partial.stdout)
            self.assertIn("ITEMS_TO_FIX=0", partial.stdout)
            self.assertIn("--lint --types", state["rerun_command"])

            full, full_state = run_loop(root)
            self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
            statuses = {item["key"]: item["status"] for item in full_state["gates"]}
            self.assertEqual(statuses["mutation"], "off")
            self.assertEqual(statuses["flaky"], "off")
            self.assertEqual(full_state["counts"]["checks_off"], 2)
            self.assertTrue(full_state["certified"])
            self.assertIn("[OFF] Mutation testing", full.stdout)
            self.assertIn(
                "Coverage today: 1 of 1 functions fully covered (100%)", full.stdout
            )
            self.assertIn("Ship report is green", full.stdout)

            later, later_state = run_loop(root, "--lint")
            self.assertEqual(later.returncode, 0)
            self.assertIn("last measured", later.stdout)
            self.assertIn("1 of 1 functions", later.stdout)
            self.assertEqual(later_state["focus"], None)

            forced, forced_state = run_loop(
                root, "--mutation", "--mutation-workers", "auto"
            )
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            statuses = {item["key"]: item["status"] for item in forced_state["gates"]}
            self.assertEqual(statuses["mutation"], "pass")
            self.assertEqual(forced_state["mode"], "partial")


class LoopReportTests(unittest.TestCase):
    def test_selection_names_and_rerun_flags(self) -> None:
        args = quality_loop.parse_args(["--coverage", "--dead-code"])
        self.assertEqual(quality_loop.selection_names(args), ("coverage", "dead-code"))
        self.assertEqual(quality_loop.selection_names(quality_loop.parse_args([])), ())
        command: list[str] = []
        quality_loop.append_rerun_execution(command, [], False, False, None, ("craap",))
        self.assertEqual(command, ["--craap"])

    def test_run_passed_and_header(self) -> None:
        scope = SimpleNamespace(description="local changes")
        partial = SimpleNamespace(
            passed=False,
            mode="partial",
            selected_passed=True,
            selection=("lint",),
            scope=scope,
        )
        self.assertTrue(quality_loop.run_passed(partial))
        self.assertEqual(
            quality_report.report_header(partial),
            "QUALITY REPORT · mode: partial (--lint) · scope: local changes",
        )
        fast = SimpleNamespace(
            passed=False, mode="fast", selected_passed=True, selection=(), scope=scope
        )
        self.assertFalse(quality_loop.run_passed(fast))
        self.assertEqual(
            quality_report.report_header(fast),
            "QUALITY REPORT · mode: fast · scope: local changes",
        )
        self.assertTrue(quality_loop.run_passed(SimpleNamespace(passed=True)))

    def test_gate_lines_show_details_only_for_failures(self) -> None:
        fake = SimpleNamespace(
            gate_outcome=lambda item: item.outcome, gate_status=lambda item: item.status
        )
        failed = SimpleNamespace(
            outcome="FAIL",
            status="fail",
            title="Types",
            summary="1 error",
            details=["", "a.py:1 bad\nmore", "x" * 300],
        )
        lines = quality_report.gate_lines(fake, failed)
        self.assertEqual(lines[0], "[FAIL] Types: 1 error")
        self.assertEqual(lines[1], "    ")
        self.assertEqual(lines[2], "    a.py:1 bad")
        self.assertEqual(len(lines[3]), 204)
        passed = SimpleNamespace(
            outcome="PASS",
            status="pass",
            title="Types",
            summary="ok",
            details=["ignored"],
        )
        self.assertEqual(quality_report.gate_lines(fake, passed), ["[PASS] Types: ok"])
        self.assertEqual(quality_report.first_line("  \n"), "")

    def test_coverage_today_counts_functions_at_the_limit_as_covered(self) -> None:
        functions = [
            SimpleNamespace(path="b.py", coverage_percent=100.0),
            SimpleNamespace(path="a.py", coverage_percent=99.9),
            SimpleNamespace(path="a.py", coverage_percent=0.0),
            SimpleNamespace(path="b.py", coverage_percent=50.0),
            SimpleNamespace(
                path="c.py", coverage_percent=10.0, coverage_measured=False
            ),
        ]
        summary = quality_report.coverage_today(functions, 100.0)
        self.assertEqual(
            summary,
            {
                "covered": 1,
                "total": 4,
                "percent": 25,
                "gaps": [("a.py", 2), ("b.py", 1)],
            },
        )
        self.assertEqual(
            quality_report.coverage_summary_text(summary),
            "1 of 4 functions fully covered (25%) · 3 not covered — most in: a.py (2), b.py (1)",
        )
        full = quality_report.coverage_today(functions[:1], 100.0)
        self.assertEqual(
            quality_report.coverage_summary_text(full),
            "1 of 1 functions fully covered (100%)",
        )
        self.assertIsNone(quality_report.coverage_today(functions[4:], 100.0))
        self.assertEqual(
            quality_report.coverage_today(functions[1:2], 99.9)["covered"], 1
        )

    def test_coverage_today_line_falls_back_to_the_previous_state(self) -> None:
        analysis = SimpleNamespace(
            thresholds={"metrics": {"coverage_limit": 100}}, functions=[]
        )
        previous = (
            "2026-09-01",
            {"covered": 2, "total": 3, "percent": 67, "gaps": [("a.py", 1)]},
        )
        self.assertEqual(
            quality_report.coverage_today_line(analysis, previous),
            "Coverage today: not measured in this run — last measured 2026-09-01: 2 of 3 functions fully covered (67%) · 1 not covered — most in: a.py (1)",
        )
        self.assertEqual(
            quality_report.coverage_today_line(analysis, None),
            "Coverage today: not measured yet — run --fast or --coverage.",
        )
        analysis.functions = [SimpleNamespace(path="a.py", coverage_percent=100.0)]
        self.assertEqual(
            quality_report.coverage_today_line(analysis, previous),
            "Coverage today: 1 of 1 functions fully covered (100%)",
        )
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            self.assertIsNone(quality_report.previous_measurement(state_path))
            state_path.write_text(json.dumps({"metrics": {"functions": []}}), "utf-8")
            self.assertIsNone(quality_report.previous_measurement(state_path))
            state_path.write_text(
                json.dumps(
                    {
                        "generated_at": "then",
                        "thresholds": {"metrics": {"coverage_limit": 90}},
                        "metrics": {
                            "functions": [{"path": "a.py", "coverage_percent": 95.0}]
                        },
                    }
                ),
                "utf-8",
            )
            self.assertEqual(
                quality_report.previous_measurement(state_path),
                ("then", {"covered": 1, "total": 1, "percent": 100, "gaps": []}),
            )
            state_path.write_text("{", "utf-8")
            self.assertIsNone(quality_report.previous_measurement(state_path))

    def test_to_fix_lists_every_failure_kind_and_caps_the_list(self) -> None:
        state = {
            "failures": {
                "functions": [
                    {
                        "path": "a.py",
                        "line": 3,
                        "name": "f",
                        "coverage_percent": 50.0,
                        "complexity": 7,
                    }
                ],
                "files": [{"path": "big.py", "lines": 700, "limit": 600}],
                "surviving_mutants": [
                    {"path": "a.py", "line": 4, "change": "== -> !="}
                ],
                "dependencies": [
                    {"source": "a.py", "line": 1, "target": "b.py", "rule": "deny"}
                ],
                "checks": [
                    {"key": "types", "title": "Types", "summary": "1 error"},
                    {
                        "key": "quality",
                        "title": "Quality",
                        "summary": "already itemised",
                    },
                ],
            }
        }
        items = quality_report.to_fix_items(state)
        self.assertEqual(
            items,
            [
                "a.py:3 f — coverage 50%, complexity 7",
                "big.py — 700 lines (max 600)",
                "a.py:4 surviving mutant `== -> !=`",
                "a.py:1 -> b.py: deny",
                "Types: 1 error",
            ],
        )
        self.assertEqual(
            quality_report.to_fix_lines(state)[0],
            "  1. a.py:3 f — coverage 50%, complexity 7",
        )
        self.assertEqual(quality_report.to_fix_lines(state)[4], "  5. Types: 1 error")
        self.assertEqual(len(quality_report.to_fix_lines(state)), 5)
        many = {
            "failures": {
                **EMPTY_FAILURES,
                "files": [
                    {"path": f"{i}.py", "lines": 1, "limit": 0} for i in range(13)
                ],
            }
        }
        lines = quality_report.to_fix_lines(many)
        self.assertEqual(len(lines), 14)
        self.assertEqual(
            lines[0], "  13 items in 13 files — one file per cycle, top first"
        )
        self.assertEqual(lines[1], "  1. 0.py — 1 item (1 file size)")
        self.assertEqual(
            lines[-1], "  … and 1 more files (quality_items.py --summary lists them)"
        )
        twelve = {
            "failures": {**EMPTY_FAILURES, "files": many["failures"]["files"][:12]}
        }
        self.assertEqual(len(quality_report.to_fix_lines(twelve)), 12)
        grouped = quality_report.grouped_fix_lines(quality_report.item_records(twelve))
        self.assertEqual(len(grouped), 13)
        self.assertNotIn("more files", grouped[-1])
        self.assertEqual(quality_report.to_fix_lines({"failures": EMPTY_FAILURES}), [])

    def test_next_step_lines_cover_every_status(self) -> None:
        state = {
            "status": "pass",
            "rerun_command": "again",
            "full_rerun_command": "full",
        }
        self.assertEqual(
            quality_report.next_step_lines(state, SimpleNamespace(mode="partial")),
            [
                "Selected checks are green. This does not certify: run the full ship report:",
                "  full",
            ],
        )
        self.assertEqual(
            quality_report.next_step_lines(state, SimpleNamespace(mode="full")),
            [
                "Ship report is green: every executed check passed.",
                quality_report.HAND_OFF_LINE,
            ],
        )
        self.assertEqual(
            quality_report.next_step_lines(
                {**state, "status": "ready_for_full"}, SimpleNamespace(mode="fast")
            ),
            [
                quality_report.COMMIT_LINE,
                quality_report.COMMIT_COMMAND,
                quality_report.CONTINUE_LINE,
                "  full",
            ],
        )
        self.assertEqual(
            quality_report.next_step_lines(
                {**state, "status": "fail"}, SimpleNamespace(mode="full")
            ),
            ["Fix the items above, then rerun:", "  again"],
        )

    def test_print_report_lists_fixes_and_counts_them(self) -> None:
        fake = SimpleNamespace(
            gate_outcome=lambda item: "FAIL", gate_status=lambda item: "fail"
        )
        analysis = SimpleNamespace(
            gates=[
                SimpleNamespace(
                    title="Types", summary="1 error", details=["a.py:1 bad"]
                )
            ],
            mode="partial",
            selection=("types",),
            scope=SimpleNamespace(description="local changes"),
            thresholds={},
            functions=[],
        )
        state = {
            "status": "fail",
            "fix_prompt": "PROMPT",
            "rerun_command": "again",
            "full_rerun_command": "full",
            "failures": {
                **EMPTY_FAILURES,
                "checks": [{"key": "types", "title": "Types", "summary": "1 error"}],
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            quality_report.print_report(
                fake, analysis, state, Path("s"), Path("h"), True, None
            )
        text = output.getvalue()
        self.assertIn("mode: partial (--types)", text)
        self.assertIn("    a.py:1 bad", text)
        self.assertIn("To fix:\n  1. Types: 1 error", text)
        self.assertIn("Fix the items above, then rerun:\n  again", text)
        self.assertIn(
            "QUALITY_LOOP=FAIL\nITEMS_TO_FIX=1\nSTATE=s\nHTML=h\n\nPROMPT", text
        )
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            quality_report.print_report(
                fake,
                analysis,
                {**state, "fix_prompt": None},
                Path("s"),
                Path("h"),
                True,
                None,
            )
        self.assertNotIn("PROMPT", quiet.getvalue())


SMOKE_SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "smoke_check.py"
smoke_check = load_script("smoke_check_test_module", SMOKE_SCRIPT)


def stub_command(text: str) -> list[list[str]]:
    return [[sys.executable, "-c", f"print({text!r})"]]


class SmokeGateTests(unittest.TestCase):
    def config(self, **smoke: Any) -> dict[str, Any]:
        return gate.deep_merge(gate.default_config(), {"smoke": smoke})

    def test_unconfigured_smoke_fails_the_ship_report_with_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = gate.smoke_gate_for_run(
                Path(temporary), self.config(), False, gate.gate_selection([])
            )
            self.assertFalse(result.passed)
            self.assertTrue(result.applicable)
            self.assertIn("runs (smoke)", result.summary)
            self.assertIn("smoke_check.py", result.details[0])
            self.assertIn("does not prove the application runs", result.details[0])

    def test_configured_smoke_passes_and_fails_with_its_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            passing = gate.smoke_gate_for_run(
                root,
                self.config(commands=stub_command("app answers")),
                False,
                gate.gate_selection([]),
            )
            self.assertTrue(passing.passed)
            self.assertEqual(
                passing.summary,
                "All 1 runs (smoke) commands passed with zero violations.",
            )
            failing = gate.smoke_gate_for_run(
                root,
                self.config(commands=[[sys.executable, "-c", "raise SystemExit(3)"]]),
                False,
                gate.gate_selection(["smoke"]),
            )
            self.assertFalse(failing.passed)
            self.assertIn("1 of 1 runs (smoke) commands failed", failing.summary)

    def test_smoke_is_deferred_in_fast_mode_forced_by_flag_and_never_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(commands=stub_command("app answers"))
            deferred = gate.smoke_gate_for_run(
                root, config, True, gate.gate_selection([])
            )
            self.assertTrue(deferred.deferred)
            self.assertIn("run --smoke to do it now", deferred.summary)
            forced = gate.smoke_gate_for_run(
                root, config, True, gate.gate_selection(["smoke"])
            )
            self.assertTrue(forced.passed)
            self.assertFalse(forced.deferred)
            skipped = gate.smoke_gate_for_run(
                root, config, False, gate.gate_selection(["lint"])
            )
            self.assertTrue(skipped.skipped)
            disabled = gate.smoke_gate_for_run(
                root, self.config(enabled=False), False, gate.gate_selection([])
            )
            self.assertFalse(disabled.passed)
            self.assertIn("cannot be skipped", disabled.summary)
        self.assertEqual(gate.SELECTABLE_CHECKS["smoke"], "smoke")
        self.assertIn("smoke", [name for name, _ in quality_loop.CHECK_FLAGS])

    def test_fix_prompt_lists_the_smoke_and_scope_conditions(self) -> None:
        report = gate.AnalysisReport(
            root="r",
            generated_at="now",
            languages=["Python"],
            gates=[],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
        )
        prompt = gate.master_fix_prompt(report)
        self.assertIn("10. Every composed-root test uses real", prompt)
        self.assertIn(
            "11. The smoke check proves the configured core user story", prompt
        )
        self.assertIn("12. No production file is hidden from the gate", prompt)


class ScopeGateTests(unittest.TestCase):
    def repository(self, root: Path) -> None:
        (root / "src" / "server").mkdir(parents=True)
        (root / "src" / "server" / "index.ts").write_text("export {};\n")
        (root / "src" / "app.ts").write_text("export const a = 1;\n")
        (root / ".eslintrc.cjs").write_text("module.exports = {};\n")
        (root / "vite.config.ts").write_text("export default {};\n")
        (root / "types.d.ts").write_text("declare const x: number;\n")

    def test_default_exclusions_pass_and_extra_production_exclusions_fail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            source = gate.default_config()["source"]
            self.assertEqual(gate.excluded_production_files(root, source), [])
            self.assertTrue(gate.run_scope_gate(root, source).passed)
            hidden = {
                **source,
                "exclude": [*source["exclude"], "src/server/index.ts", ".eslintrc.cjs"],
            }
            self.assertEqual(
                gate.excluded_production_files(root, hidden),
                [("src/server/index.ts", "src/server/index.ts")],
            )
            result = gate.run_scope_gate(root, hidden)
            self.assertFalse(result.passed)
            self.assertEqual(
                result.summary,
                "1 production files are hidden from the gate by source.exclude.",
            )
            self.assertEqual(
                result.details,
                [
                    "src/server/index.ts — hidden by source.exclude pattern 'src/server/index.ts'"
                ],
            )
            self.assertEqual(result.prompts[0][0], "Restore gate scope")

    def test_tooling_files_may_be_excluded_and_partial_runs_skip_the_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repository(root)
            config = gate.deep_merge(
                gate.default_config(),
                {"source": {"exclude": ["*.config.*", ".*rc.*", "*.d.ts"]}},
            )
            self.assertTrue(
                gate.scope_gate_for_run(root, config, gate.gate_selection([])).passed
            )
            self.assertTrue(
                gate.scope_gate_for_run(
                    root, config, gate.gate_selection(["coverage"])
                ).skipped
            )
        self.assertIsNone(gate.first_match("src/app.ts", ["docs/**"]))
        self.assertEqual(
            gate.first_match("src/app.ts", ["docs/**", "src/**"]), "src/**"
        )


class SmokeTemplateTests(unittest.TestCase):
    def test_npm_start_becomes_a_browser_smoke_when_a_page_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"name": "app", "scripts": {"start": "node server.js"}})
            )
            api_only = gate.npm_start_smoke(root)
            assert api_only is not None
            self.assertEqual(api_only[0], "python3")
            self.assertTrue(api_only[1].endswith("smoke_check.py"))
            self.assertEqual(api_only[2:], ["--start", "npm start"])
            (root / "client").mkdir()
            (root / "client" / "index.html").write_text("<html></html>")
            web = gate.npm_start_smoke(root)
            assert web is not None
            self.assertEqual(web[-1], "--browser")
            self.assertEqual(gate.inferred_smoke_commands(root), [web])
            template = gate.smoke_template(root)
            self.assertEqual(template["commands"], [web])
            self.assertIn("Generated", template["_note"])
            self.assertIn("npm start", gate.smoke_init_message(template["commands"]))

    def test_python_package_import_and_nothing_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(gate.npm_start_smoke(root))
            self.assertIsNone(gate.python_package_name(root))
            self.assertIsNone(gate.python_import_smoke(root))
            self.assertEqual(gate.inferred_smoke_commands(root), [])
            template = gate.smoke_template(root)
            self.assertEqual(template["commands"], [])
            self.assertIn("Not detected", template["_note"])
            self.assertIn("no start command detected", gate.smoke_init_message([]))
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("")
            (root / "src" / "mypkg").mkdir(parents=True)
            (root / "src" / "mypkg" / "__init__.py").write_text("")
            self.assertEqual(gate.python_package_name(root), "mypkg")
            command = gate.python_import_smoke(root)
            assert command is not None
            self.assertEqual(command[1:], ["-c", "import mypkg"])
            (root / "src" / "mypkg" / "__init__.py").unlink()
            (root / "src" / "mypkg").rmdir()
            self.assertIsNone(gate.python_package_name(root))
            (root / "toolpkg").mkdir()
            (root / "toolpkg" / "__init__.py").write_text("")
            self.assertEqual(gate.python_package_name(root), "toolpkg")

    def test_init_writes_the_smoke_section_and_announces_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps({"name": "app", "scripts": {"start": "node s.js"}})
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(gate.main(["--root", str(root), "--init"]), 0)
            written = json.loads(
                (root / ".quality" / "quality-gate.json").read_text("utf-8")
            )
            self.assertEqual(
                written["smoke"]["commands"][0][2:], ["--start", "npm start"]
            )
            self.assertIn("Runs (smoke): the ship report starts", output.getvalue())


class LoopReportWordingTests(unittest.TestCase):
    def test_not_applicable_rows_say_nothing_is_required(self) -> None:
        fake = SimpleNamespace(
            gate_outcome=lambda item: "NOT APPLICABLE",
            gate_status=lambda item: "not_applicable",
        )
        result = SimpleNamespace(
            title="Dead code", summary="Not applicable: none detected.", details=["x"]
        )
        self.assertEqual(
            quality_report.gate_lines(fake, result),
            [
                "[N/A] Dead code: Not applicable: none detected. Nothing to do here; not needed for hand-off."
            ],
        )

    def test_scope_failures_are_itemized_and_other_checks_summarized(self) -> None:
        checks = [
            {"key": "quality", "title": "Q", "summary": "s", "details": []},
            {
                "key": "scope",
                "title": "Gate scope",
                "summary": "1 hidden",
                "details": ["a.ts — hidden"],
            },
            {
                "key": "smoke",
                "title": "Runs (smoke)",
                "summary": "failed",
                "details": ["out"],
            },
        ]
        self.assertEqual(
            quality_report.check_failure_items(checks, {"quality"}),
            ["a.ts — hidden", "Runs (smoke): failed"],
        )
        self.assertEqual(
            quality_report.check_failure_items(checks[:1]),
            ["Q: s"],
        )
        failures = {**EMPTY_FAILURES, "checks": checks[:1]}
        self.assertEqual(quality_report.itemized_gates(failures), set())
        self.assertEqual(quality_report.to_fix_items({"failures": failures}), ["Q: s"])
        with_functions = {
            **failures,
            "functions": [
                {
                    "path": "a.py",
                    "line": 1,
                    "name": "f",
                    "coverage_percent": 50.0,
                    "complexity": 1,
                }
            ],
        }
        self.assertEqual(quality_report.itemized_gates(with_functions), {"quality"})
        self.assertEqual(
            quality_report.to_fix_items({"failures": with_functions}),
            ["a.py:1 f — coverage 50%, complexity 1"],
        )

    def test_green_ship_report_tells_the_agent_to_hand_off(self) -> None:
        state = {"status": "pass", "rerun_command": "r", "full_rerun_command": "f"}
        lines = quality_report.next_step_lines(state, SimpleNamespace(mode="full"))
        self.assertEqual(lines[1], quality_report.HAND_OFF_LINE)
        self.assertIn("Do not add tools, configs, or checks after green", lines[1])


class LocalHttpServer:
    """A tiny HTTP server for smoke tests: 200 on /, 404 elsewhere, one thread."""

    def __init__(self) -> None:
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status = 200 if self.path == "/" else 404
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:
                return None

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "LocalHttpServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def smoke_options(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "start": None,
        "cwd": Path.cwd(),
        "url": None,
        "path": "/",
        "port": None,
        "port_env": "PORT",
        "browser": False,
        "timeout": 3.0,
        "env": {},
    }
    values.update(overrides)
    return smoke_check.SmokeOptions(**values)


class FakeProcess:
    def __init__(self, timeouts: int = 0, output: str = "out") -> None:
        self.pid = 4194304
        self.signals: list[int] = []
        self.timeouts = timeouts
        self.output = output

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("cmd", timeout or 0)
        return self.output, None


class FakeElement:
    def __init__(self, box: dict[str, float] | None) -> None:
        self.box = box

    def bounding_box(self) -> dict[str, float] | None:
        return self.box


class FakeMouse:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def move(self, x: float, y: float, steps: int = 1) -> None:
        self.actions.append(f"move {x:g},{y:g}")

    def down(self) -> None:
        self.actions.append("down")

    def up(self) -> None:
        self.actions.append("up")


class FakePage:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.handlers: dict[str, Any] = {}
        self.events = events
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.mouse = FakeMouse()
        self.boxes: dict[str, dict[str, float] | None] = {
            "canvas": {"x": 10.0, "y": 20.0, "width": 100.0, "height": 60.0}
        }

    def click(self, selector: str, timeout: int = 0) -> None:
        if "missing" in selector:
            raise RuntimeError("not found")
        self.clicked.append(selector)

    def on(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        for name, payload in self.events:
            self.handlers[name](payload)

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def query_selector(self, selector: str) -> Any:
        if selector in self.boxes:
            return FakeElement(self.boxes[selector])
        return object() if selector == "canvas#extra" else None

    def inner_text(self, _selector: str) -> str:
        return "Whiteboard ready"


class FakePlaywright:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.chromium = self

    def __call__(self) -> "FakePlaywright":
        return self

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def launch(self) -> "FakePlaywright":
        return self

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class SmokeCheckTests(unittest.TestCase):
    def test_status_helpers_and_free_port(self) -> None:
        self.assertTrue(smoke_check.status_ok(200))
        self.assertTrue(smoke_check.status_ok(399))
        self.assertFalse(smoke_check.status_ok(400))
        self.assertFalse(smoke_check.status_ok(199))
        self.assertFalse(smoke_check.status_ok(500))
        port = smoke_check.free_port()
        self.assertTrue(1024 <= port <= 65535)

    def test_http_status_answers_missing_and_invalid_urls(self) -> None:
        with LocalHttpServer() as server:
            self.assertEqual(smoke_check.http_status(server.url(), 2), 200)
            self.assertEqual(smoke_check.http_status(server.url("/nope"), 2), 404)
        self.assertIsNone(smoke_check.http_status(server.url(), 1))
        self.assertIsNone(smoke_check.http_status("not a url", 1))

    def test_wait_for_http_polls_until_an_answer_or_the_deadline(self) -> None:
        answers = iter([None, None, 200])
        slept: list[float] = []
        with mock.patch.object(
            smoke_check, "http_status", lambda url, t: next(answers)
        ):
            status = smoke_check.wait_for_http(
                "u", 5, clock=iter([0.0, 0.1, 0.2]).__next__, sleep=slept.append
            )
        self.assertEqual(status, 200)
        self.assertEqual(slept, [smoke_check.POLL_SECONDS, smoke_check.POLL_SECONDS])
        with mock.patch.object(smoke_check, "http_status", lambda url, t: None):
            self.assertIsNone(
                smoke_check.wait_for_http(
                    "u", 1, clock=iter([0.0, 0.5, 1.0]).__next__, sleep=lambda _s: None
                )
            )

    def test_page_expectations(self) -> None:
        none = smoke_check.PageExpectations(None, None, None)
        self.assertEqual(smoke_check.page_expectation_errors(none, False, "Error"), [])
        expectations = smoke_check.PageExpectations("canvas", "Whiteboard")
        self.assertEqual(
            smoke_check.page_expectation_errors(expectations, True, "Whiteboard ready"),
            [],
        )
        self.assertEqual(
            smoke_check.page_expectation_errors(
                expectations, False, "Could not load the board: Failed to fetch"
            ),
            [
                "expected selector not found: canvas",
                "expected text not found: 'Whiteboard'",
                "page text looks like a failure: 'Could not load the board: Failed to fetch'",
            ],
        )
        self.assertEqual(
            smoke_check.snippet("a" * 100 + "ERROR" + "b" * 100, 100),
            "a" * 40 + "ERROR" + "b" * 75,
        )
        self.assertEqual(smoke_check.snippet("x  y\n z", 0), "x y z")

    def test_judge_covers_every_verdict(self) -> None:
        options = smoke_options()
        missing = smoke_check.judge("u", None, options)
        self.assertFalse(missing.passed)
        self.assertEqual(missing.reason, "no HTTP answer within 3s")
        self.assertEqual(smoke_check.judge("u", 500, options).reason, "HTTP 500")
        self.assertTrue(smoke_check.judge("u", 200, options).passed)
        health_ok = mock.patch.object(smoke_check, "health_recheck", lambda *a: None)
        with health_ok, mock.patch.object(smoke_check, "browser_errors", lambda *a: []):
            clean = smoke_check.judge("u", 200, smoke_options(browser=True))
        self.assertTrue(clean.passed)
        with (
            health_ok,
            mock.patch.object(
                smoke_check, "browser_errors", lambda *a: ["console: boom"]
            ),
        ):
            broken = smoke_check.judge("u", 200, smoke_options(browser=True))
        self.assertFalse(broken.passed)
        self.assertEqual(broken.reason, "the smoke interactions failed")
        self.assertEqual(
            broken.lines(),
            [
                "SMOKE=FAIL url=u http=200",
                "reason: the smoke interactions failed",
                "  console: boom",
            ],
        )
        self.assertEqual(clean.lines(), ["SMOKE=PASS url=u http=200"])

    def test_browser_backends_are_tried_in_order(self) -> None:
        cwd = Path.cwd()
        expectations = smoke_check.PageExpectations()
        with mock.patch.object(
            smoke_check, "python_playwright_errors", lambda u, t, e: ["a"]
        ):
            self.assertEqual(
                smoke_check.browser_errors("u", cwd, 1, expectations), ["a"]
            )
        with (
            mock.patch.object(
                smoke_check, "python_playwright_errors", lambda u, t, e: None
            ),
            mock.patch.object(
                smoke_check, "node_playwright_errors", lambda u, c, t, e: ["b"]
            ),
        ):
            self.assertEqual(
                smoke_check.browser_errors("u", cwd, 1, expectations), ["b"]
            )
        with (
            mock.patch.object(
                smoke_check, "python_playwright_errors", lambda u, t, e: None
            ),
            mock.patch.object(
                smoke_check, "node_playwright_errors", lambda u, c, t, e: None
            ),
        ):
            self.assertEqual(
                smoke_check.browser_errors("u", cwd, 1, expectations),
                [smoke_check.BROWSER_MISSING],
            )

    def test_python_playwright_collects_page_and_console_errors(self) -> None:
        def missing(name: str) -> Any:
            raise ImportError(name)

        expectations = smoke_check.PageExpectations("canvas", "Whiteboard")
        with mock.patch.object(smoke_check.importlib, "import_module", missing):
            self.assertIsNone(
                smoke_check.python_playwright_errors("u", 1, expectations)
            )
        page = FakePage(
            [
                ("pageerror", "TypeError: boom"),
                ("console", SimpleNamespace(type="error", text="bad")),
                ("console", SimpleNamespace(type="log", text="fine")),
            ]
        )
        playwright = FakePlaywright(page)
        module = SimpleNamespace(sync_playwright=playwright)
        with mock.patch.object(
            smoke_check.importlib, "import_module", lambda name: module
        ):
            errors = smoke_check.python_playwright_errors("http://x/", 2, expectations)
        self.assertEqual(errors, ["pageerror: TypeError: boom", "console: bad"])
        self.assertEqual(page.visited, ["http://x/"])
        self.assertTrue(playwright.closed)
        with mock.patch.object(
            smoke_check.importlib, "import_module", lambda name: module
        ):
            errors = smoke_check.python_playwright_errors(
                "http://x/", 2, smoke_check.PageExpectations("#app")
            )
        self.assertEqual(errors[-1], "expected selector not found: #app")
        with mock.patch.object(
            smoke_check.importlib, "import_module", lambda name: module
        ):
            plain = smoke_check.python_playwright_errors(
                "http://x/", 2, smoke_check.PageExpectations()
            )
        self.assertEqual(plain, ["pageerror: TypeError: boom", "console: bad"])

    def test_node_playwright_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            expectations = smoke_check.PageExpectations("canvas")
            self.assertIsNone(smoke_check.find_node_playwright(cwd))
            self.assertIsNone(
                smoke_check.node_playwright_errors("u", cwd, 1, expectations)
            )
            module_dir = cwd / "node_modules" / "playwright-core"
            module_dir.mkdir(parents=True)
            self.assertEqual(smoke_check.find_node_playwright(cwd), module_dir)
            calls: list[dict[str, Any]] = []

            def fake_run(command: list[str], **kwargs: Any) -> Any:
                calls.append({"command": command, **kwargs})
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"errors": ["console: x"], "found": false, "body": "ok"}',
                    stderr="",
                )

            with mock.patch.object(smoke_check.subprocess, "run", fake_run):
                self.assertEqual(
                    smoke_check.node_playwright_errors(
                        "http://x/", cwd, 2, expectations
                    ),
                    ["console: x", "expected selector not found: canvas"],
                )
            self.assertEqual(
                calls[0]["command"][3:],
                [str(module_dir), "http://x/", "2000", "canvas"],
            )
            self.assertEqual(calls[0]["timeout"], 32)

            def failing_run(command: list[str], **kwargs: Any) -> Any:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="crash\n"
                )

            with mock.patch.object(smoke_check.subprocess, "run", failing_run):
                self.assertEqual(
                    smoke_check.node_playwright_errors("u", cwd, 1, expectations),
                    ["browser runner failed: crash"],
                )

            def empty_run(command: list[str], **kwargs: Any) -> Any:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(smoke_check.subprocess, "run", empty_run):
                self.assertEqual(
                    smoke_check.node_playwright_errors(
                        "u", cwd, 1, smoke_check.PageExpectations()
                    ),
                    [],
                )

    def test_process_lifecycle_and_signal_fallbacks(self) -> None:
        process = smoke_check.start_process(
            f"{shlex.quote(sys.executable)} -c \"print('hello')\"",
            Path.cwd(),
            dict(os.environ),
        )
        process.wait(timeout=20)
        self.assertEqual(smoke_check.stop_process(process, 5).strip(), "hello")
        fake = FakeProcess()
        self.assertEqual(smoke_check.stop_process(fake), "out")
        self.assertEqual(fake.signals, [smoke_check.signal.SIGTERM])
        stubborn = FakeProcess(timeouts=1, output="late")
        self.assertEqual(smoke_check.stop_process(stubborn, 0.01), "late")
        self.assertEqual(
            stubborn.signals,
            [smoke_check.signal.SIGTERM, smoke_check.signal.SIGKILL],
        )
        self.assertEqual(smoke_check.stop_process(FakeProcess(output="")), "")

        class Vanished(FakeProcess):
            def send_signal(self, signum: int) -> None:
                raise ProcessLookupError

        smoke_check.signal_group(Vanished(), smoke_check.signal.SIGTERM)

    def test_check_starts_the_application_and_always_stops_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            (cwd / "index.html").write_text("<html></html>")
            start = (
                f"{shlex.quote(sys.executable)} -m http.server --bind 127.0.0.1 $PORT"
            )
            outcome = smoke_check.check(smoke_options(start=start, cwd=cwd, timeout=20))
            self.assertTrue(outcome.passed, outcome.lines())
            self.assertEqual(outcome.status, 200)
            self.assertTrue(outcome.url.startswith("http://127.0.0.1:"))
            self.assertIsNone(smoke_check.http_status(outcome.url, 1))
            dead = smoke_check.check(
                smoke_options(
                    start=f"{shlex.quote(sys.executable)} -c \"print('boom')\"",
                    cwd=cwd,
                    timeout=0.5,
                    port=smoke_check.free_port(),
                )
            )
            self.assertFalse(dead.passed)
            self.assertIn("no HTTP answer", dead.reason)
            self.assertIn("process output (tail): boom", dead.errors[0])
        with LocalHttpServer() as server:
            running = smoke_check.check(smoke_options(url=server.url()))
            self.assertTrue(running.passed)
            self.assertEqual(running.url, server.url())

    def test_environment_and_url_helpers(self) -> None:
        options = smoke_options(
            env={"MODE": "test"}, port_env="APP_PORT", path="/health"
        )
        env = smoke_check.build_env(options, 4321)
        self.assertEqual(env["APP_PORT"], "4321")
        self.assertEqual(env["MODE"], "test")
        self.assertNotIn("APP_PORT", smoke_check.build_env(options, None))
        self.assertEqual(
            smoke_check.target_url(options, 4321), "http://127.0.0.1:4321/health"
        )
        self.assertEqual(
            smoke_check.target_url(smoke_options(url="http://h/"), None), "http://h/"
        )
        self.assertEqual(
            smoke_check.parse_env(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"}
        )
        with self.assertRaises(ValueError):
            smoke_check.parse_env(["novalue"])
        with self.assertRaises(ValueError):
            smoke_check.parse_env(["=1"])

    def test_cli_exit_codes(self) -> None:
        with self.assertRaises(ValueError):
            smoke_check.options_from_args(smoke_check.parse_args([]))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(smoke_check.main([]), 2)
        self.assertIn("--start", error.getvalue())
        with LocalHttpServer() as server:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    smoke_check.main(["--url", server.url(), "--env", "K=V"]), 0
                )
            self.assertIn("SMOKE=PASS", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                smoke_check.main(["--url", "http://127.0.0.1:1/", "--timeout", "0.2"]),
                1,
            )
        self.assertIn("SMOKE=FAIL", output.getvalue())
        options = smoke_check.options_from_args(
            smoke_check.parse_args(
                [
                    "--start",
                    "x",
                    "--port",
                    "5",
                    "--browser",
                    "--expect-selector",
                    "canvas",
                    "--expect-text",
                    "Board",
                    "--fail-on-text",
                    "",
                ]
            )
        )
        self.assertEqual((options.port, options.browser, options.start), (5, True, "x"))
        self.assertEqual(
            options.expectations, smoke_check.PageExpectations("canvas", "Board", None)
        )
        self.assertEqual(
            smoke_check.options_from_args(
                smoke_check.parse_args(["--url", "u"])
            ).expectations,
            smoke_check.PageExpectations(None, None, smoke_check.DEFAULT_FAIL_PATTERN),
        )


def sample_state() -> dict[str, Any]:
    return {
        "status": "fail",
        "rerun_command": "rerun",
        "full_rerun_command": "full",
        "fix_prompt": "",
        "thresholds": {"metrics": {"coverage_limit": 100, "complexity_limit": 6}},
        "failures": {
            **EMPTY_FAILURES,
            "functions": [
                {
                    "path": "src/a.ts",
                    "line": 4,
                    "name": "one",
                    "coverage_percent": 50.0,
                    "complexity": 2,
                    "covered_lines": 2,
                    "total_lines": 4,
                },
                {
                    "path": "src/a.ts",
                    "line": 20,
                    "name": "two",
                    "coverage_percent": 100.0,
                    "complexity": 9,
                },
                {
                    "path": "src/b.ts",
                    "line": 1,
                    "name": "three",
                    "coverage_percent": 0.0,
                    "complexity": 1,
                },
            ],
            "checks": [
                {
                    "key": "types",
                    "title": "Types",
                    "summary": "1 error",
                    "details": ["src/b.ts:3 TS2322\nmore"],
                }
            ],
        },
    }


class ReportGroupingTests(unittest.TestCase):
    def test_records_carry_hints_keys_and_metrics(self) -> None:
        records = quality_report.item_records(sample_state())
        self.assertEqual(
            [record["key"] for record in records],
            [
                "function src/a.ts one",
                "function src/a.ts two",
                "function src/b.ts three",
                "check types",
            ],
        )
        self.assertEqual(
            records[0]["hint"], "add a test that reaches its 2 uncovered lines"
        )
        self.assertEqual(records[0]["metric"], "coverage")
        self.assertEqual(
            records[1]["hint"], "complexity 9 > 6: split it into smaller functions"
        )
        self.assertEqual(records[1]["metric"], "complexity")
        self.assertEqual(
            records[2]["hint"], "add a test that reaches its untested paths"
        )
        self.assertEqual(records[3]["path"], quality_report.REPOSITORY)
        self.assertEqual(records[3]["hint"], "src/b.ts:3 TS2322")
        self.assertEqual(records[3]["details"], ["src/b.ts:3 TS2322"])

    def test_function_hint_covers_craap_and_the_fallback(self) -> None:
        limits = {"coverage_limit": 100.0, "complexity_limit": 6.0, "craap_limit": 6.0}
        item = {"coverage_percent": 100.0, "complexity": 1, "craap_score": 7.5}
        self.assertEqual(
            quality_report.function_hint(item, limits),
            "CRAAP 7.5 > 6: cover it or simplify it",
        )
        item = {"coverage_percent": 100.0, "complexity": 1}
        self.assertEqual(
            quality_report.function_hint(item, limits), "cover it or simplify it"
        )
        both = {"coverage_percent": 10.0, "complexity": 7, "craap_score": 1}
        self.assertEqual(quality_report.function_hint(both, limits).count(";"), 1)
        self.assertEqual(
            quality_report.metric_limits({}),
            {
                "coverage_limit": 100.0,
                "branch_coverage_limit": 100.0,
                "complexity_limit": 6.0,
                "craap_limit": 6.0,
            },
        )
        self.assertEqual(
            quality_report.metric_limits(
                {"thresholds": {"metrics": {"craap_limit": 3}}}
            )["craap_limit"],
            3.0,
        )

    def test_files_are_ordered_by_count_then_path_with_repository_last(self) -> None:
        records = quality_report.item_records(sample_state())
        groups = quality_report.files_in_order(records)
        self.assertEqual(
            [path for path, _ in groups], ["src/a.ts", "src/b.ts", "(repository)"]
        )
        self.assertEqual(
            quality_report.metric_breakdown(groups[0][1]), "1 complexity · 1 coverage"
        )
        self.assertEqual(
            quality_report.file_line(2, "src/b.ts", groups[1][1]),
            "  2. src/b.ts — 1 item (1 coverage)",
        )
        tie = [
            quality_report.file_record({"path": "z.py", "lines": 1, "limit": 0}),
            quality_report.file_record({"path": "y.py", "lines": 1, "limit": 0}),
        ]
        self.assertEqual(
            [path for path, _ in quality_report.files_in_order(tie)], ["y.py", "z.py"]
        )

    def test_other_record_kinds_have_hints(self) -> None:
        mutant = quality_report.mutant_record(
            {"path": "a.py", "line": 4, "change": "== -> !="}
        )
        self.assertEqual(mutant["text"], "a.py:4 surviving mutant `== -> !=`")
        self.assertEqual(
            mutant["hint"], "add a test that fails when the code changes `== -> !=`"
        )
        self.assertEqual(mutant["metric"], "mutant")
        dependency = quality_report.dependency_record(
            {"source": "a.py", "line": 1, "target": "b.py", "rule": "deny"}
        )
        self.assertEqual(dependency["key"], "dependency a.py -> b.py")
        self.assertIn("remove the import of b.py", dependency["hint"])
        scope = quality_report.scope_record("src/x.ts — excluded by source.exclude")
        self.assertEqual(scope["path"], "src/x.ts")
        self.assertEqual(scope["line"], 0)
        plain = quality_report.check_record(
            {"key": "lint", "title": "Lint", "summary": "2"}
        )
        self.assertEqual(plain["hint"], "read the command output in the report")
        self.assertEqual(plain["details"], [])

    def test_delta_line_and_previous_keys(self) -> None:
        self.assertIsNone(quality_report.delta_line(None, {"a"}))
        self.assertEqual(
            quality_report.delta_line({"a", "b"}, {"b", "c"}),
            "Since last run: fixed 1 · remaining 2 · new 1",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            self.assertIsNone(quality_report.previous_item_keys(path))
            path.write_text("{}", encoding="utf-8")
            self.assertIsNone(quality_report.previous_item_keys(path))
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(quality_report.previous_item_keys(path))
            path.write_text(json.dumps(sample_state()), encoding="utf-8")
            self.assertEqual(
                quality_report.previous_item_keys(path),
                quality_report.item_keys(sample_state()),
            )
            self.assertEqual(len(quality_report.item_keys(sample_state())), 4)

    def test_fast_green_tells_the_agent_to_commit_and_continue(self) -> None:
        state = {
            "status": "ready_for_full",
            "rerun_command": "r",
            "full_rerun_command": "f",
        }
        lines = quality_report.next_step_lines(state, SimpleNamespace(mode="fast"))
        self.assertEqual(
            lines,
            [
                quality_report.COMMIT_LINE,
                quality_report.COMMIT_COMMAND,
                quality_report.CONTINUE_LINE,
                "  f",
            ],
        )
        self.assertIn("git add -A && git commit -m", lines[1])

    def test_empty_incremental_scope_never_reads_as_certification(self) -> None:
        state = {
            "status": "fail",
            "rerun_command": "r",
            "full_rerun_command": "python3 loop.py --root . --local-changes",
            "scope": {"kind": "local_changes", "reference": None, "changed_files": []},
        }
        self.assertTrue(quality_report.empty_incremental_scope(state))
        lines = quality_report.next_step_lines(state, SimpleNamespace(mode="fast"))
        self.assertEqual(lines[0], quality_report.EMPTY_SCOPE_LINE)
        self.assertEqual(lines[1], "Run the whole-repository ship report:")
        self.assertEqual(lines[2], "  python3 loop.py --root .")
        self.assertEqual(
            quality_report.whole_repository_command(
                "l.py --root . --commit HEAD --fast"
            ),
            "l.py --root . --fast",
        )
        state["scope"] = {"kind": "local_changes", "changed_files": ["a.ts"]}
        self.assertFalse(quality_report.empty_incremental_scope(state))
        self.assertEqual(
            quality_report.next_step_lines(state, SimpleNamespace(mode="fast"))[0],
            "Fix the items above, then rerun:",
        )
        self.assertFalse(quality_report.empty_incremental_scope({"status": "fail"}))
        commit_scope = {"kind": "commit", "reference": "HEAD", "changed_files": []}
        self.assertTrue(quality_report.empty_incremental_scope({"scope": commit_scope}))

    def test_report_prints_delta_and_next_file(self) -> None:
        analysis = SimpleNamespace(
            mode="fast",
            selection=["fast"],
            scope=SimpleNamespace(description="d"),
            gates=[],
            thresholds={},
            functions=[],
        )
        state = sample_state()
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                quality_report.print_report(
                    gate,
                    analysis,
                    state,
                    state_path,
                    Path("h"),
                    False,
                    None,
                    {"check types"},
                )
            text = output.getvalue()
            self.assertIn("Since last run: fixed 0 · remaining 4 · new 3\n", text)
            self.assertIn(
                "To fix:\n  1. src/a.ts:4 one — coverage 50%, complexity 2\n", text
            )
            self.assertIn(
                f"Next file:  python3 {ITEMS_SCRIPT} --state {state_path} --next\n",
                text,
            )
            self.assertIn("ITEMS_TO_FIX=4\n", text)
            output = io.StringIO()
            state["failures"] = dict(EMPTY_FAILURES)
            state["status"] = "ready_for_full"
            with contextlib.redirect_stdout(output):
                quality_report.print_report(
                    gate, analysis, state, state_path, Path("h"), False
                )
            text = output.getvalue()
            self.assertNotIn("Since last run", text)
            self.assertNotIn("To fix", text)
            self.assertNotIn("Next file", text)
            self.assertIn("ITEMS_TO_FIX=0\n", text)


class ItemsScriptTests(unittest.TestCase):
    def run_items(
        self, *arguments: str, state: dict[str, Any] | None = None
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if state is not None:
                state_path = root / quality_items.STATE_NAME
                state_path.parent.mkdir()
                state_path.write_text(json.dumps(state), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = quality_items.main(["--root", str(root), *arguments])
            return code, out.getvalue(), err.getvalue()

    def test_next_prints_the_first_file_with_items_and_hints(self) -> None:
        code, out, _ = self.run_items("--next", state=sample_state())
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "src/a.ts — 2 items (1 complexity · 1 coverage)")
        self.assertEqual(
            lines[1], "  L4     src/a.ts:4 one — coverage 50%, complexity 2"
        )
        self.assertEqual(
            lines[2], "         → add a test that reaches its 2 uncovered lines"
        )
        self.assertEqual(
            lines[3], "  L20    src/a.ts:20 two — coverage 100%, complexity 9"
        )
        self.assertEqual(
            lines[-1],
            "When this file is green, rerun the fast run. 2 more files after this one.",
        )
        code, out, _ = self.run_items(state=sample_state())
        self.assertEqual(
            out.splitlines()[0], "src/a.ts — 2 items (1 complexity · 1 coverage)"
        )

    def test_file_matches_a_path_or_a_file_name_and_shows_check_details(self) -> None:
        code, out, _ = self.run_items("--file", "b.ts", state=sample_state())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "src/b.ts — 1 item (1 coverage)")
        self.assertEqual(
            out.splitlines()[-1],
            "When this file is green, rerun the fast run. 2 more files after this one.",
        )
        code, out, _ = self.run_items("--file", "(repository)", state=sample_state())
        lines = out.splitlines()
        self.assertEqual(lines[0], "(repository) — 1 item (1 Types)")
        self.assertEqual(lines[1], "  —      Types: 1 error")
        self.assertEqual(lines[3], "         src/b.ts:3 TS2322")
        code, out, _ = self.run_items("--file", "nope.ts", state=sample_state())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "No open items in nope.ts.")
        self.assertEqual(
            out.splitlines()[1], "4 items in 3 files — one file per cycle, top first"
        )

    def test_summary_lists_files_and_counts(self) -> None:
        code, out, _ = self.run_items("--summary", state=sample_state())
        self.assertEqual(
            out.splitlines(),
            [
                "4 items in 3 files — one file per cycle, top first",
                "  1. src/a.ts — 2 items (1 complexity · 1 coverage)",
                "  2. src/b.ts — 1 item (1 coverage)",
                "  3. (repository) — 1 item (1 Types)",
            ],
        )

    def test_briefs_skip_repository_items_and_carry_the_rules(self) -> None:
        code, out, _ = self.run_items("--briefs", "1", state=sample_state())
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "=== brief 1 of 1: src/a.ts ===")
        self.assertEqual(
            lines[1],
            "You own src/a.ts and its test file. Bring these 2 items to green:",
        )
        self.assertTrue(
            lines[2].startswith(
                "  - src/a.ts:4 one — coverage 50%, complexity 2 → add a test"
            )
        )
        self.assertEqual(lines[4], quality_items.BRIEF_RULES)
        self.assertEqual(lines[5], quality_items.PARENT_LINE)
        code, out, _ = self.run_items("--briefs", "5", state=sample_state())
        self.assertEqual(out.splitlines()[0], "=== brief 1 of 2: src/a.ts ===")
        self.assertNotIn("(repository)", out)
        code, out, _ = self.run_items("--briefs", "0", state=sample_state())
        self.assertEqual(out.strip(), "No file-level items to delegate.")

    def test_empty_state_and_missing_state(self) -> None:
        empty = {**sample_state(), "failures": dict(EMPTY_FAILURES)}
        for flag in ("--next", "--summary"):
            code, out, _ = self.run_items(flag, state=empty)
            self.assertEqual((code, out.strip()), (0, quality_items.NOTHING_TO_FIX))
        code, out, err = self.run_items("--next")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: cannot read", err)
        self.assertIn("Run the quality loop first", err)

    def test_state_option_overrides_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "elsewhere.json"
            path.write_text(json.dumps(sample_state()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = quality_items.main(["--state", str(path), "--summary"])
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().startswith("4 items in 3 files"))
            args = quality_items.parse_args(["--root", "/r"])
            self.assertEqual(
                quality_items.state_path_for(args),
                Path("/r") / quality_items.STATE_NAME,
            )


class InitGitTests(unittest.TestCase):
    def test_init_creates_a_git_repository_only_when_none_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                gate.ensure_git_repository(root),
                "Initialized an empty git repository: commit each green step (SKILL.md rule 8).",
            )
            self.assertTrue((root / ".git").is_dir())
            self.assertIsNone(gate.ensure_git_repository(root))

    def test_init_reports_when_git_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                gate.subprocess, "run", side_effect=OSError("no git")
            ):
                self.assertEqual(
                    gate.ensure_git_repository(root),
                    "git is not available here: commit each green step once it is.",
                )
            self.assertFalse((root / ".git").exists())

    def test_init_command_prints_the_git_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = subprocess.run(
                [sys.executable, str(CORE_SCRIPT), "--root", str(root), "--init"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Initialized an empty git repository", completed.stdout)
            self.assertTrue((root / ".git").is_dir())


class AdvancedQualityMetricTests(unittest.TestCase):
    def test_smoke_story_fails_when_a_json_probe_fails_despite_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "probe.py"
            script.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "steps = [{'step': f'probe {i}', 'ok': i != 7} for i in range(10)]\n"
                "Path(sys.argv[1]).write_text(json.dumps({'steps': steps, 'page_errors': []}))\n",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["smoke"]["story"] = {
                "name": "whiteboard core user story",
                "command": [sys.executable, str(script), "{report}"],
                "report": ".quality/ui-check.json",
                "format": "steps-json",
                "minimum_probes": 10,
                "fail_on_page_errors": True,
            }

            result = gate.smoke_gate_for_run(
                root, config, False, gate.gate_selection(["smoke"])
            )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.smoke_probes), 10)
        self.assertIn("9/10", result.summary)
        self.assertIn("probe 7", result.details[0])

    def test_smoke_story_accepts_ten_probes_and_report_dir_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "probe.py"
            script.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "out = Path(sys.argv[1])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "steps = [{'step': f'probe {i}', 'ok': True} for i in range(10)]\n"
                "(out / 'report.json').write_text(json.dumps({'steps': steps, 'page_errors': []}))\n",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["smoke"]["story"] = {
                "name": "core story",
                "command": [sys.executable, str(script), "{report_dir}"],
                "report": ".quality/ui-check/report.json",
                "minimum_probes": 10,
            }

            result = gate.smoke_gate_for_run(
                root, config, False, gate.gate_selection(["smoke"])
            )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.smoke_probes), 10)
        self.assertIn("10/10", result.summary)

    def test_smoke_story_rejects_page_errors_and_too_few_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "steps": [{"name": "loads", "ok": True}],
                        "page_errors": ["paint crashed"],
                    }
                ),
                encoding="utf-8",
            )
            probes, page_errors = gate.load_smoke_story_report(report, "steps-json")

        self.assertEqual(probes, [gate.SmokeProbe("loads", True, "")])
        self.assertEqual(page_errors, ["paint crashed"])

    def test_smoke_story_requires_a_steps_array(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text('{"ok": true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "steps"):
                gate.load_smoke_story_report(report, "steps-json")

    def test_catch_coverage_prefers_lcov_branch_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src" / "load.ts"
            source.parent.mkdir()
            source.write_text(
                "export function load() {\n"
                "  try { risky(); }\n"
                "  catch (error) {\n"
                "    return fallback();\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            lcov = root / "lcov.info"
            lcov.write_text(
                "SF:src/load.ts\nDA:4,0\nBRDA:3,0,0,1\nBRF:1\nBRH:1\nend_of_record\n",
                encoding="utf-8",
            )

            coverage = gate.parse_lcov(lcov, root)
            paths = gate.scan_error_paths(source, root, coverage)

        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].covered)
        self.assertTrue(paths[0].coverage_measured)
        self.assertEqual(paths[0].coverage_kind, "branch")

    def test_uncovered_catch_branch_overrides_a_covered_handler_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "load.ts"
            source.write_text(
                "function load() {\n"
                "  try { risky(); }\n"
                "  catch (error) {\n"
                "    return fallback();\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            coverage = gate.CoverageData(
                lines={"load.ts": {4: 1}}, branches={"load.ts": {3: [0]}}
            )

            paths = gate.scan_error_paths(source, root, coverage)

        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].covered)
        self.assertEqual(paths[0].coverage_kind, "branch")

    def test_catch_coverage_falls_back_to_lines_without_branch_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "load.ts"
            source.write_text(
                "try { risky(); } catch (error) { recover(); }\n", encoding="utf-8"
            )
            coverage = gate.CoverageData(lines={"load.ts": {1: 1}})

            paths = gate.scan_error_paths(source, root, coverage)

        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].covered)
        self.assertEqual(paths[0].coverage_kind, "line")

    def test_composed_root_rejects_same_package_mock_and_null_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "packages" / "client"
            source = package / "src" / "api" / "client.ts"
            test = package / "test" / "app" / "App.test.tsx"
            package.mkdir(parents=True)
            (package / "package.json").write_text('{"name":"client"}', encoding="utf-8")
            source.parent.mkdir(parents=True)
            test.parent.mkdir(parents=True)
            source.write_text("export const load = () => 1;\n", encoding="utf-8")
            test.write_text(
                'vi.mock("../../src/api/client.js");\n'
                "vi.spyOn(HTMLCanvasElement.prototype, 'getContext')\n"
                "  .mockReturnValue(null);\n",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["test_integrity"].update(
                {
                    "enabled": True,
                    "required": True,
                    "composed_root_tests": ["**/App.test.*"],
                }
            )

            result, violations = gate.run_test_integrity_gate(
                root, config, [source], [source, test]
            )

            self.assertFalse(result.passed)
            self.assertEqual(
                {item.kind for item in violations},
                {"same-package mock", "null render surface"},
            )
            self.assertEqual({item.line for item in violations}, {1, 2})

            test.write_text('vi.mock("react");\n', encoding="utf-8")
            passing, remaining = gate.run_test_integrity_gate(
                root, config, [source], [source, test]
            )
            self.assertTrue(passing.passed)
            self.assertEqual(remaining, [])

            helper = package / "test" / "helpers" / "Widget.test.tsx"
            helper.parent.mkdir()
            helper.write_text('vi.mock("../../src/api/client.js");\n', encoding="utf-8")
            not_applicable, _ = gate.run_test_integrity_gate(
                root,
                {
                    **config,
                    "test_integrity": {
                        **config["test_integrity"],
                        "required": False,
                    },
                },
                [source],
                [source, helper],
            )
            self.assertFalse(not_applicable.applicable)

    def test_thresholds_gain_new_goals_from_the_bundled_defaults(self) -> None:
        defaults = gate.default_thresholds()
        legacy = gate.default_thresholds()
        legacy["schema_version"] = 1
        legacy["metrics"].pop("branch_coverage_limit")
        legacy["metrics"]["complexity_limit"] = 4
        legacy.pop("slow_tests")
        legacy.pop("extensibility")
        legacy.pop("error_handling")

        upgraded, added = gate.upgrade_thresholds(legacy, defaults)
        gate.validate_thresholds(upgraded)

        self.assertEqual(
            added,
            [
                "metrics.branch_coverage_limit",
                "slow_tests",
                "extensibility",
                "error_handling",
                "schema_version",
            ],
        )
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual(upgraded["metrics"]["complexity_limit"], 4)
        self.assertEqual(upgraded["metrics"]["branch_coverage_limit"], 100)
        self.assertEqual(upgraded["slow_tests"], defaults["slow_tests"])
        self.assertEqual(upgraded["extensibility"], defaults["extensibility"])
        self.assertEqual(upgraded["error_handling"], defaults["error_handling"])
        self.assertEqual(legacy["schema_version"], 1)
        self.assertNotIn("slow_tests", legacy)

        self.assertEqual(gate.upgrade_thresholds(defaults, defaults), (defaults, []))

        broken = dict(defaults)
        broken["slow_tests"] = "fast"
        broken["schema_version"] = "2"
        untouched, added = gate.upgrade_thresholds(broken, defaults)
        self.assertEqual(added, [])
        self.assertEqual(untouched["slow_tests"], "fast")
        self.assertEqual(untouched["schema_version"], "2")
        with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
            gate.validate_thresholds(untouched)
        untouched["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "slow_tests"):
            gate.validate_thresholds(untouched)

        newer = dict(defaults)
        newer["schema_version"] = 3
        self.assertEqual(gate.upgrade_thresholds(newer, defaults)[1], [])

    def test_loading_a_repository_file_applies_new_goals_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quality-thresholds.json"
            legacy = gate.default_thresholds()
            legacy["schema_version"] = 1
            legacy.pop("error_handling")
            path.write_text(json.dumps(legacy), encoding="utf-8")

            loaded, notes = gate.load_thresholds(path)

            self.assertEqual(loaded["schema_version"], 2)
            self.assertIn("error_handling", loaded)
            self.assertEqual(len(notes), 1)
            self.assertIn("new goals: error_handling, schema_version", notes[0])
            self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

            bundled, bundled_notes = gate.load_thresholds(
                gate.bundled_thresholds_path()
            )
            self.assertEqual(bundled, gate.default_thresholds())
            self.assertNotIn("new goals", bundled_notes[0])

            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                gate.load_thresholds(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cannot read"):
                gate.load_thresholds(path)

    def test_sync_writes_new_goals_into_the_repository_thresholds_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quality-thresholds.json"
            legacy = gate.default_thresholds()
            legacy["schema_version"] = 1
            legacy["file_loc"]["max_lines"] = 250
            legacy.pop("slow_tests")
            legacy["metrics"].pop("branch_coverage_limit")
            path.write_text(json.dumps(legacy), encoding="utf-8")
            path.chmod(0o600)

            notes = gate.sync_thresholds_file(path)

            self.assertEqual(len(notes), 1)
            self.assertIn(str(path), notes[0])
            self.assertIn(
                "metrics.branch_coverage_limit, slow_tests, schema_version", notes[0]
            )
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], 2)
            self.assertEqual(written["file_loc"]["max_lines"], 250)
            self.assertEqual(written["metrics"]["branch_coverage_limit"], 100)
            self.assertEqual(
                written["slow_tests"], gate.default_thresholds()["slow_tests"]
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("}\n"))
            self.assertEqual(gate.sync_thresholds_file(path), [])

            path.write_text(
                json.dumps({"schema_version": 1, "unknown": {}}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "unknown keys: unknown"):
                gate.sync_thresholds_file(path)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema_version": 1, "unknown": {}},
            )

        bundled = gate.bundled_thresholds_path()
        before = bundled.read_bytes()
        self.assertEqual(gate.sync_thresholds_file(bundled), [])
        self.assertEqual(bundled.read_bytes(), before)

    def test_branch_coverage_is_measured_per_function(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "choose.py"
            source.write_text(
                "def choose(flag):\n    if flag:\n        return 1\n    return 0\n",
                encoding="utf-8",
            )
            report = root / "coverage.json"
            report.write_text(
                json.dumps(
                    {
                        "meta": {"branch_coverage": True},
                        "files": {
                            str(source): {
                                "executed_lines": [1, 2, 3, 4],
                                "missing_lines": [],
                                "executed_branches": [[2, 3]],
                                "missing_branches": [[2, 4]],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            coverage = gate.parse_coverage_json(report, root)
            functions = gate.build_function_metrics(
                [source],
                root,
                coverage,
                branch_coverage_limit=100,
                branch_coverage_required=True,
            )

        self.assertEqual(functions[0].covered_branches, 1)
        self.assertEqual(functions[0].total_branches, 2)
        self.assertEqual(functions[0].branch_coverage_percent, 50)
        self.assertTrue(functions[0].branch_coverage_measured)
        self.assertFalse(functions[0].passed)

    def test_junit_report_identifies_slow_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            junit = root / "junit.xml"
            junit.write_text(
                """<testsuite name="suite" tests="2" time="6.3">
                <testcase classname="tests.Fast" name="test_fast" file="tests/test_fast.py" time="0.1" />
                <testcase classname="tests.Slow" name="test_slow" file="tests/test_slow.py" time="6.2" />
                </testsuite>""",
                encoding="utf-8",
            )
            config = gate.default_config()
            config["slow_tests"].update(
                {"report": str(junit), "format": "junit", "max_test_seconds": 5}
            )
            baseline = gate.CommandResult(["tests"], 0, "ok", duration_seconds=6.3)

            result, timings = gate.run_slow_test_gate(root, config, baseline, root)

        self.assertFalse(result.passed)
        self.assertEqual(
            [item.name for item in timings],
            ["tests.Slow.test_slow", "tests.Fast.test_fast"],
        )
        self.assertIn("test_slow", result.details[0])

    def test_unittest_and_pytest_console_timings_are_supported(self) -> None:
        timings = gate.console_test_timings(
            """\
1.250s     test_slow (tests.test_api.ApiTests.test_slow)
0.200s call     tests/test_web.py::test_page
"""
        )

        self.assertEqual([item.duration_seconds for item in timings], [1.25, 0.2])
        self.assertEqual(timings[0].path, "tests/test_api.py")
        self.assertEqual(timings[1].path, "tests/test_web.py")

    def test_requested_extended_metrics_cannot_be_disabled(self) -> None:
        config = gate.default_config()
        config["slow_tests"]["enabled"] = False
        config["extensibility"]["enabled"] = False
        config["error_handling"]["enabled"] = False
        baseline = gate.CommandResult(["tests"], 0, "ok", duration_seconds=0.1)

        slow, _ = gate.run_slow_test_gate(Path.cwd(), config, baseline, Path.cwd())
        extensibility, _, _ = gate.run_extensibility_gate(Path.cwd(), config, [], [])
        errors = gate.run_error_handling_gate(config, [])

        self.assertFalse(slow.passed)
        self.assertFalse(extensibility.passed)
        self.assertFalse(errors.passed)

    def test_extensibility_checks_contracts_and_core_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = root / "core" / "service.py"
            extension = root / "extensions" / "plugin.py"
            core.parent.mkdir()
            extension.parent.mkdir()
            core.write_text("from extensions.plugin import VALUE\n", encoding="utf-8")
            extension.write_text("VALUE = 1\n", encoding="utf-8")
            config = gate.default_config()
            config["extensibility"].update(
                {
                    "core": ["core/**"],
                    "extensions": ["extensions/**"],
                    "scenarios": [
                        {
                            "name": "load a configured plugin",
                            "command": [sys.executable, "-c", "pass"],
                        }
                    ],
                }
            )

            result, scenarios, dependencies = gate.run_extensibility_gate(
                root, config, [core, extension], [core, extension]
            )

        self.assertFalse(result.passed)
        self.assertTrue(scenarios[0].passed)
        self.assertEqual(len(dependencies), 1)
        self.assertEqual(dependencies[0].source, "core/service.py")
        self.assertEqual(dependencies[0].target, "extensions/plugin.py")

    def test_error_paths_measure_coverage_and_silent_handlers(self) -> None:
        source_text = """\
def ignored():
    try:
        risky()
    except ValueError:
        pass

def handled():
    try:
        risky()
    except OSError:
        return None
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "errors.py"
            source.write_text(source_text, encoding="utf-8")
            coverage = gate.CoverageData(
                lines={"errors.py": {5: 1, 11: 0}}, branch_capable=True
            )
            paths = gate.scan_error_paths(source, root, coverage)
            config = gate.default_config()
            result = gate.run_error_handling_gate(config, paths)

        self.assertEqual(len(paths), 2)
        self.assertEqual(sum(item.covered for item in paths), 1)
        self.assertEqual(sum(item.silent for item in paths), 1)
        self.assertFalse(result.passed)
        self.assertIn("50% failure-path coverage", result.summary)
        self.assertIn("1 silent", result.summary)

    def test_normalized_adapter_cannot_hide_an_unreported_error_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "errors.py"
            source.write_text(
                "def ignored():\n"
                "    try:\n"
                "        risky()\n"
                "    except ValueError:\n"
                "        pass\n",
                encoding="utf-8",
            )
            function = gate.FunctionMetric(
                "errors.py", "ignored", 1, 5, 2, 3, 3, 100, 2, "adapter"
            )

            paths = gate.merged_error_paths([source], root, [function])
            result = gate.run_error_handling_gate(gate.default_config(), paths)

        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].coverage_measured)
        self.assertTrue(paths[0].silent)
        self.assertFalse(result.passed)

    def test_branch_failure_is_actionable_in_terminal_items(self) -> None:
        item = {
            "path": "src/app.ts",
            "line": 4,
            "name": "choose",
            "coverage_percent": 100,
            "branch_coverage_percent": 0,
            "branch_coverage_measured": False,
            "complexity": 2,
            "craap_score": 2,
        }

        record = quality_report.function_record(
            item, quality_report.metric_limits({"thresholds": {}})
        )

        self.assertEqual(record["metric"], "branch coverage")
        self.assertIn("branch coverage not measured", record["text"])
        self.assertIn("report branch outcomes", record["hint"])

        limits = quality_report.metric_limits({"thresholds": {}})
        measured = {
            **item,
            "branch_coverage_measured": True,
            "branch_coverage_percent": 50,
        }
        measured_record = quality_report.function_record(measured, limits)
        self.assertIn("branch coverage 50%", measured_record["text"])
        self.assertIn("uncovered branch outcomes", measured_record["hint"])
        self.assertFalse(
            quality_report.branch_failure(
                {**measured, "branch_coverage_percent": 100}, limits
            )
        )
        self.assertIsNone(
            quality_report.branch_hint(
                {**measured, "branch_coverage_percent": 100}, limits
            )
        )
        self.assertEqual(quality_report.branch_failure_text(item, False), "")
        self.assertEqual(quality_report.function_metric(True, True), "coverage")
        self.assertEqual(quality_report.function_metric(False, False), "complexity")

    def test_state_and_html_show_all_six_metrics(self) -> None:
        function = gate.FunctionMetric(
            path="app.py",
            name="choose",
            start_line=1,
            end_line=4,
            complexity=2,
            covered_lines=4,
            total_lines=4,
            coverage_percent=100,
            craap_score=2,
            parser="python-ast",
            covered_branches=2,
            total_branches=2,
            branch_coverage_percent=100,
            branch_coverage_measured=True,
            branch_coverage_required=True,
        )
        timing = gate.TestTiming("suite.test", "tests/test_app.py", 1.25, "passed")
        scenario = gate.ExtensionScenario("load plugin", True, 0.1, "")
        error_path = gate.ErrorPath(
            "app.py", 4, "except ValueError", True, False, "python-ast"
        )
        probe = gate.SmokeProbe("draw three shapes", True, "count=3")
        integrity = gate.TestIntegrityViolation(
            "tests/App.test.tsx",
            4,
            "same-package mock",
            "src/api.ts",
            "composed-root test mocks production module src/api.ts",
        )
        report = gate.AnalysisReport(
            root="/tmp/example",
            generated_at="now",
            languages=["Python"],
            gates=[
                gate.GateResult("quality", "Quality", True, "ok"),
                gate.GateResult(
                    "smoke", "Runs (smoke)", True, "1/1", smoke_probes=[probe]
                ),
                gate.GateResult("test_integrity", "Test integrity", False, "1"),
            ],
            functions=[function],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
            test_timings=[timing],
            suite_duration_seconds=1.5,
            extension_scenarios=[scenario],
            extension_dependencies=[],
            error_paths=[error_path],
            smoke_probes=[probe],
            test_integrity_violations=[integrity],
            thresholds=gate.default_thresholds(),
        )

        rendered = gate.html_report(report)
        state = gate.analysis_state(
            gate,
            report,
            Path("report.html"),
            Path("state.json"),
            0,
        )

        for label in (
            "Branch coverage",
            "Slowest test",
            "Extension contracts",
            "Core → extension",
            "Failure-path coverage",
            "Silent handlers",
            "Core user story probes",
            "Anti-vacuous mock findings",
        ):
            with self.subTest(label=label):
                self.assertIn(label, rendered)
        self.assertEqual(
            state["metrics"]["functions"][0]["branch_coverage_percent"], 100
        )
        self.assertEqual(state["metrics"]["slow_tests"][0]["duration_seconds"], 1.25)
        self.assertEqual(
            state["metrics"]["extensibility"]["contract_coverage_percent"], 100
        )
        self.assertEqual(
            state["metrics"]["error_handling"]["failure_path_coverage_percent"], 100
        )
        self.assertEqual(state["metrics"]["smoke_story"][0]["name"], probe.name)
        self.assertEqual(
            state["failures"]["test_integrity"][0]["kind"], "same-package mock"
        )
        records = quality_report.item_records(state)
        self.assertIn("tests/App.test.tsx", [item["path"] for item in records])


class EmptyScopeAnalysisTests(unittest.TestCase):
    def analysis(self, scope: object) -> object:
        passing = SimpleNamespace(passed=True, skipped=False, deferred=False)
        return gate.AnalysisReport(
            root=".",
            generated_at="now",
            languages=[],
            gates=[passing],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=[],
            mode="fast",
            scope=scope,
        )

    def test_empty_local_changes_scope_never_passes_or_readies(self) -> None:
        empty = gate.GateScope("local_changes", ())
        report = self.analysis(empty)
        self.assertTrue(report.scope_is_empty)
        self.assertFalse(report.passed)
        self.assertFalse(report.ready_for_full)
        self.assertEqual(gate.state_status(report, None), "fail")

    def test_populated_and_repository_scopes_still_certify(self) -> None:
        populated = self.analysis(gate.GateScope("local_changes", ("a.ts",)))
        self.assertFalse(populated.scope_is_empty)
        self.assertTrue(populated.ready_for_full)
        self.assertTrue(populated.selected_passed)
        whole = self.analysis(gate.repository_scope())
        whole.mode = "full"
        self.assertFalse(whole.scope_is_empty)
        self.assertTrue(whole.passed)
        empty_commit = self.analysis(gate.GateScope("commit", (), "HEAD"))
        empty_commit.mode = "full"
        self.assertFalse(empty_commit.passed)
        self.assertFalse(empty_commit.selected_passed)


class InitScaleMessageTests(unittest.TestCase):
    def seeded_root(self, temporary: str, files: int) -> Path:
        root = Path(temporary)
        for index in range(files):
            (root / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")
        return root

    def test_small_repositories_get_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.seeded_root(temporary, 3)
            self.assertIsNone(gate.init_scale_message(root, {}))

    def test_new_project_with_many_files_gets_the_rule_1_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.seeded_root(temporary, 11)
            message = gate.init_scale_message(root, {})
            assert message is not None
            self.assertIn("WARNING: 11 source files", message)
            self.assertIn("Rule 1", message)
            self.assertIn("rule 9", message)

    def test_repository_with_history_gets_the_adoption_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.seeded_root(temporary, 12)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                [*git, "commit", "-qm", "one"], check=True, capture_output=True
            )
            (root / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                [*git, "commit", "-qm", "two"], check=True, capture_output=True
            )
            self.assertEqual(gate.commit_count(root), 2)
            message = gate.init_scale_message(root, {})
            assert message is not None
            self.assertIn("Existing repository: 13 source files", message)
            self.assertIn("quality_items.py", message)

    def test_commit_count_is_zero_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(gate.commit_count(Path(temporary)), 0)


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        status = 500 if "boom" in self.path else 200
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def log_message(self, *args: Any) -> None:
        return None


class SmokeWritePathTests(unittest.TestCase):
    def serve(self) -> tuple[Any, str]:
        server = http.server.HTTPServer(("127.0.0.1", 0), ProbeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}"

    def test_parse_probe_accepts_method_path_and_optional_body(self) -> None:
        self.assertEqual(
            smoke_check.parse_probe("post /api/x {}"), ("POST", "/api/x", "{}")
        )
        self.assertEqual(smoke_check.parse_probe("GET /"), ("GET", "/", None))
        for bad in ("nope", "POST api", "1 /x"):
            with self.assertRaises(ValueError):
                smoke_check.parse_probe(bad)

    def test_run_probe_flags_5xx_and_no_answer_only(self) -> None:
        server, base = self.serve()
        try:
            self.assertIsNone(smoke_check.run_probe(base, "POST /api/ok {}", 5))
            error = smoke_check.run_probe(base, "POST /api/boom {}", 5)
            assert error is not None
            self.assertIn("HTTP 500", error)
        finally:
            server.shutdown()
        dead = smoke_check.run_probe("http://127.0.0.1:9", "GET /", 0.3)
        assert dead is not None
        self.assertIn("no answer", dead)
        self.assertEqual(smoke_check.probe_errors(base, (), 1), [])
        with mock.patch.object(
            smoke_check, "run_probe", side_effect=[None, "probe boom", None]
        ):
            collected = smoke_check.probe_errors(base, ("a", "b", "c"), 1)
        self.assertEqual(collected, ["probe boom"])

    def test_health_recheck_reports_a_crashed_application(self) -> None:
        server, base = self.serve()
        try:
            self.assertIsNone(smoke_check.health_recheck(base + "/", 5))
        finally:
            server.shutdown()
        message = smoke_check.health_recheck("http://127.0.0.1:9/", 0.3)
        assert message is not None
        self.assertIn("stopped answering after the smoke interactions", message)
        self.assertIn("crashed on a write", message)

    def test_judge_fails_when_the_app_dies_after_the_probes(self) -> None:
        options = smoke_check.SmokeOptions(
            start=None,
            cwd=Path("."),
            url="http://127.0.0.1:9/",
            path="/",
            port=None,
            port_env="PORT",
            browser=False,
            timeout=0.3,
            env={},
            probes=("GET /",),
        )
        outcome = smoke_check.judge("http://127.0.0.1:9/", 200, options)
        self.assertFalse(outcome.passed)
        joined = " ".join(outcome.errors)
        self.assertIn("no answer", joined)
        self.assertIn("stopped answering", joined)
        self.assertEqual(outcome.reason, "the smoke interactions failed")

    def test_perform_drag_drives_the_mouse_or_reports_the_target(self) -> None:
        page = FakePage([])
        self.assertIsNone(smoke_check.perform_drag(page, "canvas"))
        self.assertEqual(
            page.mouse.actions, ["move 60,50", "down", "move 120,90", "up"]
        )
        self.assertEqual(
            smoke_check.perform_drag(page, "#missing"),
            "drag target not found: #missing",
        )
        page.boxes["#flat"] = None
        self.assertEqual(
            smoke_check.perform_drag(page, "#flat"),
            "drag target has no visible box: #flat",
        )

    def test_collect_page_errors_runs_clicks_then_the_drag(self) -> None:
        page = FakePage([])
        playwright = FakePlaywright(page)
        expectations = smoke_check.PageExpectations(
            selector="canvas",
            fail_on_text=None,
            drag="canvas",
            clicks=("button.rect",),
        )
        errors = smoke_check.collect_page_errors(
            playwright, "http://x/", 1, expectations
        )
        self.assertEqual(errors, [])
        self.assertEqual(page.clicked, ["button.rect"])
        self.assertIn("down", page.mouse.actions)
        bad = smoke_check.PageExpectations(
            fail_on_text=None, clicks=("button.missing",)
        )
        errors = smoke_check.collect_page_errors(playwright, "http://x/", 1, bad)
        self.assertEqual(len(errors), 1)
        self.assertIn("click failed on button.missing", errors[0])
        self.assertIsNone(smoke_check.perform_clicks(page, ()))
        bad_drag = smoke_check.PageExpectations(fail_on_text=None, drag="#gone")
        errors = smoke_check.collect_page_errors(playwright, "http://x/", 1, bad_drag)
        self.assertEqual(errors, ["drag target not found: #gone"])

    def test_drag_without_python_playwright_is_an_explicit_error(self) -> None:
        expectations = smoke_check.PageExpectations(drag="canvas")
        with mock.patch.object(
            smoke_check, "python_playwright_errors", return_value=None
        ):
            errors = smoke_check.browser_errors("http://x/", Path("."), 1, expectations)
        self.assertEqual(errors, [smoke_check.DRAG_NEEDS_PYTHON])

    def test_parse_args_wires_probe_and_drag(self) -> None:
        args = smoke_check.parse_args(
            [
                "--url",
                "http://x/",
                "--drag",
                "canvas",
                "--click",
                "button.rect",
                "--probe",
                "POST /a {}",
                "--probe",
                "GET /b",
            ]
        )
        options = smoke_check.options_from_args(args)
        self.assertEqual(options.probes, ("POST /a {}", "GET /b"))
        self.assertEqual(options.expectations.drag, "canvas")
        self.assertEqual(options.expectations.clicks, ("button.rect",))


class InitScaleThresholdTests(unittest.TestCase):
    def test_threshold_scales_with_package_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(gate.init_scale_threshold(root), 10)
            (root / "package.json").write_text("{}", encoding="utf-8")
            for name in ("a", "b"):
                package = root / "packages" / name
                package.mkdir(parents=True)
                (package / "package.json").write_text("{}", encoding="utf-8")
            nested = root / "node_modules" / "x"
            nested.mkdir(parents=True)
            (nested / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(gate.package_manifest_count(root), 3)
            self.assertEqual(gate.init_scale_threshold(root), 20)
            for index in range(15):
                (root / f"module_{index}.py").write_text(
                    "VALUE = 1\n", encoding="utf-8"
                )
            self.assertIsNone(gate.init_scale_message(root, {}))
            for index in range(15, 25):
                (root / f"module_{index}.py").write_text(
                    "VALUE = 1\n", encoding="utf-8"
                )
            message = gate.init_scale_message(root, {})
            assert message is not None
            self.assertIn("source files exist", message)
