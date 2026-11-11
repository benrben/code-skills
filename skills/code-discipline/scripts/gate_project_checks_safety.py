"""Decide whether a declared check can fail and leaves files alone.

A check that rewrites files (``eslint --fix``, ``black src``) or whose
failure cannot reach the exit status (``eslint . || true``) would let the
gate pass without proving anything, so the gate refuses to run it.

The inspection follows what the command really runs, whatever declared it:
``npm run lint`` reads the ``prelint``, ``lint``, and ``postlint`` scripts and
every script they call (also through ``npm-run-all``, ``run-s``, ``run-p``,
``concurrently``, ``turbo``, and ``lerna``), and ``make lint`` reads the
target's recipe and its prerequisites. Shell text is judged by
``gate_project_checks_shell``.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from pathlib import Path

from gate_project_checks_inference import (
    CheckSpec,
    find_makefile,
    package_scripts,
    read_or_empty,
)
from gate_project_checks_make import Makefile, parse_makefile
from gate_project_checks_shell import basename, command_words, exit_status_problem
from source_files import walk_files

PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
# Programs that run the command named after their own options.
_PREFIX_COMMANDS = frozenset(
    {"npx", "pnpx", "bunx", "uvx", "env", "time", "nice", "nohup", "exec"}
    | {"command", "cross-env", "xargs"}
)
_RUNNER_SUBCOMMANDS = frozenset(
    {
        ("npm", "exec"),
        ("pnpm", "exec"),
        ("pnpm", "dlx"),
        ("yarn", "exec"),
        ("yarn", "dlx"),
        ("bun", "x"),
        ("uv", "run"),
        ("poetry", "run"),
        ("pdm", "run"),
        ("pipenv", "run"),
        ("hatch", "run"),
        ("rye", "run"),
        ("pipx", "run"),
        ("bundle", "exec"),
    }
)
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)?)?")
_FIX_OR_WRITE = re.compile(r"--(?:fix(?:-[\w-]+)?|write)(?:=.*)?", re.DOTALL)
_READ_ONLY_FIX_FLAGS = frozenset({"--fix-type"})
_MESSAGE_COMMANDS = frozenset({"echo", "printf"})


class UnsafeCheck(Exception):
    """The first reason a check cannot prove anything; ends the inspection."""


# ---------------------------------------------------------------------------
# Tools that write files


@dataclasses.dataclass(frozen=True)
class _Writer:
    """A tool that rewrites files when given ``writes_with``, or, when
    ``unless`` is set, every time none of the ``unless`` flags is given."""

    tool: str
    subcommand: str | None = None
    writes_with: frozenset[str] = frozenset()
    unless: frozenset[str] | None = None

    def problem(self, args: Sequence[str]) -> str | None:
        label = self.tool
        if self.subcommand is not None:
            if args[:1] != [self.subcommand]:
                return None
            args, label = args[1:], f"{self.tool} {self.subcommand}"
        flag = _first_flag(args, self.writes_with)
        if flag is not None:
            return f"{label} {flag} modifies files"
        if self.unless is not None and _first_flag(args, self.unless) is None:
            return f"{label} rewrites files"
        return None


def _first_flag(args: Sequence[str], flags: frozenset[str]) -> str | None:
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in flags:
            return name
    return None


def _flags(*names: str) -> frozenset[str]:
    return frozenset(names)


_IN_PLACE = _flags("-i", "--in-place")
_WRITERS = (
    _Writer("prettier", writes_with=_flags("-w")),
    _Writer("gofmt", writes_with=_flags("-w")),
    _Writer("goimports", writes_with=_flags("-w")),
    _Writer("gofumpt", writes_with=_flags("-w")),
    _Writer("shfmt", writes_with=_flags("-w")),
    _Writer("clang-format", writes_with=_flags("-i")),
    _Writer("autopep8", writes_with=_IN_PLACE),
    _Writer("yapf", writes_with=_IN_PLACE),
    _Writer("swift-format", writes_with=_IN_PLACE),
    _Writer("sed", writes_with=_IN_PLACE),
    _Writer(
        "rubocop",
        writes_with=_flags("-a", "-A", "-x", "--autocorrect", "--autocorrect-all")
        | _flags("--auto-correct", "--auto-correct-all", "--fix-layout"),
    ),
    _Writer("ktlint", writes_with=_flags("-F", "--format")),
    _Writer("clang-tidy", writes_with=_flags("-fix", "-fix-errors", "--fix-errors")),
    _Writer("biome", writes_with=_flags("--apply", "--apply-unsafe")),
    _Writer("black", unless=_flags("--check", "--diff")),
    _Writer("isort", unless=_flags("--check", "--check-only", "-c", "--diff")),
    _Writer("ruff", "format", unless=_flags("--check", "--diff")),
    _Writer("cargo", "fmt", unless=_flags("--check")),
    _Writer("go", "fmt", unless=frozenset()),
    _Writer("rustfmt", unless=_flags("--check")),
    _Writer("dprint", "fmt", unless=frozenset()),
    _Writer("php-cs-fixer", "fix", unless=_flags("--dry-run")),
    _Writer("phpcbf", unless=frozenset()),
    _Writer("sqlfluff", "fix", unless=frozenset()),
    _Writer("swiftformat", unless=_flags("--lint", "--dryrun")),
    _Writer("terraform", "fmt", unless=_flags("-check", "--check")),
    _Writer("deno", "fmt", unless=_flags("--check")),
    _Writer("mix", "format", unless=_flags("--check-formatted")),
    _Writer("dotnet", "format", unless=_flags("--verify-no-changes")),
    _Writer("stylua", unless=_flags("--check")),
)
_WRITERS_BY_TOOL: dict[str, list[_Writer]] = {}
for _writer in _WRITERS:
    _WRITERS_BY_TOOL.setdefault(_writer.tool, []).append(_writer)


def _skip_options(words: Sequence[str]) -> list[str]:
    index = 0
    while index < len(words) and (words[index].startswith("-") or "=" in words[index]):
        index += 1
    return list(words[index:])


def tool_words(words: Sequence[str]) -> list[str]:
    """The words from the program that does the work, past wrappers such as
    ``npx``, ``uv run``, and ``python -m``."""
    rest = command_words(words)
    while rest:
        name, following = basename(rest[0]), rest[1:2]
        if name in _PREFIX_COMMANDS:
            rest = _skip_options(rest[1:])
        elif following and (name, following[0]) in _RUNNER_SUBCOMMANDS:
            rest = _skip_options(rest[2:])
        elif _PYTHON.fullmatch(name) and following == ["-m"]:
            rest = rest[2:]
        else:
            break
    return rest


def flag_problem(args: Sequence[str]) -> str | None:
    """``--fix``-style and ``--write`` flags, whatever tool receives them."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        if _FIX_OR_WRITE.fullmatch(arg) and flag not in _READ_ONLY_FIX_FLAGS:
            if flag == "--fix-dry-run":
                return "--fix-dry-run hides fixable problems"
            return f"{flag} modifies files"
    return None


