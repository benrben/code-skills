"""Find a repository's conventional test command from what it declares.

Each finder checks one toolchain's manifest and that its tool is installed
(project-local first, then PATH). Nothing is ever installed, and the first
toolchain the repository declares wins, in the order of ``COMMAND_FINDERS``.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import gate_tests_native

Which = Callable[[str], "str | None"]
CommandFinder = Callable[[Path, Which], "list[str] | None"]


def find_executable(root: Path, name: str, which: Which = shutil.which) -> str | None:
    """A project-local executable path (virtualenv, node_modules, vendor), or
    the bare name when it is on PATH."""
    for directory in (".venv/bin", "venv/bin", "node_modules/.bin", "vendor/bin"):
        candidate = root / directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return name if which(name) else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def read_package_json(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(root / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def package_manager(root: Path) -> str:
    """The package manager the lockfile names; npm when there is none."""
    lockfiles = (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    )
    for lockfile, manager in lockfiles:
        if (root / lockfile).is_file():
            return manager
    return "npm"


def _declares_test_script(package: Mapping[str, Any]) -> bool:
    """True for a real ``test`` script; npm init's placeholder only fails."""
    scripts = package.get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return (
        isinstance(script, str)
        and bool(script.strip())
        and "no test specified" not in script
    )


def npm_workspace_dirs(root: Path) -> list[Path]:
    """Workspace package folders declared in the root package.json."""
    workspaces = read_package_json(root).get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if not isinstance(workspaces, list):
        return []
    return [
        path
        for pattern in workspaces
        for path in sorted(root.glob(str(pattern)))
        if (path / "package.json").is_file()
    ]


def _node_command(root: Path, which: Which) -> list[str] | None:
    manager = package_manager(root)
    tool = find_executable(root, manager, which)
    if tool is None:
        return None
    if _declares_test_script(read_package_json(root)):
        # ``bun test`` is Bun's own runner; ``bun run test`` runs the script.
        return [tool, "run", "test"] if manager == "bun" else [tool, "test"]
    if manager == "npm" and npm_workspace_dirs(root):
        return [tool, "test", "--workspaces", "--if-present"]
    return None


_PYTHON_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
)
_PYTEST_SECTIONS = (
    ("pyproject.toml", "[tool.pytest"),
    ("setup.cfg", "[tool:pytest]"),
    ("tox.ini", "[pytest]"),
)
_PYTEST_DEPENDENCY_FILES = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
)


def _test_dir(root: Path) -> Path | None:
    for name in ("tests", "test"):
        if (root / name).is_dir():
            return root / name
    return None


def _is_python_project(root: Path) -> bool:
    if any((root / marker).is_file() for marker in _PYTHON_MARKERS):
        return True
    directory = _test_dir(root)
    return directory is not None and any(directory.rglob("*.py"))


def project_python(root: Path, which: Which = shutil.which) -> str | None:
    """The project's virtualenv interpreter, else python3/python on PATH."""
    for directory in (".venv/bin", "venv/bin"):
        candidate = root / directory / "python"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    for name in ("python3", "python"):
        if which(name):
            return name
    return None


def _pytest_configured(root: Path) -> bool:
    """pytest has its own config, conftest, or is a declared dependency."""
    if any((root / name).is_file() for name in ("pytest.ini", "conftest.py")):
        return True
    if any(marker in _read(root / name) for name, marker in _PYTEST_SECTIONS):
        return True
    return any(
        "pytest" in _read(root / name).lower() for name in _PYTEST_DEPENDENCY_FILES
    )


def _pytest_installed(python: str, which: Which) -> bool:
    """pytest is importable by ``python``: in its virtualenv when it is one,
    else on PATH next to the system interpreter."""
    interpreter = Path(python)
    if not interpreter.is_absolute():
        return which("pytest") is not None
    environment = interpreter.parent.parent
    return (interpreter.parent / "pytest").is_file() or any(
        (environment / "lib").glob("python*/site-packages/pytest")
    )


def _python_command(root: Path, which: Which) -> list[str] | None:
    if not _is_python_project(root):
        return None
    python = project_python(root, which)
    if python is None:
        return None
    if _pytest_configured(root) or _pytest_installed(python, which):
        return [python, "-m", "pytest", "-q"]
    command = [python, "-m", "unittest", "discover"]
    directory = _test_dir(root)
    if directory is None:
        return command
    command += ["-s", directory.name]
    # A package test folder imports as ``tests.x``, which needs the root on
    # the import path.
    if (directory / "__init__.py").is_file():
        command += ["-t", "."]
    return command


