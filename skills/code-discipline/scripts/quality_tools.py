"""Pinned analysis tools in the skill's own cache.

The gates measure with tools the project does not have to provide: lizard
and Pygments (complexity, dead code, duplication) and coverage.py for Python
projects; c8 for Node, cargo-llvm-cov for Rust, and the JaCoCo jars for JVM
projects. They are installed into a user cache, never into the project, and
never replace the project's own dependencies.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from gate_coverage_recipes import CoverageTools
from gate_types import CommandRun, ToolPython
from quality_commands import run_command

PYTHON_PINS = {"coverage": "7.10.2", "lizard": "1.26.0", "pygments": "2.21.0"}
C8_VERSION = "10.1.3"
JACOCO_VERSION = "0.8.12"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2/org/jacoco"
JACOCO_JARS = {
    "agent": (
        f"{MAVEN_CENTRAL}/org.jacoco.agent/{JACOCO_VERSION}/"
        f"org.jacoco.agent-{JACOCO_VERSION}-runtime.jar",
        "115e8e6e6593ca3a9892dfef695df4d487c706e59e71e64dc0ab95716ee02622",
    ),
    "cli": (
        f"{MAVEN_CENTRAL}/org.jacoco.cli/{JACOCO_VERSION}/"
        f"org.jacoco.cli-{JACOCO_VERSION}-nodeps.jar",
        "594c01125b84864a7fc5a9efab19b71b7de36b27346863f6d7cd0cd45400ceb0",
    ),
}
JVM_MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts")
INSTALL_TIMEOUT = 900
CARGO_INSTALL_TIMEOUT = 1800
PROBE_TIMEOUT = 60
DOWNLOAD_LIMIT = 5_000_000

VERSION_PROBE = """
import importlib.metadata, json, sys
found = {}
for name in sys.argv[1:]:
    try:
        found[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        found[name] = None
print(json.dumps(found))
"""

CommandFn = Callable[
    [Sequence[str], Path, int, Optional[Mapping[str, str]]], CommandRun
]
Download = Callable[[str], bytes]
Which = Callable[[str], Optional[str]]


@dataclasses.dataclass(frozen=True)
class ToolSet:
    """The tools one run measures with, and what setting them up reported."""

    tool_python: ToolPython
    coverage_tools: CoverageTools
    setup_notes: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class _Setup:
    root: Path
    cache: Path
    auto_install: bool
    run: CommandFn
    download: Download
    which: Which
    notes: list[str]


def default_tool_cache() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured) if configured else Path.home() / ".cache"
    return base / "code-discipline" / "tools"


def bootstrap_tools(
    root: Path,
    *,
    auto_install: bool,
    cache_dir: Path | None,
    run: CommandFn = run_command,
    download: Download | None = None,
    which: Which = shutil.which,
) -> ToolSet:
    """Find or install the pinned tools this repository needs.

    With ``auto_install`` off nothing is downloaded: Python tools come from the
    current interpreter and the other tools only from the cache or PATH."""
    setup = _Setup(
        root,
        cache_dir or default_tool_cache(),
        auto_install,
        run,
        download or download_bytes,
        which,
        [],
    )
    tool_python = _python_tools(setup)
    coverage_tools = CoverageTools(
        tool_python=tool_python,
        c8=_c8(setup),
        cargo_llvm_cov=_cargo_llvm_cov(setup),
        **_jacoco(setup),
    )
    return ToolSet(tool_python, coverage_tools, setup.notes)


# --- Python tools -------------------------------------------------------------


def python_tool_directory(cache: Path) -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    key = hashlib.sha256(sys.executable.encode()).hexdigest()[:10]
    return cache / f"python-{version}-{key}"


def _python_tools(setup: _Setup) -> ToolPython:
    # The project's own interpreter may run the tests (coverage.py), so the
    # path names where the tools live, not just this interpreter's defaults.
    interpreter_packages = sysconfig.get_paths()["purelib"]
    if not setup.auto_install:
        tool_python = ToolPython(sys.executable, {"PYTHONPATH": interpreter_packages})
        _note_missing(setup, tool_python, "installed in the current interpreter")
        return tool_python
    directory = python_tool_directory(setup.cache)
    path = os.pathsep.join([str(directory), interpreter_packages])
    tool_python = ToolPython(sys.executable, {"PYTHONPATH": path})
    missing = _missing_pins(setup, tool_python)
    if missing:
        directory.mkdir(parents=True, exist_ok=True)
        install = setup.run(
            _pip_install(directory, missing), setup.root, INSTALL_TIMEOUT, None
        )
        if not install.succeeded:
            setup.notes.append(
                f"Could not install {', '.join(missing)}: {_last_line(install.output)}"
            )
        _note_missing(setup, tool_python, "installed in the tool cache")
    return tool_python


def _pip_install(directory: Path, packages: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        "--upgrade",
        "--target",
        str(directory),
        *packages,
    ]


def _missing_pins(setup: _Setup, tool_python: ToolPython) -> list[str]:
    """``name==version`` for every pinned tool the interpreter cannot import
    at its pinned version."""
    command = [tool_python.executable, "-c", VERSION_PROBE, *PYTHON_PINS]
    probe = setup.run(command, setup.root, PROBE_TIMEOUT, tool_python.env)
    try:
        found = json.loads(probe.output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {}
    return [
        f"{name}=={version}"
        for name, version in PYTHON_PINS.items()
        if not isinstance(found, dict) or found.get(name) != version
    ]


def _note_missing(setup: _Setup, tool_python: ToolPython, where: str) -> None:
    missing = _missing_pins(setup, tool_python)
    if missing:
        setup.notes.append(
            f"Pinned analysis tools are not {where}: {', '.join(missing)}"
        )


# --- Node, Rust, JVM ----------------------------------------------------------


def _c8(setup: _Setup) -> str | None:
    if not (setup.root / "package.json").is_file():
        return None
    prefix = setup.cache / "node" / f"c8-{C8_VERSION}"
    binary = prefix / "node_modules" / ".bin" / ("c8.cmd" if os.name == "nt" else "c8")
    if binary.exists():
        return str(binary)
    npm = setup.which("npm")
    if not setup.auto_install or npm is None:
        setup.notes.append(f"c8 {C8_VERSION} is not in the tool cache.")
        return None
    prefix.mkdir(parents=True, exist_ok=True)
    command = [npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund"]
    install = setup.run(
        [*command, f"c8@{C8_VERSION}"], setup.root, INSTALL_TIMEOUT, None
    )
    if binary.exists():
        return str(binary)
    setup.notes.append(
        f"Could not install c8 {C8_VERSION}: {_last_line(install.output)}"
    )
    return None


def _cargo_llvm_cov(setup: _Setup) -> str | None:
    if not (setup.root / "Cargo.toml").is_file():
        return None
    existing = setup.which("cargo-llvm-cov")
    if existing:
        return existing
    cargo_root = setup.cache / "cargo"
    name = "cargo-llvm-cov.exe" if os.name == "nt" else "cargo-llvm-cov"
    binary = cargo_root / "bin" / name
    if binary.exists():
        return str(binary)
    cargo = setup.which("cargo")
    if not setup.auto_install or cargo is None:
        setup.notes.append("cargo-llvm-cov is not installed.")
        return None
    command = [cargo, "+stable", "install", "cargo-llvm-cov", "--locked"]
    install = setup.run(
        [*command, "--root", str(cargo_root)], setup.root, CARGO_INSTALL_TIMEOUT, None
    )
    if binary.exists():
        return str(binary)
    setup.notes.append(
        f"Could not install cargo-llvm-cov: {_last_line(install.output)}"
    )
    return None


def _jacoco(setup: _Setup) -> dict[str, str | None]:
    if not any((setup.root / name).is_file() for name in JVM_MANIFESTS):
        return {}
    directory = setup.cache / "jacoco" / JACOCO_VERSION
    jars = {kind: _jacoco_jar(setup, directory, kind) for kind in JACOCO_JARS}
    return {"jacoco_agent": jars["agent"], "jacoco_cli": jars["cli"]}


def _jacoco_jar(setup: _Setup, directory: Path, kind: str) -> str | None:
    url, digest = JACOCO_JARS[kind]
    path = directory / f"jacoco{kind}.jar"
    if path.is_file() and _sha256(path.read_bytes()) == digest:
        return str(path)
    if not setup.auto_install:
        setup.notes.append(f"The JaCoCo {kind} jar is not in the tool cache.")
        return None
    try:
        payload = setup.download(url)
    except ValueError as error:
        setup.notes.append(f"Could not download the JaCoCo {kind} jar: {error}")
        return None
    if _sha256(payload) != digest:
        setup.notes.append(f"The downloaded JaCoCo {kind} jar failed its checksum.")
        return None
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.download")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return str(path)


# --- Helpers ------------------------------------------------------------------


def download_bytes(url: str) -> bytes:
    """The body at ``url``; ValueError when it cannot be downloaded."""
    request = Request(url, headers={"User-Agent": "code-discipline-quality-loop"})
    try:
        with urlopen(request, timeout=60) as response:
            payload = bytes(response.read(DOWNLOAD_LIMIT + 1))
    except (URLError, OSError) as error:
        raise ValueError(f"{url}: {error}") from error
    if len(payload) > DOWNLOAD_LIMIT:
        raise ValueError(f"{url}: larger than {DOWNLOAD_LIMIT} bytes")
    return payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"