def writer_problem(tool: Sequence[str]) -> str | None:
    """Why this program, run with these arguments, modifies files."""
    name, args = basename(tool[0]), list(tool[1:])
    if name in _MESSAGE_COMMANDS:
        return None
    for writer in _WRITERS_BY_TOOL.get(name, ()):
        problem = writer.problem(args)
        if problem:
            return problem
    return flag_problem(args)


def _shell_script(tool: Sequence[str]) -> tuple[str, bool] | None:
    """The text of ``sh -c '<text>'`` and whether ``-e`` applies to it."""
    if basename(tool[0]) not in _SHELLS:
        return None
    errexit = False
    for index, arg in enumerate(tool[1:], start=1):
        if not arg.startswith("-") or arg.startswith("--"):
            return None
        errexit |= "e" in arg
        if "c" in arg:
            return (tool[index + 1], errexit) if index + 1 < len(tool) else None
    return None


# ---------------------------------------------------------------------------
# Which package scripts and make targets a command runs


_RUN_WORDS = frozenset({"run", "run-script", "rum", "urn"})
_NPM_LIFECYCLE = {"test": "test", "t": "test", "tst": "test", "start": "start"}
_VALUE_OPTIONS = frozenset(
    {"-w", "--workspace", "--prefix", "-C", "--dir", "--filter", "-F", "--cwd"}
)
_WORKSPACE_OPTIONS = _VALUE_OPTIONS | {"-r", "--recursive", "-ws", "--workspaces"}
_SCRIPT_RUNNERS = frozenset({"npm-run-all", "npm-run-all2", "run-s", "run-p"})
_WORKSPACE_TASK_RUNNERS = frozenset({"turbo", "lerna"})
_CONCURRENT_SCRIPT = re.compile(r"(?:npm|pnpm|yarn|bun):(\S+)\s*(.*)", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class ScriptCall:
    """Scripts a command runs: names (or ``*`` patterns), whether they come
    from every workspace package, and the arguments passed on to them."""

    patterns: tuple[str, ...]
    workspaces: bool = False
    passed_on: tuple[str, ...] = ()


def _skip_package_options(words: Sequence[str], start: int) -> tuple[int, bool]:
    index, workspaces = start, False
    while index < len(words):
        word = words[index]
        if word in ("workspace", "workspaces", "foreach"):
            workspaces = True
            index += 2 if word == "workspace" else 1
            continue
        if not word.startswith("-"):
            break
        flag = word.split("=", 1)[0]
        workspaces = workspaces or flag in _WORKSPACE_OPTIONS
        index += 2 if flag in _VALUE_OPTIONS and "=" not in word else 1
    return index, workspaces


def package_manager_call(tool: Sequence[str]) -> ScriptCall | None:
    """``npm run -s lint``, ``pnpm -r lint``, ``yarn lint``, ``npm test``."""
    manager = basename(tool[0])
    index, workspaces = _skip_package_options(tool, 1)
    if index >= len(tool):
        return None
    word = tool[index]
    if word in _RUN_WORDS:
        index, more = _skip_package_options(tool, index + 1)
        if index >= len(tool):
            return None
        name, workspaces = tool[index], workspaces or more
    elif word in _NPM_LIFECYCLE:
        name = _NPM_LIFECYCLE[word]
    elif manager != "npm":
        name = word  # yarn, pnpm, and bun run scripts by bare name
    else:
        return None
    return ScriptCall((name,), workspaces, tuple(tool[index + 1 :]))


def script_runner_call(tool: Sequence[str]) -> ScriptCall:
    """``npm-run-all lint:*`` / ``run-s "lint -- --max-warnings 0"``."""
    patterns: list[str] = []
    passed_on: list[str] = []
    for arg in tool[1:]:
        if not arg.startswith("-") and arg.split():
            first, *rest = arg.split()
            patterns.append(first)
            passed_on.extend(rest)
    return ScriptCall(tuple(patterns), False, tuple(passed_on))


def workspace_task_call(tool: Sequence[str]) -> ScriptCall:
    """``turbo run lint types`` or ``lerna run lint``: every package's script."""
    args = list(tool[1:])
    if args[:1] == ["run"]:
        args = args[1:]
    patterns = tuple(arg for arg in args if not arg.startswith("-"))
    return ScriptCall(patterns, True)


def matching_scripts(pattern: str, scripts: dict[str, str]) -> list[str]:
    """Script names a pattern selects; ``*`` stops at ``:``, ``**`` does not."""
    if "*" not in pattern:
        return [pattern] if pattern in scripts else []
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^:]*")
    return [name for name in scripts if re.fullmatch(regex, name)]


