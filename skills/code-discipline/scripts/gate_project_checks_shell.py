"""Read shell text well enough to tell whether a failing check still fails.

npm scripts and Makefile recipes run through ``sh -c``. Text such as
``eslint . || true``, ``eslint . | tee lint.log``, or ``eslint .; echo done``
exits 0 even when eslint finds errors, so passing it proves nothing. This
module parses the parts of POSIX shell syntax that decide an exit status
(lists, ``&&``/``||``, pipes, background jobs, groups, ``set -e`` and
``pipefail``) and reports the first command whose failure cannot reach the
final status.

It is not a full shell parser. Constructs it does not model are read as plain
words, and commands whose status does not matter (``rm``, ``echo``, ``cd``,
``find`` feeding a pipe, ...) are allowed to fail quietly so that ordinary
housekeeping such as ``rm -rf dist || true`` is not reported.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Optional, Union

OnCommand = Callable[[list[str]], Optional[str]]

_TOKEN = re.compile(
    r"""
    (?P<space>[ \t\r]+|\\\n)
  | (?P<comment>\#[^\n]*)
  | (?P<operator>&&|\|\||;;|\|&|>>|<<|>&|<&|&>|>\||<>|[;&|()<>\n])
  | (?P<word>(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|\$\((?:[^()]|\([^()]*\))*\)
              |`[^`]*`|[^\s;&|()<>'"`\\$]|\$)+)
  | (?P<rest>[\s\S]+)
    """,
    re.VERBOSE,
)
_QUOTED = re.compile(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)""")
_SEPARATORS = frozenset({";", "&", "\n", ";;"})
_REDIRECTIONS = frozenset({">>", "<<", ">&", "<&", "&>", ">|", "<>", "<", ">"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
# Words that open or continue a compound command; the command follows them.
_PREFIX_WORDS = frozenset({"!", "if", "then", "else", "elif", "do", "while", "until"})
# Headers whose own words are not a command.
_HEADER_WORDS = frozenset({"for", "case", "select", "function"})
# A list followed by one of these is a branch or loop body, not a statement
# whose status a later command replaces.
_BRANCH_WORDS = frozenset({"then", "else", "elif", "fi", "esac", "do"})
_LOOP_END = "done"

# Commands whose failure does not weaken a check: housekeeping, messages, and
# data sources that feed a pipe.
QUIET_COMMANDS = frozenset(
    {
        ":",
        "[",
        "[[",
        "awk",
        "cat",
        "cd",
        "chmod",
        "cp",
        "cut",
        "del",
        "del-cli",
        "echo",
        "esac",
        "exit",
        "export",
        "false",
        "fi",
        "find",
        "git",
        "grep",
        "head",
        "jq",
        "ln",
        "ls",
        "mkdir",
        "mv",
        "popd",
        "printf",
        "pushd",
        "read",
        "rimraf",
        "rm",
        "rmdir",
        "sed",
        "set",
        "shx",
        "sleep",
        "sort",
        "tail",
        "test",
        "touch",
        "tr",
        "trash",
        "true",
        "uniq",
        "unset",
        "wc",
    }
)


@dataclasses.dataclass(frozen=True)
class Token:
    text: str
    operator: bool


@dataclasses.dataclass
class Command:
    words: list[str]


@dataclasses.dataclass
class Group:
    """A ``( ... )`` subshell or ``{ ...; }`` group."""

    body: list[AndOr]


Unit = Union[Command, Group]


@dataclasses.dataclass
class AndOr:
    """Pipelines joined by ``&&``/``||``, ended by ``;``, newline, or ``&``."""

    pipelines: list[list[Unit]]
    operators: list[str]
    terminator: str = ""


# ---------------------------------------------------------------------------
# Tokens and parsing


def _unquote(word: str) -> str:
    def inner(match: re.Match[str]) -> str:
        return next(group for group in match.groups() if group is not None)

    return _QUOTED.sub(inner, word)


def tokenize(text: str) -> list[Token]:
    """Words (quotes removed) and operators; comments and spaces dropped."""
    tokens: list[Token] = []
    for match in _TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "operator":
            tokens.append(Token(match.group(), True))
        elif kind in ("word", "rest"):
            tokens.append(Token(_unquote(match.group()), False))
    return tokens


class _Parser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens
        self.at = 0

    def peek(self) -> Token | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _next_is(self, *operators: str) -> bool:
        token = self.peek()
        return token is not None and token.operator and token.text in operators

    def sequence(self, closer: str | None) -> list[AndOr]:
        lists: list[AndOr] = []
        while (token := self.peek()) is not None:
            if _closes(token, closer):
                self.at += 1
                break
            start = self.at
            if token.operator and token.text in _SEPARATORS | {")"}:
                self.at += 1  # an empty statement or a stray ``)``
                continue
            lists.append(self.and_or(closer))
            self.at = max(self.at, start + 1)
        return lists

    def and_or(self, closer: str | None) -> AndOr:
        pipelines = [self.pipeline(closer)]
        operators: list[str] = []
        while self._next_is("&&", "||"):
            operators.append(self.tokens[self.at].text)
            self.at += 1
            self._skip_newlines()
            pipelines.append(self.pipeline(closer))
        result = AndOr(pipelines, operators)
        if self._next_is(*_SEPARATORS):
            result.terminator = self.tokens[self.at].text
            self.at += 1
        return result

    def pipeline(self, closer: str | None) -> list[Unit]:
        units = [self.unit(closer)]
        while self._next_is("|", "|&"):
            self.at += 1
            self._skip_newlines()
            units.append(self.unit(closer))
        return units

    def unit(self, closer: str | None) -> Unit:
        token = self.peek()
        opener = _group_opener(token)
        if opener:
            self.at += 1
            return Group(self.sequence(_GROUP_CLOSERS[opener]))
        return Command(self.words(closer))

    def words(self, closer: str | None) -> list[str]:
        words: list[str] = []
        while (token := self.peek()) is not None and not _ends_command(token, closer):
            self.at += 1
            if token.operator:
                self.at += 1  # a redirection and its target are not arguments
            else:
                words.append(token.text)
        return words

    def _skip_newlines(self) -> None:
        while self._next_is("\n"):
            self.at += 1


_GROUP_CLOSERS = {"(": ")", "{": "}"}


def _group_opener(token: Token | None) -> str:
    """``(`` is an operator; ``{`` is a reserved word."""
    if token is None:
        return ""
    if (token.operator and token.text == "(") or (
        not token.operator and token.text == "{"
    ):
        return token.text
    return ""


def _closes(token: Token, closer: str | None) -> bool:
    if closer == ")":
        return token.operator and token.text == ")"
    return closer == "}" and not token.operator and token.text == "}"


def _ends_command(token: Token, closer: str | None) -> bool:
    if token.operator:
        return token.text not in _REDIRECTIONS
    return closer == "}" and token.text == "}"


def parse(text: str) -> list[AndOr]:
    return _Parser(tokenize(text)).sequence(None)


# ---------------------------------------------------------------------------
# Naming commands


def basename(word: str) -> str:
    """The program name of a path such as ``./node_modules/.bin/eslint``."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def command_words(words: Sequence[str]) -> list[str]:
    """The words from the program name on: environment assignments and
    keywords such as ``if`` or ``!`` before it are dropped. A ``for`` or
    ``case`` header has no program, so it gives an empty list."""
    index = 0
    while index < len(words) and (
        words[index] in _PREFIX_WORDS or _ASSIGNMENT.fullmatch(words[index])
    ):
        index += 1
    if index < len(words) and words[index] in _HEADER_WORDS:
        return []
    return list(words[index:])


def _is_quiet(command: Command) -> bool:
    words = command_words(command.words)
    return not words or basename(words[0]) in QUIET_COMMANDS


def _units(lists: Iterable[AndOr]) -> Iterable[Unit]:
    for and_or in lists:
        for pipeline in and_or.pipelines:
            yield from pipeline


def _loud_command(units: Iterable[Unit]) -> Command | None:
    """The first command whose failure matters, looking inside groups."""
    for unit in units:
        if isinstance(unit, Group):
            found = _loud_command(_units(unit.body))
            if found is not None:
                return found
        elif not _is_quiet(unit):
            return unit
    return None


def _describe(unit: Unit) -> str:
    if isinstance(unit, Group):
        inner = list(_units(unit.body))
        return _describe(inner[0]) if inner else "( )"
    text = " ".join(command_words(unit.words)[:3]) or " ".join(unit.words)
    return text if len(text) <= 40 else text[:37] + "..."


def _fails(unit: Unit) -> bool:
    """Whether a ``||`` fallback ends with a failure, such as ``exit 1``."""
    if isinstance(unit, Group):
        return bool(unit.body) and _fails(unit.body[-1].pipelines[-1][-1])
    if not unit.words:
        return True  # ``a ||`` with nothing after it is a syntax error
    words = command_words(unit.words)
    if not words:
        return False
    if words[0] == "false":
        return True
    return words[0] in ("exit", "return") and words[1:2] != ["0"]


def _first_loud(pipelines: Iterable[Sequence[Unit]], reason: str) -> str | None:
    command = _loud_command(unit for pipeline in pipelines for unit in pipeline)
    if command is None:
        return None
    return f"'{_describe(command)}' cannot fail the check: {reason}"


# ---------------------------------------------------------------------------
# Exit status analysis


@dataclasses.dataclass
class _Options:
    errexit: bool = False
    pipefail: bool = False

    def note(self, and_or: AndOr) -> None:
        """Track ``set -e`` and ``set -o pipefail`` for later statements."""
        for pipeline in and_or.pipelines:
            for unit in pipeline:
                if isinstance(unit, Command):
                    self._note_set(command_words(unit.words))

    def _note_set(self, words: list[str]) -> None:
        if words[:1] != ["set"]:
            return
        for word in words[1:]:
            short = word.startswith("-") and not word.startswith("--")
            self.errexit |= (short and "e" in word) or word == "errexit"
            self.pipefail |= word == "pipefail"


def _first_word(and_or: AndOr | None) -> str:
    if and_or is None:
        return ""
    first = and_or.pipelines[0][0]
    return first.words[0] if isinstance(first, Command) and first.words else ""


def _guarded(and_or: AndOr) -> bool:
    """``a && b || exit 1``: any failure still stops the shell."""
    return and_or.operators[-1:] == ["||"] and _fails(and_or.pipelines[-1][-1])


class _Checker:
    def __init__(self, on_command: OnCommand, options: _Options) -> None:
        self.on_command = on_command
        self.options = options

    def sequence(self, lists: Sequence[AndOr]) -> str | None:
        for index, and_or in enumerate(lists):
            following = lists[index + 1] if index + 1 < len(lists) else None
            problem = self.and_or(and_or) or self._discarded(and_or, following)
            if problem:
                return problem
            self.options.note(and_or)
        return None

    def and_or(self, and_or: AndOr) -> str | None:
        for pipeline in and_or.pipelines:
            problem = self.pipeline(pipeline)
            if problem:
                return problem
        for index, operator in enumerate(and_or.operators):
            fallback = and_or.pipelines[index + 1][-1]
            if operator == "||" and not _fails(fallback):
                reason = f"|| {_describe(fallback)} hides its failure"
                problem = _first_loud(and_or.pipelines[: index + 1], reason)
                if problem:
                    return problem
        return None

    def pipeline(self, units: Sequence[Unit]) -> str | None:
        for unit in units:
            problem = (
                self.sequence(unit.body)
                if isinstance(unit, Group)
                else self.on_command(unit.words)
            )
            if problem:
                return problem
        if self.options.pipefail:
            return None
        return _first_loud([units[:-1]], "| keeps only the last command's status")

    def _discarded(self, and_or: AndOr, following: AndOr | None) -> str | None:
        """A statement whose status a later statement replaces."""
        if and_or.terminator == "&":
            return _first_loud(and_or.pipelines, "& runs it in the background")
        next_word = _first_word(following)
        if following is None or next_word in _BRANCH_WORDS or _guarded(and_or):
            return None
        pipelines = and_or.pipelines
        considered = pipelines[:-1] if self.options.errexit else pipelines
        if next_word == _LOOP_END:
            return _first_loud(considered, "a loop keeps only its last status")
        return _first_loud(considered, "a later command decides the exit status")


def exit_status_problem(
    text: str, on_command: OnCommand, *, errexit: bool = False
) -> str | None:
    """Why a failing command in this shell text would not fail the whole
    text, or the first problem ``on_command`` finds in a simple command."""
    checker = _Checker(on_command, _Options(errexit=errexit))
    return checker.sequence(parse(text))
