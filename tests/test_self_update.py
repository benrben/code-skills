import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quality_self_update as update  # noqa: E402

import repo_quality_gate  # noqa: E402

BASE = f"{update.GITHUB_RAW_BASE}/v1/{update.SCRIPTS_PATH}"
REMOTE = {
    f"{BASE}/repo_quality_gate.py": b"import os\nimport quality_loop\n",
    f"{BASE}/quality_loop.py": b"from __future__ import annotations\nfrom . import x\nimport quality_config\nimport json\n",
    f"{BASE}/quality_config.py": b'import os.path\nVERSION = "9.9.9"\n',
    f"{update.GITHUB_RAW_BASE}/v1/{update.THRESHOLDS_PATH}": b'{"schema_version": 3}',
}


def remote(url: str) -> bytes:
    if url not in REMOTE:
        raise ValueError(f"404 {url}")
    return REMOTE[url]


def completed(
    code: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


class FakeGit:
    """Answers the git commands the shared-checkout update runs."""

    def __init__(self, top: Path, **answers: subprocess.CompletedProcess[str]) -> None:
        self.answers = {
            "rev-parse": completed(0, f"{top}\n"),
            "remote": completed(0, "git@github.com:benrben/code-skills.git\n"),
            "status": completed(0, ""),
            "pull": completed(0, ""),
            **answers,
        }
        self.calls: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return self.answers[command[3]]


class StandaloneTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.runner = self.directory / "repo_quality_gate.py"
        self.runner.write_text("old runner", encoding="utf-8")
        self.runner.chmod(0o755)

    def run_update(self, reference: str = "v1") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = update.update_from_github(
                self.runner,
                reference,
                download=remote,
                run=FakeGit(self.directory, **{"rev-parse": completed(1)}),
            )
        return code, out.getvalue(), err.getvalue()

    def test_downloads_every_imported_module(self) -> None:
        code, out, _ = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn("9.9.9", out)
        names = sorted(path.name for path in self.directory.iterdir())
        self.assertEqual(
            names,
            [
                "quality-thresholds.json",
                "quality_config.py",
                "quality_loop.py",
                "repo_quality_gate.py",
            ],
        )
        self.assertEqual(
            self.runner.read_bytes(), REMOTE[f"{BASE}/repo_quality_gate.py"]
        )
        self.assertEqual(self.runner.stat().st_mode & 0o777, 0o755)

    def test_errors_exit_2(self) -> None:
        code, _, err = self.run_update("../main")
        self.assertEqual(code, 2)
        self.assertIn("ref may contain only", err)
        with mock.patch.dict(REMOTE, {f"{BASE}/quality_config.py": b"VERSION = 3\n"}):
            self.assertEqual(self.run_update()[0], 2)

    def test_failed_install_restores_the_originals(self) -> None:
        original = update.replace_bytes
        calls = {"count": 0}

        def flaky(path: Path, payload: bytes) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
            original(path, payload)

        files = {"repo_quality_gate.py": b"new", "added.py": b"x", "third.py": b"y"}
        with mock.patch.object(update, "replace_bytes", flaky):
            with self.assertRaisesRegex(ValueError, "disk full"):
                update.install_files(self.directory, files)
        self.assertEqual(self.runner.read_text(), "old runner")
        self.assertFalse((self.directory / "added.py").exists())

    def test_replace_bytes_cleans_up_on_failure(self) -> None:
        with mock.patch.object(update.os, "replace", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                update.replace_bytes(self.runner, b"new")
        self.assertEqual(
            [p.name for p in self.directory.iterdir()], ["repo_quality_gate.py"]
        )

    def test_version_module_is_fetched_when_not_imported(self) -> None:
        with mock.patch.object(update, "download_modules", return_value={"a.py": b""}):
            version = update.update_standalone(self.directory, "v1", remote)
        self.assertEqual(version, "9.9.9")


class HelperTests(unittest.TestCase):
    def test_refs_and_urls(self) -> None:
        self.assertEqual(update.validate_update_ref("release/1.0"), "release/1.0")
        for bad in ("", "/main", "a/../b", "a b"):
            with self.subTest(ref=bad), self.assertRaises(ValueError):
                update.validate_update_ref(bad)
        self.assertEqual(
            update.github_raw_url("main", "a b.py"),
            f"{update.GITHUB_RAW_BASE}/main/a%20b.py",
        )

    def test_local_imports(self) -> None:
        source = "import os, gate_types.x\nfrom json import loads\nfrom . import y\nfrom diff_scope import ChangeSet\n"
        self.assertEqual(
            update.local_imports(source, "m.py"), {"gate_types", "diff_scope"}
        )
        with self.assertRaisesRegex(ValueError, "not valid Python"):
            update.local_imports("def (", "m.py")

    def test_declared_version_and_thresholds(self) -> None:
        self.assertEqual(
            update.declared_version(b'X = 1\nVERSION: str\nVERSION = "1.2"\n'), "1.2"
        )
        with self.assertRaisesRegex(ValueError, "no VERSION"):
            update.declared_version(b"OTHER = 'x'\n")
        update.validate_thresholds_payload(b'{"schema_version": 3}')
        for bad in (b"{", b"[]", b'{"schema_version": "3"}', b"\xff"):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                update.validate_thresholds_payload(bad)

    def test_normalized_git_remote(self) -> None:
        self.assertEqual(
            update.normalized_git_remote("git@github.com:Benrben/Code-Skills.git\n"),
            "https://github.com/benrben/code-skills",
        )


class CheckoutAndInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.top = Path(directory.name).resolve()
        self.runner = self.top / update.SCRIPTS_PATH / "repo_quality_gate.py"
        self.runner.parent.mkdir(parents=True)

    def update(self, run: Any) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = update.update_from_github(
                self.runner, "main", download=remote, run=run
            )
        return code, err.getvalue()

    def test_clean_checkout_pulls(self) -> None:
        git = FakeGit(self.top)
        self.assertEqual(self.update(git)[0], 0)
        self.assertEqual(git.calls[-1][3:], ["pull", "--ff-only", "origin", "main"])

    def test_checkout_problems(self) -> None:
        self.assertIn(
            "local changes",
            self.update(FakeGit(self.top, status=completed(0, " M x")))[1],
        )
        self.assertIn(
            "bad", self.update(FakeGit(self.top, status=completed(1, err="bad")))[1]
        )
        self.assertIn(
            "git pull failed", self.update(FakeGit(self.top, pull=completed(1)))[1]
        )

    def test_other_clones_are_not_shared_checkouts(self) -> None:
        other = FakeGit(
            self.top, remote=completed(0, "https://github.com/someone/fork")
        )
        self.assertIsNone(update.shared_checkout_root(self.runner, other))
        elsewhere = FakeGit(self.top / "x")
        self.assertIsNone(update.shared_checkout_root(self.runner, elsewhere))
        with mock.patch.object(update.shutil, "which", return_value=None):
            self.assertIsNone(
                update.shared_checkout_root(self.runner, FakeGit(self.top))
            )

    def test_installed_skill_runs_its_installer(self) -> None:
        (self.top / "skills" / "code-discipline" / "SKILL.md").write_text("skill")
        (self.runner.parent / "install.py").write_text("")
        calls: list[list[str]] = []

        def run(
            command: Sequence[str], **options: Any
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(command))
            if command[0] == "git":
                return completed(1)
            return completed(5)

        self.assertEqual(self.update(run)[0], 5)
        self.assertEqual(calls[-1][2:], ["--update-current", "--ref", "main"])


class EntryPointTests(unittest.TestCase):
    def test_update_flag_and_delegation(self) -> None:
        with mock.patch.object(
            repo_quality_gate.quality_self_update, "update_from_github", return_value=3
        ) as fake:
            self.assertEqual(repo_quality_gate.main(["--update-from-github"]), 3)
        self.assertEqual(fake.call_args.args[1], "main")
        with mock.patch.object(
            repo_quality_gate.quality_loop, "main", return_value=4
        ) as loop:
            self.assertEqual(repo_quality_gate.main(["--root", "x", "--fast"]), 4)
        loop.assert_called_once_with(["--root", "x", "--fast"])

    def test_root_shim_loads_the_engine(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "root_shim", ROOT / "repo_quality_gate.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.VERSION, repo_quality_gate.VERSION)
        self.assertTrue(callable(module.run_analysis))

    def test_version_output(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "repo_quality_gate.py"), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.stdout.strip(), repo_quality_gate.VERSION)
        self.assertEqual(json.loads('"' + result.stdout.strip() + '"'), "7.0.0")


if __name__ == "__main__":
    unittest.main()
