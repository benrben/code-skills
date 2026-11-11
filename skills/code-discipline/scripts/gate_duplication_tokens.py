"""Pygments tokens for the duplication gate, normalized for Type-2 matching.

A helper program runs under ``ToolPython`` (the interpreter that can import
the pinned Pygments) and prints each file's tokens as JSON. Normalization
happens here, in the gate's own process: comments and import statements are
dropped, identifiers become ``N``, strings ``S``, and numbers ``0``, so a copy
with renamed variables or a different layout produces the same tokens.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

import source_files
from gate_types import ToolPython

HELPER_TIMEOUT_SECONDS = 600


# Words that start a dependency declaration rather than logic. Such lines are
# identical across files by nature and are never worth extracting.
_IMPORT_KEYWORDS = frozenset(
    {
        "import",
        "from",
        "package",
        "using",
        "use",
        "require",
        "require_once",
        "require_relative",
        "include",
        "include_once",
        "alias",
        "library",
    }
)
# Import words that also load a file when called with a single string,
# as in ``require("x")`` or PHP ``include('a.php');``.
_LOADER_CALLS = frozenset(
    {"require", "require_once", "require_relative", "include", "include_once"}
)
_DECLARATION_KEYWORDS = frozenset({"const", "let", "var", "local"})
# A second token like these means the first word is a variable, not a keyword.
_NOT_IMPORT_FOLLOWERS = frozenset(
    {"=", ".", ":", "?.", "->", "+=", "-=", ":=", "==", "!=", "["}
)
# ``package object`` (Scala) and ``package(...)`` open code, not a declaration.
_NOT_PACKAGE_FOLLOWERS = frozenset({"object", "(", "["})
# Words that may sit inside a multi-line import list (``a as b``, ``type A``,
# Rust ``self``/``super``). Any other keyword or operator ends the list.
_IMPORT_LIST_WORDS = frozenset(
    {"as", "from", "type", "typeof", "self", "super", "crate", "static", "_"}
)
_IMPORT_LIST_PUNCTUATION = frozenset(",.:;{}()*\\/")
_OPENING = "([{"
_CLOSING = ")]}"
# Some lexers (Solidity) report identifiers as plain text.
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")

# Literals, and separators that only hold them together. A line made mostly of
# these is a row of data (a string list, a config map, an enum table): two
# such tables have the same shape without sharing any logic.
_LITERALS = frozenset({"S", "0"})
_SEPARATORS = frozenset({",", ";", ":", "(", ")", "[", "]", "{", "}"})
# A line is data when literals reach this share of its tokens ...
DATA_LINE_LITERAL_SHARE = 0.25
# ... or when names, keywords, and operators stay below this share.
CODE_LINE_LOGIC_SHARE = 0.3

TOKENIZER_HELPER = r"""
import sys

# Drop the working directory from the import path before importing anything
# else, so a repository file named json.py or pygments/ cannot stand in.
sys.path[:] = [entry for entry in sys.path if entry not in ("", ".")]

import json

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

