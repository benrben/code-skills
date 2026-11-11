"""`--init` writes configuration only: no checks, no questions, no overwrites."""

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import project_setup  # noqa: E402


def fake_config(
    *,
    config: Path | None,
    thresholds: Path | None,
    git_message: str | None = None,
    error: Exception | None = None,
) -> SimpleNamespace:
    def write_config(root: Path) -> Path | None:
        if error is not None:
            raise error
        return config

    return SimpleNamespace(
        CONFIG_NAME=".quality/quality-gate.json",
        THRESHOLDS_NAME=".quality/quality-thresholds.json",
        bundled_thresholds_path=lambda: Path("/skill/quality-thresholds.json"),
        write_initial_config=mock.Mock(side_effect=write_config),
        write_initial_thresholds=mock.Mock(return_value=thresholds),
        ensure_git_repository=mock.Mock(return_value=git_message),
        load_config=mock.Mock(return_value={"source": {}}),
    )


def run_init(
    module: SimpleNamespace, args: argparse.Namespace | None = None
) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(project_setup, "quality_config", module),
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
    ):
        code = project_setup.run_init(Path("/repo"), args or argparse.Namespace())
    return code, output.getvalue(), errors.getvalue()


class SetupArgumentTests(unittest.TestCase):
    def test_only_the_init_flag_is_added(self):
        parser = argparse.ArgumentParser()
        project_setup.add_setup_arguments(parser)
        self.assertFalse(parser.parse_args([]).init)
        self.assertTrue(parser.parse_args(["--init"]).init)
        for removed in ("--setup", "--answer", "--show-profile", "--non-interactive"):
            with self.subTest(flag=removed):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args([removed])


class RunInitTests(unittest.TestCase):
    def test_reports_written_files_and_git_setup_then_runs_nothing(self):
        module = fake_config(
            config=Path("/repo/.quality/quality-gate.json"),
            thresholds=Path("/repo/.quality/quality-thresholds.json"),
            git_message="Initialized a git repository in /repo",
        )
        code, output, errors = run_init(module)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        lines = output.splitlines()
        self.assertEqual(lines[0], "Wrote /repo/.quality/quality-gate.json")
        self.assertEqual(
            lines[1],
            "Wrote /repo/.quality/quality-thresholds.json "
            "(defaults from /skill/quality-thresholds.json)",
        )
        self.assertEqual(lines[2], "Initialized a git repository in /repo")
        self.assertIn("No checks ran", lines[3])
        module.write_initial_config.assert_called_once_with(Path("/repo"))
        module.write_initial_thresholds.assert_called_once_with(Path("/repo"))
        module.ensure_git_repository.assert_called_once_with(Path("/repo"))

    def test_existing_files_are_kept_and_reported(self):
        code, output, _ = run_init(fake_config(config=None, thresholds=None))
        self.assertEqual(code, 0)
        self.assertIn("Kept existing /repo/.quality/quality-gate.json", output)
        self.assertIn("Kept existing /repo/.quality/quality-thresholds.json", output)
        self.assertNotIn("Wrote", output)
        self.assertNotIn("git", output)

    def test_write_errors_exit_2_without_claiming_success(self):
        for error in (OSError("read-only file system"), ValueError("bad defaults")):
            with self.subTest(error=error):
                module = fake_config(config=None, thresholds=None, error=error)
                code, output, errors = run_init(module)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(errors, f"error: {error}\n")
                module.ensure_git_repository.assert_not_called()

    def test_custom_paths_are_noted_because_init_writes_the_defaults(self):
        args = argparse.Namespace(config="custom.json", thresholds=None)
        _, output, _ = run_init(fake_config(config=None, thresholds=None), args)
        self.assertIn("--config custom.json applies only to quality runs", output)
        self.assertNotIn("--thresholds", output)
        args = argparse.Namespace(config=None, thresholds="limits.json")
        _, output, _ = run_init(fake_config(config=None, thresholds=None), args)
        self.assertIn("--thresholds limits.json applies only", output)


def seeded_root(temporary: str, files: int) -> Path:
    root = Path(temporary)
    for index in range(files):
        (root / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
        + list(arguments),
        check=True,
        capture_output=True,
    )


