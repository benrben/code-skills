import argparse
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "skills/code-discipline/scripts"
SCRIPT = SCRIPT_DIRECTORY / "project_setup.py"


def load_module():
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
    spec = importlib.util.spec_from_file_location("project_setup_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load project setup module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load_module()


def arguments(**overrides):
    values = {
        "init": False,
        "setup": None,
        "answer": [],
        "non_interactive": False,
        "config": None,
        "thresholds": None,
        "artifact_dir": None,
        "html": None,
        "gate_script": None,
        "no_install": False,
        "fast": False,
        "mutation_workers": "auto",
        "print_prompt": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def run_files(root, **overrides):
    values = {
        "config": root / ".quality/quality-gate.json",
        "thresholds": root / ".quality/quality-thresholds.json",
        "artifact_dir": root / ".quality",
        "html": root / ".quality/report.html",
        "state": root / ".quality/state.json",
        "gate_script": root / "gate.py",
        "profile": root / ".quality/project-profile.json",
        "explicit_config": False,
        "explicit_thresholds": False,
        "explicit_artifacts": False,
        "explicit_html": False,
        "explicit_gate_script": False,
    }
    values.update(overrides)
    return setup.RunFiles(**values)


def question(category="risk"):
    return {
        "key": "security_level",
        "category": category,
        "prompt": "Which security level applies?",
        "reason": "It selects the required controls.",
        "options": ["Internal", "Personal", "Decide later"],
        "recommended": "Internal",
    }


class SetupArgumentTests(unittest.TestCase):
    def test_arguments_support_progressive_and_noninteractive_modes(self):
        parser = argparse.ArgumentParser()

        setup.add_setup_arguments(parser)

        defaults = parser.parse_args([])
        self.assertFalse(defaults.init)
        self.assertIsNone(defaults.setup)
        self.assertFalse(defaults.show_profile)
        self.assertEqual(defaults.answer, [])
        self.assertFalse(defaults.non_interactive)

        selected = parser.parse_args(
            [
                "--init",
                "--setup",
                "runtime",
                "--show-profile",
                "--answer",
                "security_level=Internal",
                "--answer",
                'stories=["Publish"]',
                "--non-interactive",
            ]
        )
        self.assertTrue(selected.init)
        self.assertEqual(selected.setup, "runtime")
        self.assertTrue(selected.show_profile)
        self.assertEqual(
            selected.answer,
            ["security_level=Internal", 'stories=["Publish"]'],
        )
        self.assertTrue(selected.non_interactive)
        self.assertEqual(parser.parse_args(["--setup"]).setup, "all")
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--setup", "language"])

    def test_answer_parser_accepts_json_plain_text_and_last_value(self):
        self.assertEqual(setup.parse_setup_answers([]), {})
        self.assertEqual(
            setup.parse_setup_answers(
                [
                    'critical_user_stories=["Publish"]',
                    'performance={"mode":"measure_only"}',
                    "enabled=true",
                    "security_level=Internal",
                    " security_level =Personal",
                    "empty=",
                ]
            ),
            {
                "critical_user_stories": ["Publish"],
                "performance": {"mode": "measure_only"},
                "enabled": True,
                "security_level": "Personal",
                "empty": "",
            },
        )
        with self.assertRaisesRegex(ValueError, "KEY=VALUE"):
            setup.parse_setup_answers(["invalid"])
        with self.assertRaisesRegex(ValueError, "key cannot be empty"):
            setup.parse_setup_answers(["  =value"])


class ProfileQuestionTests(unittest.TestCase):
    def test_saved_and_detected_profiles_preserve_confirmed_answers(self):
        existing = SimpleNamespace(as_dict=mock.Mock(return_value={"facts": {}}))
        detected = object()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            with mock.patch.object(setup, "load_profile") as load:
                self.assertIsNone(setup.saved_profile(path))
            load.assert_not_called()

            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(setup, "load_profile", return_value=existing):
                self.assertIs(setup.saved_profile(path), existing)

        with mock.patch.object(setup, "saved_profile", return_value=existing):
            with mock.patch.object(
                setup, "detect_profile", return_value=detected
            ) as detect:
                self.assertIs(setup.detected_profile(Path("root"), path), detected)
        existing.as_dict.assert_called_once_with()
        detect.assert_called_once_with(Path("root"), {"facts": {}})

        with mock.patch.object(setup, "saved_profile", return_value=None):
            with mock.patch.object(
                setup, "detect_profile", return_value=detected
            ) as detect:
                setup.detected_profile(Path("root"), path)
        detect.assert_called_once_with(Path("root"), None)

    def test_questions_filter_and_print_clear_next_actions(self):
        profile = object()
        questions = [question("risk"), {**question("product"), "key": "story"}]
        with mock.patch.object(setup, "profile_questions", return_value=questions):
            self.assertEqual(setup.pending_questions(profile, "all"), questions)
            self.assertEqual(setup.pending_questions(profile, "risk"), [questions[0]])
            self.assertEqual(
                setup.pending_questions(profile, "product"), [questions[1]]
            )
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                setup.print_pending_questions(profile, "runtime")
            self.assertEqual(
                output.getvalue(),
                "Project setup has no pending questions in this group.\n",
            )
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                setup.print_pending_questions(profile, "risk")
        self.assertIn("Decisions needed", output.getvalue())
        self.assertIn(
            "security_level: Which security level applies?", output.getvalue()
        )
        self.assertIn("It selects the required controls.", output.getvalue())

    def test_prompt_policy_requires_terminal_outside_ci(self):
        args = arguments()
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(sys.stdin, "isatty", return_value=True):
                with mock.patch.object(sys.stdout, "isatty", return_value=True):
                    self.assertTrue(setup.can_prompt(args))
                    self.assertFalse(setup.can_prompt(arguments(non_interactive=True)))

            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                self.assertFalse(setup.can_prompt(args))

            with mock.patch.object(sys.stdin, "isatty", return_value=True):
                with mock.patch.object(sys.stdout, "isatty", return_value=False):
                    self.assertFalse(setup.can_prompt(args))

        with mock.patch.dict(os.environ, {"CI": "1"}, clear=True):
            self.assertFalse(setup.can_prompt(args))

    def test_prompt_answers_labels_recommendation_and_selects_values(self):
        profile = object()
        questions = [
            question(),
            {
                **question(),
                "key": "security_notes",
                "prompt": "Any notes?",
                "recommended": "Personal",
            },
        ]
        with mock.patch.object(setup, "pending_questions", return_value=questions):
            with mock.patch("builtins.input", side_effect=["2", "custom"]):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    answers = setup.prompt_answers(profile, "risk")
        self.assertEqual(
            answers,
            {"security_level": "Personal", "security_notes": "custom"},
        )
        self.assertIn("Internal (recommended)", output.getvalue())
        self.assertIn("Personal (recommended)", output.getvalue())
        self.assertIn("Why: It selects the required controls.", output.getvalue())

        with mock.patch.object(setup, "pending_questions", return_value=[]):
            self.assertEqual(setup.prompt_answers(profile, "all"), {})

    def test_selected_answer_handles_boundaries_custom_and_deferred(self):
        options = ["A", "B"]
        self.assertEqual(setup.selected_answer("1", options), "A")
        self.assertEqual(setup.selected_answer("2", options), "B")
        self.assertEqual(setup.selected_answer("0", options), "0")
        self.assertEqual(setup.selected_answer("3", options), "3")
        self.assertEqual(setup.selected_answer("custom", options), "custom")
        self.assertEqual(setup.selected_answer("", options), "Decide later")


class InitializationTests(unittest.TestCase):
    def test_initialization_is_idempotent_and_always_ensures_git(self):
        class FakeGate:
            DEPENDENCIES_NAME = ".quality/quality-dependencies.json"

            def __init__(self):
                self.events = []

            def write_initial_thresholds(self, path):
                self.events.append(("thresholds", path))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            def default_thresholds(self):
                self.events.append(("defaults",))
                return {"metric": 1}

            def write_initial_config(self, root, path, thresholds):
                self.events.append(("config", root, path, thresholds))
                path.write_text("{}", encoding="utf-8")

            def write_initial_dependencies(self, root, path):
                self.events.append(("dependencies", root, path))
                if path.exists():
                    return False
                path.write_text("{}", encoding="utf-8")
                return True

            def ensure_git_repository(self, root):
                self.events.append(("git", root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = run_files(root)
            gate = FakeGate()

            first = setup.initialize_gate_files(
                gate, root, files.config, files.thresholds
            )
            second = setup.initialize_gate_files(
                gate, root, files.config, files.thresholds
            )

        self.assertEqual(
            first,
            [
                str(files.thresholds),
                str(files.config),
                str(root / FakeGate.DEPENDENCIES_NAME),
            ],
        )
        self.assertEqual(second, [])
        self.assertEqual(
            [event[0] for event in gate.events],
            [
                "thresholds",
                "defaults",
                "config",
                "dependencies",
                "git",
                "dependencies",
                "git",
            ],
        )

    def test_show_profile_prints_json_without_writing(self):
        profile = SimpleNamespace(to_json=mock.Mock(return_value='{"ok": true}\n'))
        with mock.patch.object(setup, "detected_profile", return_value=profile):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                result = setup.show_profile(Path("root"), Path("profile"))
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), '{"ok": true}\n')
        profile.to_json.assert_called_once_with()

    def test_setup_request_detection_covers_each_trigger(self):
        self.assertFalse(setup.setup_requested(arguments()))
        self.assertTrue(setup.setup_requested(arguments(init=True)))
        self.assertTrue(setup.setup_requested(arguments(setup="risk")))
        self.assertTrue(
            setup.setup_requested(arguments(answer=["security_level=Internal"]))
        )

    def test_setup_before_handles_noop_answers_and_full_initialization(self):
        root = Path("root")
        files = run_files(root)
        profile = object()
        merged = object()

        with mock.patch.object(setup, "detected_profile") as detect:
            self.assertIsNone(
                setup.setup_before_baseline(arguments(), mock.Mock(), root, files)
            )
        detect.assert_not_called()

        with mock.patch.object(setup, "detected_profile", return_value=profile):
            with mock.patch.object(
                setup, "merge_answers", return_value=merged
            ) as merge:
                with mock.patch.object(
                    setup,
                    "initialize_gate_files",
                    return_value=["config.json", "thresholds.json"],
                ) as initialize:
                    with mock.patch.object(setup, "write_profile") as write:
                        with mock.patch(
                            "sys.stdout", new_callable=io.StringIO
                        ) as output:
                            result = setup.setup_before_baseline(
                                arguments(
                                    init=True,
                                    answer=["security_level=Internal"],
                                ),
                                mock.Mock(),
                                root,
                                files,
                            )
        self.assertIs(result, merged)
        merge.assert_called_once_with(profile, {"security_level": "Internal"})
        initialize.assert_called_once()
        write.assert_called_once_with(files.profile, merged)
        self.assertIn("Wrote config.json", output.getvalue())
        self.assertIn(f"Profile: {files.profile}", output.getvalue())

        answer_only = object()
        with mock.patch.object(setup, "detected_profile", return_value=profile):
            with mock.patch.object(
                setup, "merge_answers", return_value=answer_only
            ) as merge:
                with mock.patch.object(setup, "initialize_gate_files") as initialize:
                    with mock.patch.object(setup, "write_profile") as write:
                        result = setup.setup_before_baseline(
                            arguments(answer=["empty=Decide later"]),
                            mock.Mock(),
                            root,
                            files,
                        )
        self.assertIs(result, answer_only)
        merge.assert_called_once_with(profile, {"empty": "Decide later"})
        initialize.assert_not_called()
        write.assert_called_once_with(files.profile, answer_only)

        with mock.patch.object(setup, "detected_profile", return_value=profile):
            with mock.patch.object(setup, "initialize_gate_files") as initialize:
                with mock.patch.object(setup, "write_profile"):
                    setup.setup_before_baseline(
                        arguments(setup="product"), mock.Mock(), root, files
                    )
        initialize.assert_called_once()

    def test_safe_setup_surfaces_expected_errors(self):
        expected = object()
        with mock.patch.object(setup, "setup_before_baseline", return_value=expected):
            self.assertEqual(
                setup.safe_setup_before_baseline(
                    arguments(), mock.Mock(), Path("root"), run_files(Path("root"))
                ),
                (expected, True),
            )

        for error in (OSError("disk"), ValueError("answer"), TypeError("profile")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    setup, "setup_before_baseline", side_effect=error
                ):
                    with mock.patch("sys.stderr", new_callable=io.StringIO) as output:
                        result = setup.safe_setup_before_baseline(
                            arguments(),
                            mock.Mock(),
                            Path("root"),
                            run_files(Path("root")),
                        )
                self.assertEqual(result, (None, False))
                self.assertIn(str(error), output.getvalue())


class RunWiringTests(unittest.TestCase):
    def test_threshold_selection_uses_bundle_only_for_missing_default(self):
        root = Path("root")
        bundled = Path("bundled.json")
        gate = SimpleNamespace(bundled_thresholds_path=mock.Mock(return_value=bundled))
        missing = run_files(root)

        self.assertEqual(setup.active_thresholds_path(gate, missing), bundled)
        gate.bundled_thresholds_path.assert_called_once_with()

        explicit = run_files(root, explicit_thresholds=True)
        self.assertEqual(
            setup.active_thresholds_path(gate, explicit), explicit.thresholds
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "thresholds.json"
            path.write_text("{}", encoding="utf-8")
            existing = run_files(root, thresholds=path)
            self.assertEqual(setup.active_thresholds_path(gate, existing), path)
        gate.bundled_thresholds_path.assert_called_once_with()

    def test_run_file_resolution_tracks_paths_and_explicit_options(self):
        root = Path("/project")
        defaults = arguments()
        default_gate = Path("/skill/gate.py")
        resolved_artifacts = (
            root / ".quality",
            root / ".quality/report.html",
            root / ".quality/state.json",
        )

        def resolve_path(value, current_root, default):
            self.assertEqual(current_root, root)
            return current_root / value if value else default

        resolve_artifacts = mock.Mock(return_value=resolved_artifacts)
        files = setup.resolve_run_files(
            defaults,
            root,
            ".quality",
            ".quality/project-profile.json",
            default_gate,
            resolve_path,
            resolve_artifacts,
        )
        self.assertEqual(files.config, root / ".quality/quality-gate.json")
        self.assertEqual(files.thresholds, root / ".quality/quality-thresholds.json")
        self.assertEqual(files.artifact_dir, resolved_artifacts[0])
        self.assertEqual(files.html, resolved_artifacts[1])
        self.assertEqual(files.state, resolved_artifacts[2])
        self.assertEqual(files.gate_script, default_gate)
        self.assertEqual(files.profile, root / ".quality/project-profile.json")
        self.assertFalse(files.explicit_config)
        self.assertFalse(files.explicit_thresholds)
        self.assertFalse(files.explicit_artifacts)
        self.assertFalse(files.explicit_html)
        self.assertFalse(files.explicit_gate_script)
        resolve_artifacts.assert_called_once_with(defaults, root)

        explicit_args = arguments(
            config="config.json",
            thresholds="limits.json",
            artifact_dir="artifacts",
            html="report.html",
            gate_script="custom-gate.py",
        )
        explicit = setup.resolve_run_files(
            explicit_args,
            root,
            ".quality",
            "profile.json",
            default_gate,
            resolve_path,
            lambda _args, current_root: (
                current_root / "artifacts",
                current_root / "report.html",
                current_root / "state.json",
            ),
        )
        self.assertTrue(explicit.explicit_config)
        self.assertTrue(explicit.explicit_thresholds)
        self.assertTrue(explicit.explicit_artifacts)
        self.assertTrue(explicit.explicit_html)
        self.assertTrue(explicit.explicit_gate_script)

    def test_rerun_command_receives_scope_selection_and_all_paths(self):
        root = Path("root")
        files = run_files(
            root,
            explicit_config=True,
            explicit_thresholds=True,
            explicit_artifacts=True,
            explicit_html=True,
            explicit_gate_script=True,
        )
        args = arguments(no_install=True, fast=True, mutation_workers="3")
        build = mock.Mock(return_value="quality --rerun")
        scope = mock.Mock(return_value=["--commit", "HEAD"])
        selections = mock.Mock(return_value=("tests", "coverage"))

        result = setup.rerun_for(
            args, root, files, Path("active-thresholds.json"), build, scope, selections
        )

        self.assertEqual(result, "quality --rerun")
        build.assert_called_once_with(
            root,
            files.config,
            True,
            Path("active-thresholds.json"),
            True,
            files.artifact_dir,
            True,
            files.html,
            True,
            files.gate_script,
            True,
            ["--commit", "HEAD"],
            True,
            True,
            "3",
            ("tests", "coverage"),
        )
        scope.assert_called_once_with(args)
        selections.assert_called_once_with(args)

    def test_artifact_refresh_loads_active_inputs_and_attaches_profile(self):
        root = Path("root")
        analysis = object()
        thresholds = {"coverage": 100}
        config = {"smoke": {}}
        writer = mock.Mock(return_value={"ok": True})
        gate = SimpleNamespace(
            load_thresholds=mock.Mock(return_value=(thresholds, ["note"])),
            load_config=mock.Mock(return_value=(config, ["config note"])),
            attach_project_quality=mock.Mock(),
        )

        missing = run_files(root)
        setup.refresh_project_profile_artifacts(
            gate,
            analysis,
            root,
            missing,
            Path("active.json"),
            1,
            "run failed",
            writer,
        )
        gate.load_thresholds.assert_called_once_with(Path("active.json"))
        gate.load_config.assert_called_once_with(None, thresholds)
        gate.attach_project_quality.assert_called_once_with(analysis, root, config)
        writer.assert_called_once_with(
            gate, analysis, missing.html, missing.state, 1, "run failed"
        )

        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "quality-gate.json"
            config_path.write_text("{}", encoding="utf-8")
            existing = run_files(root, config=config_path)
            setup.refresh_project_profile_artifacts(
                gate,
                analysis,
                root,
                existing,
                Path("active.json"),
                0,
                None,
                writer,
            )
        self.assertEqual(
            gate.load_config.call_args_list[-1].args, (config_path, thresholds)
        )

    def test_setup_after_noninteractive_empty_and_answered_paths(self):
        root = Path("root")
        files = run_files(root)
        profile = object()
        analysis = object()
        writer = mock.Mock()

        with mock.patch.object(setup, "can_prompt", return_value=False):
            with mock.patch.object(setup, "print_pending_questions") as pending:
                setup.setup_after_baseline(
                    arguments(setup="risk"),
                    mock.Mock(),
                    analysis,
                    root,
                    files,
                    Path("limits.json"),
                    profile,
                    0,
                    None,
                    writer,
                )
        pending.assert_called_once_with(profile, "risk")
        writer.assert_not_called()

        with mock.patch.object(setup, "can_prompt", return_value=True):
            with mock.patch.object(setup, "prompt_answers", return_value={}) as prompt:
                setup.setup_after_baseline(
                    arguments(),
                    mock.Mock(),
                    analysis,
                    root,
                    files,
                    Path("limits.json"),
                    profile,
                    0,
                    None,
                    writer,
                )
        prompt.assert_called_once_with(profile, "all")

        merged = object()
        gate = mock.Mock()
        with mock.patch.object(setup, "can_prompt", return_value=True):
            with mock.patch.object(
                setup,
                "prompt_answers",
                return_value={"security_level": "Internal"},
            ):
                with mock.patch.object(
                    setup, "merge_answers", return_value=merged
                ) as merge:
                    with mock.patch.object(setup, "write_profile") as write:
                        with mock.patch.object(
                            setup, "refresh_project_profile_artifacts"
                        ) as refresh:
                            with mock.patch(
                                "sys.stdout", new_callable=io.StringIO
                            ) as output:
                                setup.setup_after_baseline(
                                    arguments(setup="risk"),
                                    gate,
                                    analysis,
                                    root,
                                    files,
                                    Path("limits.json"),
                                    profile,
                                    2,
                                    "adapter failed",
                                    writer,
                                )
        merge.assert_called_once_with(profile, {"security_level": "Internal"})
        write.assert_called_once_with(files.profile, merged)
        refresh.assert_called_once_with(
            gate,
            analysis,
            root,
            files,
            Path("limits.json"),
            2,
            "adapter failed",
            writer,
        )
        self.assertIn(str(files.html), output.getvalue())

    def test_print_analysis_delegates_complete_report_context(self):
        root = Path("root")
        files = run_files(root)
        args = arguments(print_prompt=True)
        gate = object()
        analysis = object()
        state = {"status": "PASS"}
        previous = object()
        previous_items = object()
        printer = mock.Mock()

        setup.print_analysis(
            args,
            gate,
            analysis,
            state,
            files,
            previous,
            previous_items,
            printer,
        )

        printer.assert_called_once_with(
            gate,
            analysis,
            state,
            files.state,
            files.html,
            True,
            previous,
            previous_items,
        )


if __name__ == "__main__":
    unittest.main()
