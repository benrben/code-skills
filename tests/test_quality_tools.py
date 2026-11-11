from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest import mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quality_tools  # noqa: E402
from gate_types import CommandRun  # noqa: E402

PINNED = json.dumps(quality_tools.PYTHON_PINS)
NOTHING = json.dumps({name: None for name in quality_tools.PYTHON_PINS})


class FakeRun:
    """Records commands; answers version probes and installs from scripts."""

    def __init__(
        self,
        probes: Sequence[str] = (PINNED,),
        on_install: Callable[[Sequence[str]], None] | None = None,
        install_code: int = 0,
    ) -> None:
        self.probes = list(probes)
        self.on_install = on_install
        self.install_code = install_code
        self.commands: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []

    def __call__(
        self,
        command: Sequence[str],
        root: Path,
        timeout: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        self.commands.append(list(command))
        self.envs.append(env)
        if (
            "-c" in command
            and command[command.index("-c") + 1] == quality_tools.VERSION_PROBE
        ):
            output = self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]
            return CommandRun(tuple(command), 0, output, 0.1)
        if self.on_install:
            self.on_install(command)
        return CommandRun(tuple(command), self.install_code, "warning\nboom\n", 0.1)

    def installs(self) -> list[list[str]]:
        return [c for c in self.commands if quality_tools.VERSION_PROBE not in c]


def no_download(url: str) -> bytes:
    raise AssertionError(f"unexpected download {url}")


class ToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "repo"
        self.root.mkdir()
        self.cache = Path(directory.name) / "cache"

    def bootstrap(
        self,
        run: FakeRun,
        *,
        auto_install: bool = True,
        which: Callable[[str], str | None] = lambda name: None,
        download: Callable[[str], bytes] = no_download,
    ) -> quality_tools.ToolSet:
        return quality_tools.bootstrap_tools(
            self.root,
            auto_install=auto_install,
            cache_dir=self.cache,
            run=run,
            download=download,
            which=which,
        )