class InitScaleTests(unittest.TestCase):
    def test_small_repositories_get_no_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = seeded_root(temporary, 3)
            self.assertIsNone(project_setup.init_scale_message(root, {}))

    def test_new_project_with_many_files_gets_the_rule_1_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = seeded_root(temporary, 11)
            message = project_setup.init_scale_message(root, {})
            self.assertIsNotNone(message)
            self.assertIn("WARNING: 11 source files", str(message))
            self.assertIn("Rule 1", str(message))
            self.assertIn("rule 9", str(message))

    def test_repository_with_history_gets_the_adoption_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = seeded_root(temporary, 12)
            git(root, "init", "-q")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "one")
            (root / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "two")
            self.assertEqual(project_setup.commit_count(root), 2)
            message = project_setup.init_scale_message(root, {})
            self.assertIn("Existing repository: 13 source files", str(message))
            self.assertIn("quality_items.py", str(message))

    def test_commit_count_is_zero_without_git_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(project_setup.commit_count(Path(temporary)), 0)

    def test_threshold_scales_with_package_manifests_outside_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(project_setup.init_scale_threshold(root), 10)
            (root / "package.json").write_text("{}", encoding="utf-8")
            for name in ("a", "b"):
                package = root / "packages" / name
                package.mkdir(parents=True)
                (package / "package.json").write_text("{}", encoding="utf-8")
            nested = root / "node_modules" / "x"
            nested.mkdir(parents=True)
            (nested / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(project_setup.init_scale_threshold(root), 20)

    def test_configured_source_settings_limit_the_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = seeded_root(temporary, 11)
            config = root / ".quality" / "quality-gate.json"
            module = fake_config(config=None, thresholds=None)
            module.load_config.return_value = {"source": {"include": ["module_1.py"]}}
            with mock.patch.object(project_setup, "quality_config", module):
                self.assertEqual(
                    project_setup.configured_source(config),
                    {"include": ["module_1.py"]},
                )
                module.load_config.assert_called_once_with(None)
                config.parent.mkdir()
                config.write_text("{}", encoding="utf-8")
                module.load_config.return_value = {"source": ["not", "a", "map"]}
                self.assertEqual(project_setup.configured_source(config), {})
                module.load_config.assert_called_with(config)

    def test_init_prints_the_scale_warning_after_the_written_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = seeded_root(temporary, 11)
            module = fake_config(config=None, thresholds=None)
            output = io.StringIO()
            with (
                mock.patch.object(project_setup, "quality_config", module),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(project_setup.run_init(root, argparse.Namespace()), 0)
            lines = output.getvalue().splitlines()
            self.assertTrue(lines[2].startswith("WARNING: 11 source files"))


class InitIntegrationTests(unittest.TestCase):
    def test_real_configuration_module_writes_once_and_initializes_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(project_setup.run_init(root, argparse.Namespace()), 0)
            quality = root / ".quality"
            thresholds = json.loads(
                (quality / "quality-thresholds.json").read_text(encoding="utf-8")
            )
            self.assertEqual(thresholds["schema_version"], 3)
            self.assertTrue((quality / "quality-gate.json").is_file())
            self.assertTrue((root / ".git").exists())
            self.assertIn("Wrote", output.getvalue())
            (quality / "quality-gate.json").write_text("{}\n", encoding="utf-8")
            again = io.StringIO()
            with contextlib.redirect_stdout(again):
                self.assertEqual(project_setup.run_init(root, argparse.Namespace()), 0)
            self.assertNotIn("Wrote", again.getvalue())
            self.assertEqual(
                (quality / "quality-gate.json").read_text(encoding="utf-8"), "{}\n"
            )

    def test_loop_init_writes_configuration_without_running_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "example.py").write_text(
                "raise RuntimeError('do not run before choices')\n", encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "quality_loop.py"),
                    "--root",
                    str(root),
                    "--init",
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            quality = root / ".quality"
            self.assertTrue((quality / "quality-gate.json").is_file())
            self.assertTrue((quality / "quality-thresholds.json").is_file())
            self.assertFalse((quality / "quality-gate-state.json").exists())
            self.assertFalse((quality / "quality-gate-report.html").exists())
            self.assertNotIn("QUALITY_LOOP=", result.stdout)


if __name__ == "__main__":
    unittest.main()
