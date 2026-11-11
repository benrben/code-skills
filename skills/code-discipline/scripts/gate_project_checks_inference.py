"""Find the lint, type, format, and build checks a repository declares.

Only what the repository already has counts: package.json scripts, Makefile
targets, ruff and mypy configured and installed in the project environment,
and the Go, Rust, .NET, and Swift toolchains' own checks when that toolchain
is installed. Nothing is ever installed. Configured commands in
``.quality/quality-gate.json`` replace inference.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Optional

CONFIG_SOURCE = ".quality/quality-gate.json"

# package.json scripts that are checks, in the order they run.
PACKAGE_CHECK_SCRIPTS = (
    "lint",
    "typecheck",
    "type-check",
    "types",
    "check-types",
    "tsc",
    "format:check",
    "check-format",
    "fmt:check",
    "prettier:check",
    "check",
    "build",
)
TYPE_CHECK_SCRIPTS = frozenset(
    {"typecheck", "type-check", "types", "check-types", "tsc"}
)
MAKE_TARGET = re.compile(r"^(lint|typecheck|check):", re.MULTILINE)
MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
PROJECT_ENV_DIRS = (".venv", "venv")
DOTNET_SOLUTION_SUFFIXES = (".sln", ".slnx")
DOTNET_PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj")
# gofmt takes files in batches so a large module stays under the OS limit
# on command-line length.
GOFMT_FILES_PER_COMMAND = 400
# Listing tools that exit 0 even when they list files needing work.
LISTING_FORMATTERS = frozenset({"gofmt", "goimports", "gofumpt"})
DISPLAYED_ARGUMENTS = 8

Which = Callable[[str], Optional[str]]


@dataclasses.dataclass(frozen=True)
class CheckSpec:
    """One check command and the file that declares it.

    ``source`` is the repo-relative file that made us run the command, so a
    failure points at where the check is defined. ``fail_on_output`` marks
    listing tools such as ``gofmt -l`` that exit 0 even when they find work.
    """

    name: str
    command: tuple[str, ...]
    source: str
    fail_on_output: bool = False

    def display(self) -> str:
        """The command as shell text, shortened when it lists many files."""
        if len(self.command) <= DISPLAYED_ARGUMENTS + 1:
            return shlex.join(self.command)
        hidden = len(self.command) - DISPLAYED_ARGUMENTS - 1
        shown = shlex.join(self.command[: DISPLAYED_ARGUMENTS + 1])
        return f"{shown} ... (+{hidden} more)"


# ---------------------------------------------------------------------------
# Reading project files


def read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_or_empty(path))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


_JSONC_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)
_JSONC_TRAILING_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,(?=\s*[}\]])')


def load_jsonc(text: str) -> dict[str, Any]:
    """Parse tsconfig-style JSON, which allows comments and trailing commas."""

    def keep_strings(match: re.Match[str]) -> str:
        return match.group(1) or ""

    cleaned = _JSONC_COMMENT.sub(keep_strings, text)
    cleaned = _JSONC_TRAILING_COMMA.sub(keep_strings, cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def package_scripts(root: Path) -> dict[str, str]:
    scripts = _read_json_object(root / "package.json").get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(text) for name, text in scripts.items()}


def package_manager(root: Path) -> str:
    """The package manager the lockfile says this repository uses."""
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _has_dependency(root: Path, name: str) -> bool:
    package = _read_json_object(root / "package.json")
    for section in ("dependencies", "devDependencies"):
        values = package.get(section)
        if isinstance(values, dict) and name in values:
            return True
    return False


def _section_text(text: str, header: re.Pattern[str]) -> str | None:
    """The body of an INI/TOML section up to the next ``[`` header, or None."""
    match = header.search(text)
    if match is None:
        return None
    rest = text[match.end() :]
    following = re.search(r"^\s*\[", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


_TOOL_RUFF = re.compile(r"^\s*\[tool\.ruff[\].]", re.MULTILINE)
_TOOL_MYPY = re.compile(r"^\s*\[tool\.mypy\]", re.MULTILINE)
_INI_MYPY = re.compile(r"^\s*\[mypy\]", re.MULTILINE)
_FILES_SETTING = re.compile(r"^\s*files\s*=", re.MULTILINE)


def _ruff_config(root: Path) -> str | None:
    for name in ("ruff.toml", ".ruff.toml"):
        if (root / name).is_file():
            return name
    if _TOOL_RUFF.search(read_or_empty(root / "pyproject.toml")):
        return "pyproject.toml"
    return None


def _mypy_config(root: Path) -> tuple[str, bool] | None:
    """Where mypy is configured and whether that config lists ``files``."""
    candidates = (
        ("mypy.ini", _INI_MYPY),
        (".mypy.ini", _INI_MYPY),
        ("pyproject.toml", _TOOL_MYPY),
        ("setup.cfg", _INI_MYPY),
    )
    for name, header in candidates:
        body = _section_text(read_or_empty(root / name), header)
        if body is not None:
            return name, bool(_FILES_SETTING.search(body))
    return None


def _project_env_executable(root: Path, name: str) -> str | None:
    for env_dir in PROJECT_ENV_DIRS:
        for candidate in (
            root / env_dir / "bin" / name,
            root / env_dir / "Scripts" / f"{name}.exe",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Inference, one function per ecosystem


def package_script_checks(root: Path) -> list[CheckSpec]:
    scripts = package_scripts(root)
    manager = package_manager(root)
    return [
        CheckSpec(f"{manager} run {name}", (manager, "run", name), "package.json")
        for name in PACKAGE_CHECK_SCRIPTS
        if name in scripts
    ]


def _tsconfig_projects(root: Path) -> list[str]:
    """The tsconfig files to check; a solution config expands to its references."""
    config = load_jsonc(read_or_empty(root / "tsconfig.json"))
    references = config.get("references")
    if config.get("files") != [] or not isinstance(references, list):
        return ["tsconfig.json"]
    return [
        str(reference["path"])
        for reference in references
        if isinstance(reference, dict) and isinstance(reference.get("path"), str)
    ]


def typescript_checks(root: Path) -> list[CheckSpec]:
    """``tsc --noEmit`` when TypeScript is a dependency but no script runs it."""
    if TYPE_CHECK_SCRIPTS & set(package_scripts(root)):
        return []
    tsc = root / "node_modules" / ".bin" / "tsc"
    if not _has_dependency(root, "typescript") or not tsc.is_file():
        return []
    if not (root / "tsconfig.json").is_file():
        return []
    return [
        CheckSpec(
            f"tsc -p {project}", (str(tsc), "--noEmit", "-p", project), "tsconfig.json"
        )
        for project in _tsconfig_projects(root)
    ]


def python_checks(root: Path, which: Which) -> list[CheckSpec]:
    checks: list[CheckSpec] = []
    ruff_source = _ruff_config(root)
    ruff = _project_env_executable(root, "ruff") or which("ruff")
    if ruff_source and ruff:
        checks.append(CheckSpec("ruff check", (ruff, "check", "."), ruff_source))
        checks.append(
            CheckSpec("ruff format", (ruff, "format", "--check", "."), ruff_source)
        )
    mypy_config = _mypy_config(root)
    mypy = _project_env_executable(root, "mypy")
    if mypy_config and mypy:
        source, lists_files = mypy_config
        command = (mypy,) if lists_files else (mypy, ".")
        checks.append(CheckSpec("mypy", command, source))
    return checks


def _ignored_by_go(name: str) -> bool:
    return name in ("vendor", "testdata") or name.startswith((".", "_"))


def go_source_files(root: Path) -> list[str]:
    """The .go files ``go vet ./...`` reads.

    Like the go tool, skip ``vendor`` and ``testdata`` folders, names that
    start with ``.`` or ``_``, and nested modules. ``gofmt -l .`` would walk
    all of them and fail on vendored code or deliberately broken test data.
    """
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(
            name
            for name in dirs
            if not _ignored_by_go(name) and not (here / name / "go.mod").is_file()
        )
        found.extend(
            (here / name).relative_to(root).as_posix()
            for name in sorted(files)
            if name.endswith(".go") and not _ignored_by_go(name)
        )
    return found


def _gofmt_checks(root: Path, gofmt: str) -> list[CheckSpec]:
    files = go_source_files(root)
    size = GOFMT_FILES_PER_COMMAND
    batches = [files[start : start + size] for start in range(0, len(files), size)]
    return [
        CheckSpec(
            "gofmt" if len(batches) == 1 else f"gofmt ({number} of {len(batches)})",
            (gofmt, "-l", *batch),
            "go.mod",
            True,
        )
        for number, batch in enumerate(batches, start=1)
    ]


def go_checks(root: Path, which: Which) -> list[CheckSpec]:
    go = which("go")
    if not (root / "go.mod").is_file() or not go:
        return []
    checks = [CheckSpec("go vet", (go, "vet", "./..."), "go.mod")]
    gofmt = which("gofmt")
    if gofmt:
        checks.extend(_gofmt_checks(root, gofmt))
    return checks


def rust_checks(root: Path, which: Which) -> list[CheckSpec]:
    cargo = which("cargo")
    if not (root / "Cargo.toml").is_file() or not cargo:
        return []
    # --frozen refuses to create a missing Cargo.lock, and libraries often
    # do not commit one; --offline alone still never downloads.
    locked = ("--frozen",) if (root / "Cargo.lock").is_file() else ()
    command = (cargo, "check", "--all-targets", *locked, "--offline")
    checks = [CheckSpec("cargo check", command, "Cargo.toml")]
    if which("rustfmt"):
        checks.append(CheckSpec("cargo fmt", (cargo, "fmt", "--check"), "Cargo.toml"))
    return checks


def _dotnet_targets(root: Path) -> list[str]:
    """Solutions when present, else project files; ``dotnet build`` refuses a
    folder that holds more than one of either."""
    names = sorted(path.name for path in root.iterdir() if path.is_file())
    solutions = _with_suffix(names, DOTNET_SOLUTION_SUFFIXES)
    return solutions or _with_suffix(names, DOTNET_PROJECT_SUFFIXES)


def _with_suffix(names: Iterable[str], suffixes: tuple[str, ...]) -> list[str]:
    return [name for name in names if name.endswith(suffixes)]


def dotnet_checks(root: Path, which: Which) -> list[CheckSpec]:
    dotnet = which("dotnet")
    if not dotnet:
        return []
    return [
        CheckSpec(
            f"dotnet build {target}", (dotnet, "build", target, "--no-restore"), target
        )
        for target in _dotnet_targets(root)
    ]


def swift_checks(root: Path, which: Which) -> list[CheckSpec]:
    swift = which("swift")
    if not (root / "Package.swift").is_file() or not swift:
        return []
    return [CheckSpec("swift build", (swift, "build"), "Package.swift")]


def find_makefile(directory: Path) -> Path | None:
    """The Makefile make would read, under its real name, which matters on
    case-insensitive file systems where ``makefile`` also opens ``Makefile``."""
    try:
        present = {path.name for path in directory.iterdir() if path.is_file()}
    except OSError:
        # A folder we cannot list has no Makefile that make could run either.
        return None
    for name in MAKEFILE_NAMES:
        if name in present:
            return directory / name
    return None


def make_checks(root: Path, which: Which) -> list[CheckSpec]:
    makefile = find_makefile(root)
    if makefile is None:
        return []
    make = which("make") or "make"
    targets = dict.fromkeys(MAKE_TARGET.findall(read_or_empty(makefile)))
    return [
        CheckSpec(f"make {target}", (make, target), makefile.name) for target in targets
    ]


def _unique(specs: Iterable[CheckSpec]) -> list[CheckSpec]:
    seen: set[tuple[str, ...]] = set()
    result: list[CheckSpec] = []
    for spec in specs:
        if spec.command not in seen:
            seen.add(spec.command)
            result.append(spec)
    return result


def infer_project_checks(root: Path, *, which: Which = shutil.which) -> list[CheckSpec]:
    """Every check the repository declares, without installing anything.

    ``which`` finds toolchain executables on PATH; tests pass a fake.
    """
    return _unique(
        [
            *package_script_checks(root),
            *typescript_checks(root),
            *python_checks(root, which),
            *go_checks(root, which),
            *rust_checks(root, which),
            *dotnet_checks(root, which),
            *swift_checks(root, which),
            *make_checks(root, which),
        ]
    )


# ---------------------------------------------------------------------------
# Configured commands


def _command_tuple(value: Any, root: Path) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = shlex.split(value)
    elif isinstance(value, list) and all(
        isinstance(part, (str, int, float)) for part in value
    ):
        parts = [str(part) for part in value]
    else:
        raise ValueError(f"project_checks command must be text or a list: {value!r}")
    if not parts:
        raise ValueError("project_checks command must not be empty")
    return tuple(part.replace("{root}", str(root)) for part in parts)


AMBIGUOUS_COMMAND_LIST = (
    "project_checks.commands lists bare strings, which could be one argument "
    "list or several commands. Write each command as an argument list such as "
    '["npm", "run", "lint"] or as {"command": "npm run lint"}.'
)


def _lists_files(command: tuple[str, ...]) -> bool:
    """``gofmt -l`` exits 0 even when it lists unformatted files."""
    name = command[0].replace("\\", "/").rsplit("/", 1)[-1]
    return name in LISTING_FORMATTERS and "-l" in command[1:]


def _configured_spec(value: Any, root: Path) -> CheckSpec:
    if isinstance(value, dict):
        command = _command_tuple(value.get("command"), root)
        name = str(value.get("name") or shlex.join(command))
        fail_on_output = bool(value.get("fail_on_output", _lists_files(command)))
        return CheckSpec(name, command, CONFIG_SOURCE, fail_on_output)
    if isinstance(value, str):
        raise ValueError(AMBIGUOUS_COMMAND_LIST)
    return _command_spec(_command_tuple(value, root))


def _command_spec(command: tuple[str, ...]) -> CheckSpec:
    return CheckSpec(shlex.join(command), command, CONFIG_SOURCE, _lists_files(command))


def configured_project_checks(
    section: Mapping[str, Any], root: Path
) -> list[CheckSpec] | None:
    """Commands from the ``project_checks.commands`` config, or None when the
    project leaves the checks to inference.

    The value is one command as text, or a list whose entries are argument
    lists or ``{"name", "command", "fail_on_output"}`` objects (``command``
    is text or an argument list). A list of bare strings is refused: it
    could mean one argument list or several commands.
    """
    value = section.get("commands")
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, str):
        return [_command_spec(_command_tuple(value, root))]
    if not isinstance(value, list):
        raise ValueError("project_checks.commands must be a command or a list")
    return _unique(_configured_spec(entry, root) for entry in value)


def project_check_specs(
    root: Path, section: Mapping[str, Any], *, which: Which = shutil.which
) -> list[CheckSpec]:
    """Configured commands replace inference entirely."""
    configured = configured_project_checks(section, root)
    if configured is not None:
        return configured
    return infer_project_checks(root, which=which)


# Before the generic gates, lint/format and type commands lived in separate
# sections. Their commands are the project's declared checks, so they carry
# over in this order when ``project_checks.commands`` is not set.
LEGACY_COMMAND_SECTIONS = ("format_lint", "types")


def project_checks_section(config: Mapping[str, Any]) -> dict[str, Any]:
    """The ``project_checks`` section, filled from legacy sections when the
    project declared its lint and type commands there."""
    section = dict(config.get("project_checks") or {})
    if section.get("commands"):
        return section
    legacy: list[Any] = []
    for name in LEGACY_COMMAND_SECTIONS:
        old = config.get(name)
        if not isinstance(old, Mapping) or old.get("enabled") is False:
            continue
        commands = old.get("commands")
        if isinstance(commands, list):
            legacy.extend(commands)
    if legacy:
        section["commands"] = legacy
    return section