@dataclasses.dataclass
class MakeCall:
    directory: str | None = None
    file: str | None = None
    targets: list[str] = dataclasses.field(default_factory=list)
    ignores_errors: bool = False


_MAKE_VALUE_OPTIONS = {
    "-C": "directory",
    "--directory": "directory",
    "-f": "file",
    "--file": "file",
    "--makefile": "file",
}


def make_call(args: Sequence[str]) -> MakeCall:
    """Targets, ``-C``/``-f``, and ``-i`` from a ``make`` command line."""
    call = MakeCall()
    pending: str | None = None
    for arg in args:
        if pending is not None:
            setattr(call, pending, arg)
            pending = None
        elif not arg.startswith("-"):
            if "=" not in arg:
                call.targets.append(arg)
        else:
            pending = _make_option(call, arg)
    return call


def _make_option(call: MakeCall, arg: str) -> str | None:
    """Record one option; return the field its next word fills, if any."""
    for option, field in _MAKE_VALUE_OPTIONS.items():
        if arg == option:
            return field
        if arg.startswith(f"{option}="):
            setattr(call, field, arg[len(option) + 1 :])
            return None
        if len(option) == 2 and arg.startswith(option):
            setattr(call, field, arg[2:])
            return None
    short_flags = not arg.startswith("--") and "i" in arg[1:]
    call.ignores_errors |= arg == "--ignore-errors" or short_flags
    return None