request = json.loads(sys.stdin.read())
result = {"files": {}, "skipped": {}}
for filename in request["files"]:
    try:
        with open(filename, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        lexer = get_lexer_for_filename(filename, text, stripnl=False)
    except ClassNotFound:
        result["skipped"][filename] = "no Pygments lexer"
        continue
    except OSError as error:
        result["skipped"][filename] = f"unreadable: {error}"
        continue
    tokens = []
    line = 1
    for token_type, value in lexer.get_tokens(text):
        if value.strip():
            tokens.append([str(token_type), value, line])
        line += value.count("\n")
    result["files"][filename] = tokens
sys.stdout.write(json.dumps(result))
"""


class DuplicationToolError(RuntimeError):
    """The Pygments helper could not run or returned unreadable output."""


class RawToken(NamedTuple):
    """A Pygments token as the helper reports it."""

    kind: str
    value: str
    line: int


class Token(NamedTuple):
    """A normalized token: the text clones are compared on, and its line."""

    text: str
    line: int


@dataclasses.dataclass(frozen=True)
class TokenizedSources:
    """Normalized tokens per repo-relative path, and files that were skipped
    with the reason (no lexer, unreadable)."""

    tokens: dict[str, list[Token]]
    skipped: list[tuple[str, str]]

    def unreadable(self) -> list[str]:
        """Files the helper could not open; the gate cannot judge them."""
        return [
            path for path, reason in self.skipped if reason.startswith("unreadable")
        ]


def tokenize_sources(
    tool_python: ToolPython,
    root: Path,
    files: Sequence[Path],
    timeout_seconds: int = HELPER_TIMEOUT_SECONDS,
) -> TokenizedSources:
    """Run the Pygments helper over ``files`` and normalize its tokens."""
    if not files:
        return TokenizedSources({}, [])
    request = json.dumps({"files": [str(Path(path).resolve()) for path in files]})
    output = _run_helper(tool_python, request, timeout_seconds)
    return parse_helper_output(output, root)


def _run_helper(tool_python: ToolPython, request: str, timeout_seconds: int) -> str:
    """Run the helper from an empty directory with absolute file paths, so
    nothing in the repository can be imported in place of Pygments or the
    standard library."""
    command = [tool_python.executable, "-c", TOKENIZER_HELPER]
    environment = {**os.environ, **tool_python.env, "PYTHONSAFEPATH": "1"}
    try:
        with tempfile.TemporaryDirectory(prefix="duplication-") as neutral:
            result = subprocess.run(
                command,
                cwd=neutral,
                input=request.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise DuplicationToolError(
            f"Pygments tokenizer timed out after {timeout_seconds} seconds."
        ) from error
    except OSError as error:
        raise DuplicationToolError(
            f"Pygments tokenizer could not start: {error}"
        ) from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise DuplicationToolError(
            f"Pygments tokenizer failed (exit {result.returncode}): {message}"
        )
    return result.stdout.decode("utf-8", errors="replace")


def parse_helper_output(output: str, root: Path) -> TokenizedSources:
    """Read the helper's JSON into normalized tokens keyed by relative path."""
    try:
        value = json.loads(output)
        files = value["files"]
        skipped = value["skipped"]
        tokens = {
            _relative(filename, root): normalize_tokens(
                RawToken(str(kind), str(text), int(line)) for kind, text, line in rows
            )
            for filename, rows in files.items()
        }
        reasons = [
            (_relative(filename, root), str(reason))
            for filename, reason in skipped.items()
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise DuplicationToolError(
            f"Pygments tokenizer returned unreadable output: {error}"
        ) from error
    return TokenizedSources(tokens, sorted(reasons))


def _relative(filename: str, root: Path) -> str:
    return source_files.normalize_path(Path(filename), root)


def normalize_tokens(raw: Iterable[RawToken]) -> list[Token]:
    """Drop comments and import statements, then normalize names, strings, and
    numbers so renamed copies compare equal. Adjacent string pieces collapse
    into one ``S`` because lexers split strings at escapes differently."""
    code = [token for token in raw if not token.kind.startswith("Token.Comment")]
    result: list[Token] = []
    for token in _without_imports(code):
        text = _normalized_text(token)
        if text == "S" and result and result[-1].text == "S":
            continue
        result.append(Token(text, token.line))
    return result


def _normalized_text(token: RawToken) -> str:
    if token.kind.startswith("Token.Name") or _is_plain_identifier(token):
        return "N"
    if token.kind.startswith("Token.Literal.String"):
        return "S"
    if token.kind.startswith("Token.Literal.Number"):
        return "0"
    return token.value


def _is_plain_identifier(token: RawToken) -> bool:
    return token.kind.startswith(("Token.Text", "Token.Other")) and bool(
        _IDENTIFIER.fullmatch(token.value)
    )


def code_line_flags(tokens: Sequence[Token]) -> list[bool]:
    """For each token, whether its line holds code rather than a row of
    data such as ``("name", 3),`` or ``"key": value,``."""
    flags: list[bool] = []
    for _, group in itertools.groupby(tokens, key=lambda token: token.line):
        texts = [token.text for token in group]
        flags.extend([is_code_line(texts)] * len(texts))
    return flags


def is_code_line(texts: Sequence[str]) -> bool:
    literals = sum(text in _LITERALS for text in texts)
    separators = sum(text in _SEPARATORS for text in texts)
    logic = len(texts) - literals - separators
    return (
        literals < len(texts) * DATA_LINE_LITERAL_SHARE
        and logic >= len(texts) * CODE_LINE_LOGIC_SHARE
    )


# --- Import statements ---------------------------------------------------


class ImportSpan(NamedTuple):
    """The import part of a line: its first ``end`` tokens. ``open_brackets``
    is above zero when the import list continues on the next lines."""

    end: int
    open_brackets: int


def _without_imports(tokens: Sequence[RawToken]) -> Iterator[RawToken]:
    """Skip import statements, including multi-line import lists
    (``from x import (``, Go ``import (``, JS ``import {``), but keep code that
    shares a line with an import or follows an unclosed bracket."""
    open_brackets = 0
    for line_tokens in _group_by_line(tokens):
        if open_brackets > 0 and _continues_import_list(line_tokens):
            open_brackets += _bracket_balance(line_tokens)
            continue
        code, open_brackets = _strip_imports(line_tokens)
        yield from code


def _group_by_line(tokens: Sequence[RawToken]) -> Iterator[list[RawToken]]:
    for _, group in itertools.groupby(tokens, key=lambda token: token.line):
        yield list(group)


def _strip_imports(line_tokens: list[RawToken]) -> tuple[list[RawToken], int]:
    """Remove the import statements a line starts with (``import a; import
    b; code``); return the code left and the brackets left open."""
    code = line_tokens
    open_brackets = 0
    while code:
        span = import_span(code)
        if span is None:
            break
        code = code[span.end :]
        open_brackets = span.open_brackets
    return code, open_brackets


def _continues_import_list(tokens: Sequence[RawToken]) -> bool:
    """Lines inside a multi-line import list hold only names, strings,
    separators, and words such as ``as``; anything else is code."""
    return all(_fits_import_list(token) for token in tokens)


def _fits_import_list(token: RawToken) -> bool:
    if token.kind.startswith(("Token.Name", "Token.Literal.String")):
        return True
    if token.value in _IMPORT_LIST_WORDS:
        return True
    return all(character in _IMPORT_LIST_PUNCTUATION for character in token.value)


def _bracket_balance(tokens: Sequence[RawToken]) -> int:
    """Opened minus closed brackets outside string literals. Some lexers join
    punctuation (PHP ``);``), so characters are counted, not tokens."""
    balance = 0
    for token in tokens:
        if not token.kind.startswith("Token.Literal"):
            balance += sum(token.value.count(bracket) for bracket in _OPENING)
            balance -= sum(token.value.count(bracket) for bracket in _CLOSING)
    return balance


def import_span(tokens: Sequence[RawToken]) -> ImportSpan | None:
    """The leading import statement of a line, or None when the line starts
    with code. Recognized: import keywords (after modifiers such as Rust
    ``pub``, C# ``global``, Swift ``@testable``), re-exports (``export {``,
    ``export * from``, ``export type {``), and ``require(...)`` assignments.
    The statement ends at its first top-level ``;``."""
    start = _skip_modifiers(tokens)
    if start >= len(tokens) or not _starts_import(tokens, start):
        return None
    end = _statement_end(tokens, start)
    if _first_word(tokens[start]) == "package":
        return ImportSpan(end, 0)
    return ImportSpan(end, max(0, _bracket_balance(tokens[start:end])))


def _skip_modifiers(tokens: Sequence[RawToken]) -> int:
    """Index of the first token after leading ``pub``/``pub(crate)``,
    ``global``, and ``@attribute`` modifiers."""
    index = 0
    while index < len(tokens):
        value = tokens[index].value
        if value == "pub" and _value_at(tokens, index + 1) == "(":
            index = _after_group(tokens, index + 1)
        elif value in ("pub", "global"):
            index += 1
        elif value == "@" and _kind_at(tokens, index + 1).startswith("Token.Name"):
            index += 2
        else:
            return index
    return index


def _after_group(tokens: Sequence[RawToken], opening: int) -> int:
    """Index after the bracket group that starts at ``opening``."""
    depth = 0
    for index in range(opening, len(tokens)):
        depth += _bracket_balance(tokens[index : index + 1])
        if depth <= 0:
            return index + 1
    return len(tokens)


def _starts_import(tokens: Sequence[RawToken], start: int) -> bool:
    first = tokens[start]
    word = _first_word(first)
    if word == "export":
        return _is_reexport(tokens, start + 1)
    if word in _DECLARATION_KEYWORDS:
        return any(token.value == "require" for token in tokens[start:])
    if word not in _IMPORT_KEYWORDS:
        return False
    if first.kind.startswith(("Token.Literal", "Token.Operator")):
        return False
    return _follower_allows_import(tokens, start, word)


def _is_reexport(tokens: Sequence[RawToken], index: int) -> bool:
    """``export {``, ``export *``, ``export type {``/``*``, Dart
    ``export 'a.dart'``; not ``export type Name = ...`` or exported code."""
    if _value_at(tokens, index) == "type":
        index += 1
    follower = _value_at(tokens, index)
    return follower in ("{", "*") or _kind_at(tokens, index).startswith("Token.Literal")


def _follower_allows_import(tokens: Sequence[RawToken], start: int, word: str) -> bool:
    """The token after an import word decides whether the word is used as a
    keyword: ``use = 3``, ``import(...)``, and ``package object`` are code."""
    follower = _value_at(tokens, start + 1)
    if follower == "(":
        return _is_bracketed_import(tokens, start, word)
    if word == "package":
        return follower not in _NOT_PACKAGE_FOLLOWERS
    trusted = tokens[start].kind.startswith("Token.Keyword.Namespace")
    return trusted or follower not in _NOT_IMPORT_FOLLOWERS


def _is_bracketed_import(tokens: Sequence[RawToken], start: int, word: str) -> bool:
    """Go ``import (`` opens an import block; ``require("x")`` loads a file.
    Every other ``word(`` is a call: dynamic ``import()``, Solidity
    ``require(cond, msg)``, React ``use(promise)``, C# ``using (var ...)``."""
    rest = tokens[start + 2 :]
    if not rest:
        return word == "import"
    return word in _LOADER_CALLS and all(
        token.kind.startswith("Token.Literal.String")
        or all(character in ");" for character in token.value)
        for token in rest
    )


def _statement_end(tokens: Sequence[RawToken], start: int) -> int:
    """Index after the first ``;`` outside brackets, or the line's end."""
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        depth += _bracket_balance([token])
        if depth <= 0 and ";" in token.value and not _is_literal(token):
            return index + 1
    return len(tokens)


def _is_literal(token: RawToken) -> bool:
    return token.kind.startswith("Token.Literal")


def _first_word(token: RawToken) -> str:
    """Java's lexer joins ``import static`` into one token."""
    words = token.value.split()
    return words[0] if words else ""


def _value_at(tokens: Sequence[RawToken], index: int) -> str:
    return tokens[index].value if index < len(tokens) else ""


def _kind_at(tokens: Sequence[RawToken], index: int) -> str:
    return tokens[index].kind if index < len(tokens) else ""


def indented_lines(text: str) -> frozenset[int]:
    """1-based numbers of non-blank lines that start with whitespace: lines
    inside a function, class, or block rather than at the top level."""
    return frozenset(
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip() and line[:1] in (" ", "\t")
    )


def has_body_line(indented: frozenset[int], start: int, end: int) -> bool:
    """True when ``start..end`` holds at least one indented line. A repeated
    run of top-level one-line statements (constants, signatures) is a shape,
    not copied logic."""
    return any(line in indented for line in range(start, end + 1))


def whole_lines(
    first: tuple[Sequence[int], int],
    second: tuple[Sequence[int], int],
    length: int,
) -> tuple[int, int]:
    """Trim a match of ``length`` tokens, given as (token line numbers, start
    position) per side, to tokens that begin and end whole lines on both
    sides, so the reported line ranges are exact. Returns (tokens skipped at
    the start, new length); (0, length) when no whole line is left."""

    def starts(offset: int) -> bool:
        return all(
            position + offset == 0
            or lines[position + offset - 1] != lines[position + offset]
            for lines, position in (first, second)
        )

    def ends(offset: int) -> bool:
        return all(
            position + offset + 1 == len(lines)
            or lines[position + offset + 1] != lines[position + offset]
            for lines, position in (first, second)
        )

    skip = next((k for k in range(length) if starts(k)), length)
    end = next((k for k in range(length - 1, skip - 1, -1) if ends(k)), skip - 1)
    return (skip, end - skip + 1) if end >= skip else (0, length)
