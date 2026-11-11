import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quality_commands  # noqa: E402

PYTHON = sys.executable


class EnvironmentTests(unittest.TestCase):
    def test_project_environment_keeps_package_managers_offline(self) -> None:
        env = quality_commands.project_execution_env()
        self.assertEqual(env["UV_OFFLINE"], "1")
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertEqual(env["NPM_CONFIG_OFFLINE"], "true")
        self.assertEqual(env["GOPROXY"], "off")
        self.assertEqual(env["CARGO_NET_OFFLINE"], "true")

    def test_extra_values_win(self) -> None:
        env = quality_commands.project_execution_env({"GOPROXY": "x", "A": "b"})
        self.assertEqual(env["GOPROXY"], "x")
        self.assertEqual(env["A"], "b")

    def test_command_list(self) -> None:
        self.assertIsNone(quality_commands.command_list(None))
        self.assertIsNone(quality_commands.command_list(""))
        self.assertIsNone(quality_commands.command_list([]))
        self.assertEqual(
            quality_commands.command_list("pytest -k 'a b'"), ["pytest", "-k", "a b"]
        )
        self.assertEqual(quality_commands.command_list(["go", 1]), ["go", "1"])
        with self.assertRaises(ValueError):
            quality_commands.command_list({"command": "x"})
        with self.assertRaises(ValueError):
            quality_commands.command_list([["nested"]])


class RunCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_success_output_and_environment(self) -> None:
        code = "import os, sys; print(os.getcwd()); print(os.environ['Q']); sys.exit(3)"
        run = quality_commands.run_command(
            [PYTHON, "-c", code], self.root, 30, {"Q": "value"}
        )
        self.assertEqual(run.returncode, 3)
        self.assertIn("value", run.output)
        self.assertIn(self.root.resolve().name, run.output)
        self.assertFalse(run.timed_out)
        self.assertEqual(run.command[0], PYTHON)

    def test_missing_program_is_127(self) -> None:
        run = quality_commands.run_command(["/no/such/program"], self.root, 5)
        self.assertEqual(run.returncode, 127)

    def test_timeout_stops_the_command(self) -> None:
        code = "import time; print('started', flush=True); time.sleep(30)"
        run = quality_commands.run_command([PYTHON, "-c", code], self.root, 1)
        self.assertEqual(run.returncode, 124)
        self.assertTrue(run.timed_out)
        self.assertIn("started", run.output)
        self.assertLess(run.duration_seconds, 20)

    def test_a_command_that_ignores_sigterm_is_killed(self) -> None:
        code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)"
        )
        with mock.patch.object(quality_commands, "STOP_GRACE_SECONDS", 0.5):
            run = quality_commands.run_command([PYTHON, "-c", code], self.root, 1)
        self.assertTrue(run.timed_out)
        self.assertIn("ready", run.output)

    def test_interrupt_stops_the_tree_and_propagates(self) -> None:
        original = subprocess.Popen.communicate
        calls = {"count": 0}

        def communicate(
            process: subprocess.Popen[str], timeout: float | None = None
        ) -> tuple[str, str]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise KeyboardInterrupt
            return original(process, timeout=timeout)

        code = "import time; time.sleep(30)"
        with mock.patch.object(subprocess.Popen, "communicate", communicate):
            with self.assertRaises(KeyboardInterrupt):
                quality_commands.run_command([PYTHON, "-c", code], self.root, 30)
        self.assertEqual(calls["count"], 2)

    def test_runner_applies_the_offline_environment(self) -> None:
        runner = quality_commands.runner_for(self.root)
        code = "import os; print(os.environ['UV_OFFLINE'], os.environ['EXTRA'])"
        run = runner([PYTHON, "-c", code], 30, {"EXTRA": "yes"})
        self.assertEqual(run.output.split(), ["1", "yes"])
        self.assertEqual(runner([PYTHON, "-c", "pass"], 30).returncode, 0)


class PlatformTests(unittest.TestCase):
    def test_process_group_options(self) -> None:
        self.assertEqual(
            quality_commands._process_group_options(), {"start_new_session": True}
        )
        with (
            mock.patch.object(quality_commands.os, "name", "nt"),
            mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        ):
            self.assertEqual(
                quality_commands._process_group_options(), {"creationflags": 512}
            )

    def test_windows_uses_taskkill(self) -> None:
        process = mock.Mock(pid=42)
        with (
            mock.patch.object(quality_commands.os, "name", "nt"),
            mock.patch.object(quality_commands.subprocess, "run") as run,
        ):
            quality_commands._signal_tree(process, signal.SIGTERM)
        self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/PID", "42"])

    def test_vanished_process_group_is_ignored(self) -> None:
        process = mock.Mock(pid=42)
        with mock.patch.object(
            quality_commands.os, "killpg", side_effect=ProcessLookupError
        ) as killpg:
            quality_commands._signal_tree(process, signal.SIGTERM)
        killpg.assert_called_once_with(42, signal.SIGTERM)

    def test_finished_process_is_not_signalled(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        process.communicate.return_value = (None, None)
        with mock.patch.object(quality_commands, "_signal_tree") as signal_tree:
            self.assertEqual(quality_commands.stop_process_tree(process), "")
        signal_tree.assert_not_called()


if __name__ == "__main__":
    unittest.main()