# ---------------------------------------------------------------------------
# Following a check through the scripts and recipes it runs


def _located(problem: str, where: str) -> str:
    return f"{problem} in {where}" if where else problem


class _Inspector:
    """Walks one check: its command, then every script or recipe it runs.

    ``root`` is None when only the given text is to be judged, without
    reading package.json or a Makefile.
    """

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.seen: set[tuple[str, str, str]] = set()
        self.makefiles: dict[Path, Makefile] = {}
        self.scripts: dict[Path, dict[str, str]] = {}
        self.workspace_dirs: list[Path] | None = None

    def text(self, text: str, cwd: Path, where: str, errexit: bool = False) -> None:
        def on_command(words: list[str]) -> None:
            self.command(words, cwd, where)

        problem = exit_status_problem(text, on_command, errexit=errexit)
        if problem:
            raise UnsafeCheck(_located(problem, where))

    def command(self, words: Sequence[str], cwd: Path, where: str) -> None:
        tool = tool_words(words)
        if not tool:
            return
        script = _shell_script(tool)
        if script is not None:
            self.text(script[0], cwd, where, errexit=script[1])
            return
        problem = writer_problem(tool)
        if problem:
            raise UnsafeCheck(_located(problem, where))
        if self.root is not None:
            self._follow(tool, cwd, where)

    def _follow(self, tool: list[str], cwd: Path, where: str) -> None:
        name = basename(tool[0])
        if name in PACKAGE_MANAGERS:
            self._scripts(package_manager_call(tool), cwd, where)
        elif name in _SCRIPT_RUNNERS:
            self._scripts(script_runner_call(tool), cwd, where)
        elif name in _WORKSPACE_TASK_RUNNERS:
            self._scripts(workspace_task_call(tool), cwd, where)
        elif name == "concurrently":
            self._concurrently(tool, cwd, where)
        elif name in ("make", "gmake"):
            self._make(make_call(tool[1:]), cwd, where)

    # -- package.json scripts

    def _package_dirs(self, cwd: Path, workspaces: bool) -> list[Path]:
        if not workspaces or self.root is None:
            return [cwd]
        if self.workspace_dirs is None:
            self.workspace_dirs = sorted(
                path.parent
                for path in walk_files(self.root)
                if path.name == "package.json"
            )
        return self.workspace_dirs

    def _package_scripts(self, directory: Path) -> dict[str, str]:
        if directory not in self.scripts:
            self.scripts[directory] = package_scripts(directory)
        return self.scripts[directory]

    def _scripts(self, call: ScriptCall | None, cwd: Path, where: str) -> None:
        if call is None:
            return
        problem = flag_problem(call.passed_on)
        if problem:
            raise UnsafeCheck(_located(problem, where))
        for directory in self._package_dirs(cwd, call.workspaces):
            scripts = self._package_scripts(directory)
            for pattern in call.patterns:
                for name in matching_scripts(pattern, scripts):
                    self._script(directory, scripts, name)

    def _script(self, directory: Path, scripts: dict[str, str], name: str) -> None:
        """npm also runs ``pre<name>`` before and ``post<name>`` after."""
        for hook in (f"pre{name}", name, f"post{name}"):
            key = ("script", str(directory), hook)
            if hook in scripts and key not in self.seen:
                self.seen.add(key)
                where = f"{self._relative(directory / 'package.json')} script {hook!r}"
                self.text(scripts[hook], directory, where)

    def _concurrently(self, tool: list[str], cwd: Path, where: str) -> None:
        """Each argument is a command, or ``npm:<script>``."""
        for arg in tool[1:]:
            match = _CONCURRENT_SCRIPT.fullmatch(arg)
            if match:
                passed_on = tuple(match.group(2).split())
                self._scripts(
                    ScriptCall((match.group(1),), False, passed_on), cwd, where
                )
            elif not arg.startswith("-"):
                self.text(arg, cwd, where)

    # -- Makefile targets

    def _make(self, call: MakeCall, cwd: Path, where: str) -> None:
        if call.ignores_errors:
            raise UnsafeCheck(_located("make -i ignores failures", where))
        directory = cwd / call.directory if call.directory else cwd
        path = directory / call.file if call.file else find_makefile(directory)
        if path is None or not path.is_file():
            return
        if path not in self.makefiles:
            self.makefiles[path] = parse_makefile(read_or_empty(path))
        makefile = self.makefiles[path]
        for target in call.targets or [makefile.default_goal or ""]:
            self._target(makefile, path, target)

    def _target(self, makefile: Makefile, path: Path, target: str) -> None:
        key = ("make", str(path), target)
        if target not in makefile.rules or key in self.seen:
            return
        self.seen.add(key)
        where = f"{self._relative(path)} target {target!r}"
        if makefile.ignores_errors(target):
            raise UnsafeCheck(f".IGNORE makes make ignore failures in {where}")
        for prerequisite in makefile.rules[target].prerequisites:
            self._target(makefile, path, prerequisite)
        for line in makefile.recipe_lines(target):
            if line.ignores_errors:
                command = line.text.strip()[:40]
                raise UnsafeCheck(
                    f"the - prefix makes make ignore the failure of '{command}' "
                    f"in {where}"
                )
            self.text(line.text, path.parent, where, errexit=makefile.errexit)

    def _relative(self, path: Path) -> str:
        if self.root is not None and path.is_relative_to(self.root):
            return path.relative_to(self.root).as_posix()
        return path.name


def unsafe_reason(spec: CheckSpec, root: Path) -> str | None:
    """Why running this check would prove nothing, or None when it is safe.

    The command is run without a shell, so only the scripts, recipes, and
    ``sh -c`` text it reaches are read as shell text.
    """
    try:
        _Inspector(root).command(spec.command, root, "")
    except UnsafeCheck as problem:
        return f"check cannot fail or modifies files ({problem})"
    return None


def command_problem(text: str, root: Path | None = None) -> str | None:
    """Why this shell text cannot fail or would modify files, or None.

    With ``root``, scripts and make targets the text calls are read too.
    """
    try:
        _Inspector(root).text(text, root or Path("."), "")
    except UnsafeCheck as problem:
        return str(problem)
    return None
