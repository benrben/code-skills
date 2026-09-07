import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skills/code-discipline/scripts"
LOOP_PATH = SCRIPT_DIR / "quality_loop.py"
LAUNCHER = SCRIPT_DIR / "quality"


def load_loop():
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("setup_quality_loop_test", LOOP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load quality loop")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


quality_loop = load_loop()
project_setup = quality_loop.sys.modules["project_setup"]


class SetupUxTests(unittest.TestCase):
    def test_run_locked_handles_profile_display_and_setup_failure(self):
        files = SimpleNamespace(
            profile=Path("profile.json"), gate_script=Path("gate.py")
        )
        args = SimpleNamespace(show_profile=True)
        with (
            mock.patch.object(quality_loop, "resolve_run_files", return_value=files),
            mock.patch.object(quality_loop, "show_profile", return_value=0) as show,
        ):
            self.assertEqual(quality_loop.run_locked(args, Path("root")), 0)
        show.assert_called_once_with(Path("root"), files.profile)

        args.show_profile = False
        with (
            mock.patch.object(quality_loop, "resolve_run_files", return_value=files),
            mock.patch.object(quality_loop, "load_gate_safely", return_value=object()),
            mock.patch.object(
                quality_loop,
                "safe_setup_before_baseline",
                return_value=(None, False),
            ),
        ):
            self.assertEqual(quality_loop.run_locked(args, Path("root")), 2)

    def test_run_locked_preserves_failed_baseline_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact_dir = Path(temporary) / ".quality"
            files = SimpleNamespace(
                profile=artifact_dir / "project-profile.json",
                config=artifact_dir / "quality-gate.json",
                html=artifact_dir / "report.html",
                state=artifact_dir / "state.json",
                artifact_dir=artifact_dir,
                gate_script=Path("gate.py"),
            )
            args = SimpleNamespace(show_profile=False)
            gate = object()
            profile = object()
            analysis = object()
            with (
                mock.patch.object(
                    quality_loop, "resolve_run_files", return_value=files
                ),
                mock.patch.object(quality_loop, "load_gate_safely", return_value=gate),
                mock.patch.object(
                    quality_loop,
                    "safe_setup_before_baseline",
                    return_value=(profile, True),
                ),
                mock.patch.object(
                    quality_loop,
                    "active_thresholds_path",
                    return_value=Path("thresholds.json"),
                ),
                mock.patch.object(quality_loop, "rerun_for", return_value="quality"),
                mock.patch.object(
                    quality_loop,
                    "execute_analysis",
                    return_value=(analysis, 1, "baseline failed"),
                ),
                mock.patch.object(
                    quality_loop, "previous_measurement", return_value=None
                ),
                mock.patch.object(
                    quality_loop, "previous_item_keys", return_value=set()
                ),
                mock.patch.object(
                    quality_loop, "write_analysis_artifacts", return_value={}
                ),
                mock.patch.object(quality_loop, "print_analysis"),
                mock.patch.object(quality_loop, "setup_after_baseline") as setup_after,
            ):
                self.assertEqual(quality_loop.run_locked(args, Path("root")), 1)
            setup_after.assert_called_once()

    def test_setup_arguments_and_answer_parsing(self):
        args = quality_loop.parse_args(
            [
                "--init",
                "--setup",
                "risk",
                "--answer",
                'critical_user_stories=["Publish"]',
                "--non-interactive",
            ]
        )
        self.assertTrue(args.init)
        self.assertEqual(args.setup, "risk")
        self.assertTrue(args.non_interactive)
        self.assertEqual(
            project_setup.parse_setup_answers(args.answer),
            {"critical_user_stories": ["Publish"]},
        )
        self.assertEqual(
            project_setup.parse_setup_answers(["security_level=Internal"]),
            {"security_level": "Internal"},
        )
        with self.assertRaisesRegex(ValueError, "KEY=VALUE"):
            project_setup.parse_setup_answers(["broken"])
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            project_setup.parse_setup_answers(["=value"])

    def test_question_filter_selection_and_noninteractive_policy(self):
        question = SimpleNamespace(
            as_dict=lambda: {
                "key": "security_level",
                "category": "risk",
                "prompt": "Risk?",
                "reason": "Choose controls.",
                "options": ["Internal", "Decide later"],
                "recommended": "Internal",
                "required": True,
            }
        )
        profile = SimpleNamespace(questions=(question,))
        with mock.patch.object(
            project_setup, "profile_questions", return_value=[question.as_dict()]
        ):
            self.assertEqual(len(project_setup.pending_questions(profile, "all")), 1)
            self.assertEqual(len(project_setup.pending_questions(profile, "risk")), 1)
            self.assertEqual(project_setup.pending_questions(profile, "product"), [])
            with mock.patch("builtins.input", return_value="1"):
                answers = project_setup.prompt_answers(profile, "risk")
            self.assertEqual(answers, {"security_level": "Internal"})
        self.assertEqual(project_setup.selected_answer("2", ["A", "B"]), "B")
        self.assertEqual(project_setup.selected_answer("custom", ["A"]), "custom")
        self.assertEqual(project_setup.selected_answer("", ["A"]), "Decide later")
        args = SimpleNamespace(non_interactive=True)
        self.assertFalse(project_setup.can_prompt(args))

    def test_show_profile_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
            result = subprocess.run(
                [str(LAUNCHER), "--root", str(root), "--show-profile"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"languages"', result.stdout)
            self.assertFalse((root / ".quality").exists())

    def test_public_init_writes_profile_and_reports_before_questions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.py").write_text(
                "def value():\n    return 1\n", encoding="utf-8"
            )
            environment = {**os.environ, "CI": "1"}
            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "--root",
                    str(root),
                    "--init",
                    "--fast",
                    "--no-install",
                    "--non-interactive",
                ],
                capture_output=True,
                text=True,
                env=environment,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("QUALITY_LOOP=PASS", result.stdout)
            for name in (
                "quality-gate.json",
                "quality-thresholds.json",
                "project-profile.json",
                "quality-gate-report.html",
                "quality-gate-state.json",
            ):
                self.assertTrue((root / ".quality" / name).exists(), name)
            profile = json.loads(
                (root / ".quality/project-profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["facts"]["languages"]["value"], ["Python"])
            self.assertLess(
                result.stdout.index("HTML="), result.stdout.index("Decisions needed")
            )


if __name__ == "__main__":
    unittest.main()
