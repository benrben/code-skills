"""Write gcov text reports for the coverage data a C/C++ test run left.

The C/C++ coverage recipe runs this after the tests with the tool
interpreter: ``python -S gate_coverage_gcov_collect.py BUILD ROOT REPORT``.
It picks the gcov that reads the compiler's data (GCC's ``gcov`` for GCC,
``llvm-cov gcov`` for Clang and Apple Clang), runs it on every ``.gcda`` file
in the build directory, and writes one ``.gcov`` report per data file into
the report directory. Only records of files under the root and outside the
build directory are kept, with absolute paths, so system headers and
generated sources never count. It imports only the standard library.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

TIMEOUT_SECONDS = 300
GCOV_OPTIONS = ("-b", "-c", "-t")

_CMAKE_ID = re.compile(r'set\(CMAKE_\w+_COMPILER_ID "([^"]+)"\)')
_CMAKE_PATH = re.compile(r'set\(CMAKE_\w+_COMPILER "([^"]+)"\)')
_SOURCE_HEADER = re.compile(r"^(\s*-:\s*0:Source:)(.*)$")
_CLANG = re.compile(r"clang\+\+|clang")
_GNU = re.compile(r"g\+\+|gcc")

Run = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]


class CollectError(Exception):
    """Coverage data could not be turned into gcov reports."""


def run_tool(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one tool and capture its output; a tool that cannot start or does
    not finish raises ``CollectError``."""
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollectError(f"{shlex.join(command)} did not run: {error}") from error


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _meson_identity(build: Path) -> tuple[str, str]:
    text = _read(build / "meson-info" / "intro-compilers.json")
    try:
        compilers = json.loads(text or "{}").get("host", {})
    except (ValueError, AttributeError):
        compilers = {}  # unparseable introspection data names no compiler
    for compiler in compilers.values():
        command = compiler.get("exelist") or [""]
        return str(compiler.get("id", "")), str(command[0])
    return "", ""


def compiler_identity(build: Path) -> tuple[str, str]:
    """(compiler id, compiler path) as CMake or Meson recorded them in the
    build directory; empty strings when neither did."""
    for path in sorted(build.glob("CMakeFiles/*/CMake*Compiler.cmake")):
        text = _read(path)
        found_id, found_path = _CMAKE_ID.search(text), _CMAKE_PATH.search(text)
        if found_id:
            return found_id.group(1), found_path.group(1) if found_path else ""
    return _meson_identity(build)


def _versioned(compiler: str, pattern: re.Pattern[str], tool: str) -> list[str]:
    """``tool`` named like the compiler (``gcc-13`` gives ``gcov-13``): beside
    the compiler first, then on PATH. Empty for unversioned names."""
    path = Path(compiler)
    name = pattern.sub(tool, path.name, count=1)
    if not compiler or name == path.name:
        return []
    if path.parent == Path():
        return [name]
    return [str(path.with_name(name)), name]


def _first_on_path(names: Sequence[str], which: Which) -> str | None:
    for name in names:
        found = which(name)
        if found:
            return found
    return None


def _llvm_gcov(compiler: str, which: Which, run: Run, build: Path) -> list[str]:
    names = [*_versioned(compiler, _CLANG, "llvm-cov"), "llvm-cov"]
    llvm_cov = _first_on_path(names, which)
    if llvm_cov:
        return [llvm_cov, "gcov"]
    gcov = which("gcov")
    if gcov and "LLVM" in run([gcov, "--version"], build).stdout:
        return [gcov]
    xcrun = which("xcrun")
    if xcrun and run([xcrun, "--find", "llvm-cov"], build).returncode == 0:
        return [xcrun, "llvm-cov", "gcov"]
    raise CollectError("llvm-cov is not on PATH")


def gcov_command(
    compiler_id: str, compiler: str, build: Path, which: Which, run: Run
) -> list[str]:
    """The gcov that reads this compiler's data: ``llvm-cov gcov`` for Clang,
    else GCC's gcov (the one named like the compiler when there is one)."""
    if "clang" in compiler_id.lower():
        return _llvm_gcov(compiler, which, run, build)
    gcov = _first_on_path([*_versioned(compiler, _GNU, "gcov"), "gcov"], which)
    if gcov is None:
        raise CollectError("gcov is not on PATH")
    return [gcov]


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def kept_records(text: str, build: Path, root: Path) -> str:
    """The records of files under ``root`` and outside ``build``, with each
    ``Source:`` made absolute (relative ones are relative to ``build``)."""
    root_path, build_path = Path(os.path.realpath(root)), Path(os.path.realpath(build))
    kept: list[str] = []
    keep = False
    for row in text.splitlines():
        header = _SOURCE_HEADER.match(row)
        if header:
            source = Path(os.path.realpath(build / header.group(2).strip()))
            keep = _inside(source, root_path) and not _inside(source, build_path)
            row = f"{header.group(1)}{source}"
        if keep:
            kept.append(row)
    return "\n".join(kept) + "\n" if kept else ""


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (result.stderr or result.stdout).splitlines() if line]
    return lines[-1] if lines else f"exit {result.returncode}"


def collect(
    build: Path, root: Path, report: Path, gcov: Sequence[str], run: Run
) -> int:
    """Write one report per ``.gcda`` file that names a file under ``root``;
    the number written."""
    data = sorted(build.rglob("*.gcda"))
    if not data:
        raise CollectError(f"no .gcda coverage data was written under {build}")
    report.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, gcda in enumerate(data):
        result = run([*gcov, *GCOV_OPTIONS, str(gcda)], build)
        if result.returncode != 0:
            raise CollectError(f"{shlex.join(gcov)} failed: {_last_line(result)}")
        text = kept_records(result.stdout, build, root)
        if text:
            (report / f"{index:05d}.gcov").write_text(text, encoding="utf-8")
            written += 1
    if not written:
        raise CollectError(f"the coverage data names no file under {root}")
    return written


def main(
    argv: Sequence[str] | None = None,
    run: Run = run_tool,
    which: Which = shutil.which,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print("usage: gate_coverage_gcov_collect.py BUILD ROOT REPORT")
        return 2
    build, root, report = (Path(argument) for argument in arguments)
    try:
        compiler_id, compiler = compiler_identity(build)
        gcov = gcov_command(compiler_id, compiler, build, which, run)
        count = collect(build, root, report, gcov, run)
    except CollectError as error:
        print(error)
        return 1
    print(f"wrote {count} gcov report(s) to {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
