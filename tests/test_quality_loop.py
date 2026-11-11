from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
for path in (SCRIPTS, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import quality_config  # noqa: E402
import quality_loop  # noqa: E402
from quality_repo_fixture import (  # noqa: E402
    add_unused_function,
    git,
    make_python_repository,
    write_test_config,
)

ENGINE = """
from quality_model import Analysis, GateRun

def run_analysis(root, config, thresholds, **options):
    gate = GateRun.not_run("secrets", "pass", "engine override ran")
    return Analysis(str(root), "now", "whole repository", "full", [gate])
"""


class LoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = make_python_repository(self.base / "repo")
        write_test_config(self.root)
        environment = mock.patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        for number in (signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, number, signal.getsignal(number))

    def loop(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = quality_loop.main(
                ["--root", str(self.root), "--no-install", *arguments]
            )
        return code, out.getvalue(), err.getvalue()

    def state(self) -> dict[str, Any]:
        path = self.root / ".quality" / "quality-gate-state.json"
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return value


class MainTests(LoopTestCase):
    def test_passing_run_writes_state_and_html(self) -> None:
        code, out, _ = self.loop()
        self.assertEqual(code, 0, out)
        self.assertIn("QUALITY_LOOP=PASS", out)
        state = self.state()
        self.assertEqual(state["status"], "pass")
        self.assertEqual(state["scope"], "whole repository")
        self.assertEqual(state["baselines"]["tests_executed"], 2)
        self.assertTrue(state["state_path"].endswith("quality-gate-state.json"))
        self.assertIn("--no-install", state["rerun_command"])
        html = (self.root / ".quality" / "quality-gate-report.html").read_text()
        self.assertIn("<!doctype html>", html)

    def test_failing_local_changes_exit_1(self) -> None:
        add_unused_function(self.root)
        code, out, _ = self.loop("--local-changes")
        self.assertEqual(code, 1)
        self.assertIn("QUALITY_LOOP=FAIL", out)
        state = self.state()
        self.assertEqual(state["scope"], "local changes")
        self.assertTrue(any(issue["gate"] == "dead_code" for issue in state["issues"]))
        self.assertNotIn("--local-changes", state["full_rerun_command"])

    def test_fast_selected_and_commit_runs(self) -> None:
        code, out, _ = self.loop("--fast")
        self.assertEqual((code, self.state()["status"]), (1, "ready_for_full"))
        code, _, _ = self.loop("--secrets", "--loc")
        self.assertEqual((code, self.state()["status"]), (0, "selected_pass"))
        code, _, _ = self.loop("--commit")
        state = self.state()
        # The first commit adds add(), which only tests call: dead code fails.
        self.assertEqual(
            (code, state["scope"], state["reference"]), (1, "commit HEAD", "HEAD")
        )

    def test_policy_change_needs_approval(self) -> None:
        policy = self.root / ".quality-policy"
        thresholds = self.root / "policy" / "quality-thresholds.json"
        thresholds.parent.mkdir()
        (self.root / ".gitignore").write_text(
            ".quality/quality-gate-*\n.quality-policy\n"
        )
        self.assertEqual(self.loop("--init")[0], 0)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "policy")
        path = self.root / ".quality" / "quality-thresholds.json"
        path.write_text(
            path.read_text().replace('"max_lines": 600', '"max_lines": 700')
        )
        code, out, _ = self.loop("--local-changes", "--shortcuts")
        self.assertEqual(code, 1, out)
        code, _, _ = self.loop(
            "--local-changes", "--shortcuts", "--allow-policy-change"
        )
        self.assertEqual(code, 0)
        self.assertIn("--allow-policy-change", self.state()["rerun_command"])
        self.assertFalse(policy.exists())

    def test_test_removal_needs_approval(self) -> None:
        tests = self.root / "tests" / "test_calc.py"
        original = tests.read_text()
        extra = "\n    def test_zero(self) -> None:\n        self.assertEqual(add(0, 0), 0)\n"
        tests.write_text(original + extra)
        self.assertEqual(self.loop()[0], 0)
        tests.write_text(original)
        self.assertEqual(self.loop("--tests")[0], 1)
        self.assertEqual(self.loop()[0], 1)
        self.assertEqual(self.loop("--tests", "--allow-test-removal")[0], 0)
        self.assertEqual(self.loop()[0], 1)
        code, out, _ = self.loop("--allow-test-removal")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["baselines"]["tests_executed"], 2)
        self.assertEqual(self.loop()[0], 0)

    def test_runner_errors_exit_2_with_an_error_state(self) -> None:
        code, out, _ = self.loop("--commit", "no-such-ref")
        self.assertEqual(code, 2)
        self.assertIn("QUALITY_LOOP=ERROR", out)
        state = self.state()
        self.assertEqual(state["status"], "error")
        self.assertIn("no-such-ref", state["error"])
        self.assertEqual(state["scope"], "commit no-such-ref")
        code, _, _ = self.loop("--config", "missing.json")
        self.assertEqual(code, 2)
        self.assertIn("configuration not found", self.state()["error"])

    def test_gate_script_override(self) -> None:
        engine = self.base / "engine.py"
        engine.write_text(ENGINE, encoding="utf-8")
        code, out, _ = self.loop("--gate-script", str(engine))
        self.assertEqual(code, 0, out)
        self.assertIn("engine override ran", out)
        self.assertIn("--gate-script", self.state()["rerun_command"])
        empty = self.base / "empty.py"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(self.loop("--gate-script", str(empty))[0], 2)
        self.assertIn("no run_analysis", self.state()["error"])
        self.assertEqual(self.loop("--gate-script", str(self.base / "x.txt"))[0], 2)

    def test_missing_root_and_init(self) -> None:
        self.assertEqual(quality_loop.main(["--root", str(self.base / "nope")]), 2)
        with mock.patch.object(
            quality_loop.project_setup, "run_init", return_value=7
        ) as init:
            self.assertEqual(self.loop("--init")[0], 7)
        self.assertEqual(init.call_args.args[0], self.root)

    def test_concurrent_run_and_interrupt(self) -> None:
        with mock.patch.object(
            quality_loop,
            "run_locked",
            side_effect=quality_loop.ConcurrentRunError("busy"),
        ):
            code, _, err = self.loop()
        self.assertEqual((code, err.strip()), (2, "error: busy"))
        with mock.patch.object(
            quality_loop, "run_locked", side_effect=KeyboardInterrupt
        ):
            code, _, err = self.loop()
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)
        with self.assertRaises(KeyboardInterrupt):
            quality_loop.raise_keyboard_interrupt(signal.SIGTERM, None)


class ArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def files(self, *arguments: str) -> quality_loop.RunFiles:
        return quality_loop.resolve_run_files(
            quality_loop.parse_args(list(arguments)), self.root
        )

    def test_run_files(self) -> None:
        files = self.files()
        self.assertEqual(files.config, self.root / ".quality" / "quality-gate.json")
        self.assertIsNone(files.thresholds)
        self.assertEqual(
            files.state, self.root / ".quality" / "quality-gate-state.json"
        )
        html = self.files("--html", "out/report.html")
        self.assertEqual(
            (html.html, html.artifact_dir),
            (self.root / "out/report.html", self.root / "out"),
        )
        self.assertEqual(
            self.files("--artifact-dir", "/tmp/a").html,
            Path("/tmp/a/quality-gate-report.html").resolve(),
        )
        self.assertEqual(
            self.files("--thresholds", "t.json").thresholds, self.root / "t.json"
        )
        (self.root / ".quality").mkdir()
        (self.root / ".quality" / "quality-thresholds.json").write_text("{}")
        self.assertEqual(
            self.files().thresholds, self.root / ".quality" / "quality-thresholds.json"
        )

    def rerun(self, *arguments: str, full: bool = False) -> str:
        args = quality_loop.parse_args(list(arguments))
        files = quality_loop.resolve_run_files(args, self.root)
        return quality_loop.rerun_command(args, self.root, files, full=full)

    def test_rerun_commands(self) -> None:
        command = self.rerun(
            "--config",
            "c.json",
            "--thresholds",
            "t.json",
            "--html",
            "r.html",
            "--commit",
            "abc",
            "--fast",
            "--crap",
            "--lint",
            "--no-install",
        )
        self.assertIn("--config c.json --thresholds t.json --html r.html", command)
        self.assertIn("--commit abc --no-install --fast --lint --crap", command)
        self.assertTrue(command.startswith(sys.executable))
        full = self.rerun(
            "--local-changes", "--fast", "--tests", "--allow-test-removal", full=True
        )
        self.assertTrue(full.endswith("--root . --allow-test-removal"))
        self.assertIn("--artifact-dir /x", self.rerun("--artifact-dir", "/x"))
        self.assertIn("--local-changes", self.rerun("--local-changes"))

    def test_selection_and_modes(self) -> None:
        args = quality_loop.parse_args(["--dead-code", "--test-count"])
        self.assertEqual(
            quality_loop.selection_names(args), ("test-count", "dead-code")
        )
        self.assertEqual(quality_loop.run_mode(args), "partial")
        self.assertEqual(
            quality_loop.run_mode(quality_loop.parse_args(["--fast"])), "fast"
        )
        self.assertEqual(quality_loop.run_mode(quality_loop.parse_args([])), "full")
        self.assertEqual(
            quality_loop.scope_kind(quality_loop.parse_args(["--local-changes"])),
            "local_changes",
        )
        self.assertEqual(
            quality_loop.display_path(Path("/elsewhere"), self.root), "/elsewhere"
        )

    def test_tool_cache_dir_and_previous_state(self) -> None:
        self.assertIsNone(quality_loop.tool_cache_dir({"tools": {}}, self.root))
        self.assertEqual(
            quality_loop.tool_cache_dir({"tools": {"cache_dir": "c"}}, self.root),
            self.root / "c",
        )
        legacy = self.root / "state.json"
        legacy.write_text('{"schema_version": 1}')
        self.assertIsNone(quality_loop.previous_state_for(legacy))
        legacy.write_text('{"schema_version": 2}')
        self.assertEqual(quality_loop.previous_state_for(legacy), {"schema_version": 2})

    def test_removed_flags_are_rejected(self) -> None:
        for flag in (
            "--mutation",
            "--max-mutants",
            "--print-prompt",
            "--setup",
            "--gherkin",
        ):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    quality_loop.parse_args([flag])

    def test_version(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            quality_loop.parse_args(["--version"])
        self.assertEqual(out.getvalue().strip(), quality_config.VERSION)


if __name__ == "__main__":
    unittest.main()
