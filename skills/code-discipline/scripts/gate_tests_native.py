"""C and C++ projects built with CMake or Meson.

A CMake project that declares tests runs ``ctest``; a Meson project runs
``meson test``. Neither works before the project is configured and built, so
``native_build`` turns such a command into the configure and build steps
plus the test command, all in a build directory under the run's own output
folder. The project's own build directories are never touched: ``ctest``
with ``--test-dir`` and ``meson test`` with ``-C`` name a build the project
made itself and are left alone. Nothing is ever installed.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

Command = tuple[str, ...]
Which = Callable[[str], "str | None"]

CMAKE = "cmake"
MESON = "meson"
BUILD_TYPE = "Debug"
COVERAGE_FLAG = "--coverage"

# Source files a C/C++ coverage run measures. Headers are left out: one that
# holds only declarations has no executable line, so a report never names it.
MEASURED_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm"})

_CMAKE_TESTING = re.compile(
    r"^\s*(?:enable_testing\s*\(|add_test\s*\(|include\s*\(\s*CTest\b)",
    re.IGNORECASE | re.MULTILINE,
)
_REQUIRED_TOOLS = {CMAKE: ("cmake", "ctest"), MESON: ("meson", "ninja")}
_COMPILERS = ("cc", "c++", "gcc", "g++", "clang", "clang++", "cl")
# CMake's language flag variables and the environment variables that would
# have initialized them, so the coverage flag extends the user's flags.
_CMAKE_COVERAGE_FLAGS = (
    ("CMAKE_C_FLAGS", "CFLAGS"),
    ("CMAKE_CXX_FLAGS", "CXXFLAGS"),
    ("CMAKE_EXE_LINKER_FLAGS", "LDFLAGS"),
    ("CMAKE_SHARED_LINKER_FLAGS", "LDFLAGS"),
    ("CMAKE_MODULE_LINKER_FLAGS", "LDFLAGS"),
)


@dataclasses.dataclass(frozen=True)
class NativeBuild:
    """Configure and build ``steps`` in ``directory``, then the test ``command``."""

    directory: Path
    steps: tuple[Command, ...]
    command: Command


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def project_kind(root: Path) -> str | None:
    """``cmake`` for a root CMakeLists.txt that enables testing, ``meson`` for
    a root meson.build, else None."""
    cmake = root / "CMakeLists.txt"
    if cmake.is_file() and _CMAKE_TESTING.search(_read(cmake)):
        return CMAKE
    if (root / "meson.build").is_file():
        return MESON
    return None


def _has_compiler(which: Which, environ: Mapping[str, str]) -> bool:
    configured = [environ.get(name, "") for name in ("CC", "CXX")]
    names = [shlex.split(value)[0] for value in configured if value.strip()]
    return any(which(name) for name in (*names, *_COMPILERS))


def toolchain_problem(
    root: Path, which: Which = shutil.which, environ: Mapping[str, str] | None = None
) -> str | None:
    """What a CMake or Meson project lacks to build and test, or None (also
    for projects that are neither)."""
    kind = project_kind(root)
    if kind is None:
        return None
    for tool in _REQUIRED_TOOLS[kind]:
        if which(tool) is None:
            return f"{tool} is not on PATH"
    inherited = os.environ if environ is None else environ
    generator = inherited.get("CMAKE_GENERATOR") or which("ninja") or which("make")
    if kind == CMAKE and not generator:
        return "neither ninja nor make is on PATH"
    if not _has_compiler(which, inherited):
        return "no C or C++ compiler is on PATH"
    return None


def native_test_command(root: Path, which: Which = shutil.which) -> list[str] | None:
    """``ctest --output-on-failure`` or ``meson test --print-errorlogs`` for a
    project whose toolchain is installed; ``which`` gives each tool's path."""
    kind = project_kind(root)
    if kind is None or toolchain_problem(root, which) is not None:
        return None
    if kind == CMAKE:
        return [which("ctest") or "ctest", "--output-on-failure"]
    return [which("meson") or "meson", "test", "--print-errorlogs"]


def _program(command: Sequence[str]) -> str:
    name = Path(command[0]).name.lower() if command else ""
    return name.removesuffix(".exe")


def _names_option(command: Sequence[str], option: str) -> bool:
    return any(part == option or part.startswith(option) for part in command[1:])


def _extended(environ: Mapping[str, str], name: str) -> str:
    inherited = environ.get(name, "").strip()
    return f"{inherited} {COVERAGE_FLAG}" if inherited else COVERAGE_FLAG


def _sibling(command: Sequence[str], tool: str) -> str:
    """``tool`` next to the command's program when that is a path."""
    program = Path(command[0])
    return str(program.with_name(tool)) if program.parent != Path() else tool


@dataclasses.dataclass(frozen=True)
class _Options:
    coverage: bool
    environ: Mapping[str, str]
    which: Which


def _cmake_build(
    root: Path, command: Sequence[str], directory: Path, options: _Options
) -> NativeBuild:
    cmake = _sibling(command, "cmake")
    configure = [cmake, "-S", str(root), "-B", str(directory)]
    configure.append(f"-DCMAKE_BUILD_TYPE={BUILD_TYPE}")
    if not options.environ.get("CMAKE_GENERATOR") and options.which("ninja"):
        configure += ["-G", "Ninja"]
    if options.coverage:
        configure += [
            f"-D{variable}={_extended(options.environ, source)}"
            for variable, source in _CMAKE_COVERAGE_FLAGS
        ]
    jobs = str(os.cpu_count() or 1)
    build = (cmake, "--build", str(directory), "--config", BUILD_TYPE, "-j", jobs)
    test = (command[0], "--test-dir", str(directory), "-C", BUILD_TYPE, *command[1:])
    return NativeBuild(directory, (tuple(configure), build), test)


def _meson_build(
    root: Path, command: Sequence[str], directory: Path, options: _Options
) -> NativeBuild:
    meson = command[0]
    coverage = ("-Db_coverage=true",) if options.coverage else ()
    setup = (meson, "setup", "--buildtype=debug", *coverage, str(directory), str(root))
    build = (meson, "compile", "-C", str(directory))
    test = (meson, "test", "-C", str(directory), *command[2:])
    return NativeBuild(directory, (setup, build), test)


def native_build(
    root: Path,
    command: Sequence[str],
    build_root: Path,
    *,
    coverage: bool,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
) -> NativeBuild | None:
    """The steps that build the project under ``build_root`` and the command
    that then tests it, or None when ``command`` is not a CMake or Meson test
    command of this project or names its own build directory. ``coverage``
    builds with ``--coverage``, keeping flags ``environ`` already sets."""
    options = _Options(coverage, os.environ if environ is None else environ, which)
    program = _program(command)
    if program == "ctest" and not _names_option(command, "--test-dir"):
        if (root / "CMakeLists.txt").is_file():
            return _cmake_build(root, command, build_root / "cmake-build", options)
    meson_test = program == "meson" and tuple(command[1:2]) == ("test",)
    if meson_test and not _names_option(command, "-C"):
        if (root / "meson.build").is_file():
            return _meson_build(root, command, build_root / "meson-build", options)
    return None