class PythonToolTests(ToolTestCase):
    def test_no_install_uses_the_current_interpreter(self) -> None:
        run = FakeRun()
        tools = self.bootstrap(run, auto_install=False)
        self.assertEqual(tools.tool_python.executable, sys.executable)
        self.assertIn("PYTHONPATH", tools.tool_python.env)
        self.assertEqual(tools.setup_notes, [])
        self.assertEqual(run.installs(), [])
        self.assertIs(tools.coverage_tools.tool_python, tools.tool_python)

    def test_no_install_notes_missing_tools(self) -> None:
        tools = self.bootstrap(FakeRun([NOTHING]), auto_install=False)
        self.assertIn("lizard==1.26.0", tools.setup_notes[0])
        self.assertIn("current interpreter", tools.setup_notes[0])

    def test_cached_tools_are_not_reinstalled(self) -> None:
        run = FakeRun()
        tools = self.bootstrap(run)
        directory = quality_tools.python_tool_directory(self.cache)
        path = tools.tool_python.env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(path[0], str(directory))
        self.assertEqual(len(path), 2)
        self.assertEqual(run.installs(), [])

    def test_missing_tools_are_installed_into_the_cache(self) -> None:
        run = FakeRun([NOTHING, PINNED])
        tools = self.bootstrap(run)
        [install] = run.installs()
        self.assertEqual(install[1:4], ["-m", "pip", "install"])
        target = install[install.index("--target") + 1]
        self.assertTrue(tools.tool_python.env["PYTHONPATH"].startswith(target))
        self.assertIn("pygments==2.21.0", install)
        self.assertEqual(tools.setup_notes, [])

    def test_failed_install_is_noted(self) -> None:
        tools = self.bootstrap(FakeRun(["not json"], install_code=1))
        self.assertIn("Could not install", tools.setup_notes[0])
        self.assertIn("boom", tools.setup_notes[0])
        self.assertIn("tool cache", tools.setup_notes[1])

    def test_default_tool_cache(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/x/cache"}):
            self.assertEqual(
                quality_tools.default_tool_cache(),
                Path("/x/cache/code-discipline/tools"),
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                quality_tools.default_tool_cache(),
                Path.home() / ".cache" / "code-discipline" / "tools",
            )


class NodeAndRustToolTests(ToolTestCase):
    def c8_binary(self) -> Path:
        prefix = self.cache / "node" / f"c8-{quality_tools.C8_VERSION}"
        return prefix / "node_modules" / ".bin" / "c8"

    def test_c8_only_for_node_projects(self) -> None:
        self.assertIsNone(self.bootstrap(FakeRun()).coverage_tools.c8)

    def test_c8_is_installed_with_npm(self) -> None:
        (self.root / "package.json").write_text("{}")

        def create(command: Sequence[str]) -> None:
            self.c8_binary().parent.mkdir(parents=True)
            self.c8_binary().write_text("")

        run = FakeRun(on_install=create)
        tools = self.bootstrap(run, which=lambda name: f"/bin/{name}")
        self.assertEqual(tools.coverage_tools.c8, str(self.c8_binary()))
        self.assertEqual(run.installs()[0][-1], "c8@10.1.3")
        again = self.bootstrap(FakeRun(), which=lambda name: None)
        self.assertEqual(again.coverage_tools.c8, str(self.c8_binary()))

    def test_c8_problems_are_noted(self) -> None:
        (self.root / "package.json").write_text("{}")
        tools = self.bootstrap(FakeRun())
        self.assertIn("c8 10.1.3 is not in the tool cache", tools.setup_notes[0])
        failed = self.bootstrap(FakeRun(), which=lambda name: f"/bin/{name}")
        self.assertIn("Could not install c8", failed.setup_notes[0])

    def test_cargo_llvm_cov(self) -> None:
        self.assertIsNone(self.bootstrap(FakeRun()).coverage_tools.cargo_llvm_cov)
        (self.root / "Cargo.toml").write_text("[package]\n")
        on_path = self.bootstrap(FakeRun(), which=lambda name: f"/bin/{name}")
        self.assertEqual(on_path.coverage_tools.cargo_llvm_cov, "/bin/cargo-llvm-cov")
        missing = self.bootstrap(FakeRun())
        self.assertIn("cargo-llvm-cov is not installed", missing.setup_notes[0])
        binary = self.cache / "cargo" / "bin" / "cargo-llvm-cov"

        def cargo(name: str) -> str | None:
            return "/bin/cargo" if name == "cargo" else None

        failed = self.bootstrap(FakeRun(), which=cargo)
        self.assertIn("Could not install cargo-llvm-cov", failed.setup_notes[0])

        def create(command: Sequence[str]) -> None:
            binary.parent.mkdir(parents=True)
            binary.write_text("")

        run = FakeRun(on_install=create)
        installed = self.bootstrap(run, which=cargo)
        self.assertEqual(installed.coverage_tools.cargo_llvm_cov, str(binary))
        self.assertIn("--locked", run.installs()[0])
        cached = self.bootstrap(FakeRun())
        self.assertEqual(cached.coverage_tools.cargo_llvm_cov, str(binary))


class JacocoTests(ToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.payloads = {"agent": b"agent-jar", "cli": b"cli-jar"}
        jars = {
            kind: (f"https://example.test/{kind}.jar", hashlib.sha256(data).hexdigest())
            for kind, data in self.payloads.items()
        }
        patcher = mock.patch.dict(quality_tools.JACOCO_JARS, jars)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, url: str) -> bytes:
        return self.payloads[url.rsplit("/", 1)[1].removesuffix(".jar")]

    def test_not_a_jvm_project(self) -> None:
        tools = self.bootstrap(FakeRun())
        self.assertIsNone(tools.coverage_tools.jacoco_agent)

    def test_jars_are_downloaded_verified_and_reused(self) -> None:
        (self.root / "pom.xml").write_text("<project/>")
        tools = self.bootstrap(FakeRun(), download=self.download)
        agent = tools.coverage_tools.jacoco_agent
        self.assertTrue(agent and Path(agent).read_bytes() == b"agent-jar")
        cached = self.bootstrap(FakeRun(), auto_install=False)
        self.assertEqual(
            cached.coverage_tools.jacoco_cli, tools.coverage_tools.jacoco_cli
        )

    def test_problems_are_noted(self) -> None:
        (self.root / "build.gradle").write_text("")
        offline = self.bootstrap(FakeRun(), auto_install=False)
        self.assertIn("not in the tool cache", offline.setup_notes[0])

        def unavailable(url: str) -> bytes:
            raise ValueError("offline")

        failed = self.bootstrap(FakeRun(), download=unavailable)
        self.assertIn("Could not download the JaCoCo agent jar", failed.setup_notes[0])
        tampered = self.bootstrap(FakeRun(), download=lambda url: b"other")
        self.assertIn("failed its checksum", tampered.setup_notes[0])
        self.assertIsNone(tampered.coverage_tools.jacoco_agent)


class DownloadTests(unittest.TestCase):
    def response(self, payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def test_download_bytes(self) -> None:
        with mock.patch.object(
            quality_tools, "urlopen", return_value=self.response(b"data")
        ):
            self.assertEqual(quality_tools.download_bytes("https://x.test/a"), b"data")

    def test_download_limits_and_errors(self) -> None:
        big = b"x" * (quality_tools.DOWNLOAD_LIMIT + 1)
        with mock.patch.object(
            quality_tools, "urlopen", return_value=self.response(big)
        ):
            with self.assertRaisesRegex(ValueError, "larger than"):
                quality_tools.download_bytes("https://x.test/a")
        with mock.patch.object(quality_tools, "urlopen", side_effect=URLError("down")):
            with self.assertRaisesRegex(ValueError, "down"):
                quality_tools.download_bytes("https://x.test/a")

    def test_last_line(self) -> None:
        self.assertEqual(quality_tools._last_line(""), "no output")
        self.assertEqual(quality_tools._last_line("a\nb\n\n"), "b")


if __name__ == "__main__":
    unittest.main()
