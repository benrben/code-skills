"""Find the Python interpreter behind a project's Python test command.

The Python coverage recipe runs the test command unchanged, but its finalize
step (combining data files and writing the JSON report) must run under the
project's own interpreter: only that one can parse the project's syntax.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

Command = tuple[str, ...]

PYTHON_PROGRAM = re.compile(r"^(python(\d+(\.\d+)?)?|py)$")
PYTEST_PROGRAMS = frozenset({"pytest", "py.test"})
# Project managers whose ``run`` starts a command in the project environment.
PYTHON_WRAPPERS = frozenset({"uv", "poetry", "pdm", "hatch", "rye"})

_VENV_PYTHONS = (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe")


def program_name(executable: str) -> str:
    """The executable's bare name: ``.venv/bin/pytest.exe`` gives ``pytest``."""
    name = Path(executable).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(suffix)
    return name


def is_python_wrapper(command: Command) -> bool:
    """``uv run ...``, ``poetry run ...`` and the like."""
    return program_name(command[0]) in PYTHON_WRAPPERS and command[1:2] == ("run",)


def is_python_command(command: Command) -> bool:
    program = program_name(command[0])
    return bool(
        PYTHON_PROGRAM.match(program)
        or program in PYTEST_PROGRAMS
        or is_python_wrapper(command)
    )


def _script_path(root: Path, executable: str) -> Path | None:
    """The file an executable names, found like the shell would."""
    if "/" in executable or "\\" in executable:
        path = Path(executable)
        return path if path.is_absolute() else root / path
    found = shutil.which(executable)
    return Path(found) if found else None


def _first_line(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            return handle.readline(512).decode("utf-8", errors="replace")
    except OSError:
        return ""


def shebang_python(root: Path, executable: str) -> str | None:
    """The Python interpreter in a script's ``#!`` line, if any: pipx and
    Homebrew install ``pytest`` with an interpreter of its own."""
    first = _first_line(_script_path(root, executable))
    words = first[2:].split() if first.startswith("#!") else []
    if words and Path(words[0]).name == "env":
        words = [shutil.which(words[1]) or ""] if len(words) > 1 else []
    if words and PYTHON_PROGRAM.match(program_name(words[0])):
        return words[0]
    return None


def _local_python(root: Path, executable: str) -> str | None:
    """The interpreter next to a script, or the project's virtual env."""
    folder = Path(executable).parent
    if folder != Path("."):
        for name in ("python", "python3", "python.exe"):
            if (root / folder / name).exists():
                return str(folder / name)
    for candidate in _VENV_PYTHONS:
        if (root / candidate).exists():
            return str(root / candidate)
    return None


def python_interpreter(root: Path, command: Command) -> Command | None:
    """How to start the project's interpreter: the command's own python, the
    project manager's ``run python``, the interpreter a test script names,
    the one next to it, the project's virtual env, or python3 on PATH."""
    if PYTHON_PROGRAM.match(program_name(command[0])):
        return (command[0],)
    if is_python_wrapper(command):
        return (*command[:2], "python")
    found = shebang_python(root, command[0]) or _local_python(root, command[0])
    found = found or shutil.which("python3") or shutil.which("python")
    return (found,) if found else None