def _tool_command(
    manifest: str, tool: str, *arguments: str
) -> Callable[[Path, Which], list[str] | None]:
    """A finder for a toolchain that needs one manifest file and its tool."""

    def finder(root: Path, which: Which) -> list[str] | None:
        if not (root / manifest).is_file():
            return None
        executable = find_executable(root, tool, which)
        return [executable, *arguments] if executable else None

    return finder


def _ruby_command(root: Path, which: Which) -> list[str] | None:
    if not (root / "Gemfile").is_file():
        return None
    bundle = find_executable(root, "bundle", which)
    if bundle is None:
        return None
    if (root / "spec").is_dir() or (root / ".rspec").is_file():
        return [bundle, "exec", "rspec"]
    return [bundle, "exec", "rake", "test"]


def _php_command(root: Path, which: Which) -> list[str] | None:
    phpunit = root / "vendor" / "bin" / "phpunit"
    if phpunit.is_file() and os.access(phpunit, os.X_OK):
        return [str(phpunit)]
    return None


def _wrapper_or_tool(root: Path, wrapper: str, tool: str, which: Which) -> str | None:
    script = root / wrapper
    if script.is_file() and os.access(script, os.X_OK):
        return str(script)
    return find_executable(root, tool, which)


def _maven_command(root: Path, which: Which) -> list[str] | None:
    if not (root / "pom.xml").is_file():
        return None
    maven = _wrapper_or_tool(root, "mvnw", "mvn", which)
    return [maven, "-o", "-q", "test"] if maven else None


def _gradle_command(root: Path, which: Which) -> list[str] | None:
    """Gradle skips an UP-TO-DATE test task and then writes no fresh JUnit
    report to count, so ``cleanTest`` (every Gradle version) forces a run."""
    wrapper = root / "gradlew"
    if wrapper.is_file():
        return [str(wrapper), "cleanTest", "test", "--offline"]
    return None


def _cargo_command(root: Path, which: Which) -> list[str] | None:
    """``--frozen`` needs a committed Cargo.lock; library crates often have
    none, and cargo would then refuse to run any test."""
    if not (root / "Cargo.toml").is_file():
        return None
    cargo = find_executable(root, "cargo", which)
    if cargo is None:
        return None
    locked = (root / "Cargo.lock").is_file()
    return [cargo, "test", "--workspace", "--frozen" if locked else "--offline"]


_DOTNET_PATTERNS = ("*.sln", "*.slnx", "*.csproj", "*.fsproj", "*.vbproj")


def _dotnet_command(root: Path, which: Which) -> list[str] | None:
    if not any(any(root.glob(pattern)) for pattern in _DOTNET_PATTERNS):
        return None
    dotnet = find_executable(root, "dotnet", which)
    return [dotnet, "test", "--no-restore"] if dotnet else None


def _dart_command(root: Path, which: Which) -> list[str] | None:
    pubspec = root / "pubspec.yaml"
    if not pubspec.is_file():
        return None
    if "sdk: flutter" in _read(pubspec):
        flutter = find_executable(root, "flutter", which)
        if flutter:
            return [flutter, "test"]
    dart = find_executable(root, "dart", which)
    return [dart, "test"] if dart else None


def _local_which(root: Path, which: Which) -> Which:
    """``which`` that prefers project-local executables (``find_executable``)."""
    return lambda name: find_executable(root, name, which)


def _native_command(root: Path, which: Which) -> list[str] | None:
    """CMake (``ctest``) or Meson (``meson test``); the tests gate builds the
    project first, see ``gate_tests_native``."""
    return gate_tests_native.native_test_command(root, _local_which(root, which))


def native_toolchain_problem(root: Path, which: Which = shutil.which) -> str | None:
    """What a CMake or Meson project lacks to build and test, or None."""
    return gate_tests_native.toolchain_problem(root, _local_which(root, which))


# Checked in order; the first toolchain the repository declares wins.
COMMAND_FINDERS: tuple[CommandFinder, ...] = (
    _node_command,
    _python_command,
    _tool_command("go.mod", "go", "test", "-json", "./..."),
    _cargo_command,
    _ruby_command,
    _php_command,
    _maven_command,
    _gradle_command,
    _dotnet_command,
    _tool_command("Package.swift", "swift", "test"),
    _dart_command,
    _tool_command("mix.exs", "mix", "test"),
    _native_command,
)


def infer_test_command(root: Path, which: Which = shutil.which) -> list[str] | None:
    """The repository's conventional test command, or None when no supported
    toolchain is declared and installed."""
    for finder in COMMAND_FINDERS:
        command = finder(root, which)
        if command:
            return command
    return None
