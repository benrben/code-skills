"""Find error handlers that swallow errors without a trace.

A silent handler hides a failure: the program carries on as if nothing went
wrong and no test or log shows why. Detection is static and per language
family: Python uses its own parser, Ruby and the brace languages use
regular expressions over the text with comments and strings blanked out.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import warnings
from collections.abc import Iterable, Sequence

PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
RUBY_EXTENSIONS = frozenset({".rb"})
GO_EXTENSIONS = frozenset({".go"})


@dataclasses.dataclass(frozen=True)
class SilentHandler:
    """A handler that swallows errors; lines are 1-based and inclusive."""

    kind: str
    start_line: int
    end_line: int


def silent_handlers(text: str, extension: str) -> list[SilentHandler]:
    """Silent handlers in one source file, chosen by its extension."""
    suffix = extension.lower()
    if suffix in PYTHON_EXTENSIONS:
        handlers = python_silent_handlers(text)
    elif suffix in RUBY_EXTENSIONS:
        handlers = ruby_silent_handlers(text)
    else:
        handlers = brace_silent_handlers(text)
    if suffix in GO_EXTENSIONS:
        handlers.extend(go_discarded_errors(text))
    lines = text.split("\n")
    undocumented = [
        handler for handler in handlers if not is_documented(handler, lines)
    ]
    return sorted(undocumented, key=lambda handler: handler.start_line)


# A comment of three or more words inside the handler gives the reason the
# error is ignored. That is the skill's rule: never swallow an error
# without a documented reason.
_REASON_COMMENT = re.compile(r"(?:#|//|/\*)\s*\w+\W+\w+\W+\w+")


def is_documented(handler: SilentHandler, lines: Sequence[str]) -> bool:
    first = max(handler.start_line, 1)
    last = min(handler.end_line, len(lines))
    return any(
        _REASON_COMMENT.search(lines[number - 1]) for number in range(first, last + 1)
    )


# --- Python ---------------------------------------------------------------


def python_silent_handlers(text: str) -> list[SilentHandler]:
    """``except`` blocks whose body does nothing, and ``with suppress(...)``
    blocks. A file the parser cannot read yields nothing; other gates report
    syntax errors."""
    try:
        with warnings.catch_warnings():
            # Invalid escape sequences warn on parse; they are not our concern.
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Very long generated expressions exhaust the parser's recursion or
        # memory limits; one such file must not abort the whole gate.
        return []
    handlers: list[SilentHandler] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and all(
            _does_nothing(statement) for statement in node.body
        ):
            handlers.append(
                SilentHandler(_except_label(node), node.lineno, _end_line(node))
            )
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            handlers.extend(_suppressions(node))
    return handlers


def _does_nothing(statement: ast.stmt) -> bool:
    """``pass``, ``...``, any bare constant, ``continue``, ``break``, or a
    ``return`` without a value."""
    if isinstance(statement, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(statement, ast.Return):
        value = statement.value
        return value is None or (
            isinstance(value, ast.Constant) and value.value is None
        )
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _suppressions(node: ast.With | ast.AsyncWith) -> list[SilentHandler]:
    """``with suppress(...)`` is ``except ...: pass`` spelled differently.
    Only the ``with`` header counts as the handler, not the guarded body."""
    header_end = max(node.lineno, node.body[0].lineno - 1)
    return [
        SilentHandler(f"with {ast.unparse(item.context_expr)}", node.lineno, header_end)
        for item in node.items
        if _is_suppress_call(item.context_expr)
    ]


_BROAD_ERRORS = frozenset({"Exception", "BaseException"})


def _is_suppress_call(expression: ast.expr) -> bool:
    """``suppress()`` of a broad error. Naming one specific error, such as
    ``suppress(FileNotFoundError)`` around cleanup, is a visible decision."""
    if not isinstance(expression, ast.Call) or not _suppresses_broadly(expression):
        return False
    function = expression.func
    if isinstance(function, ast.Name):
        return function.id == "suppress"
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "suppress"
        and isinstance(function.value, ast.Name)
        and function.value.id == "contextlib"
    )


def _suppresses_broadly(call: ast.Call) -> bool:
    if not call.args:
        return True
    return any(
        isinstance(argument, ast.Name)
        and argument.id in _BROAD_ERRORS
        or isinstance(argument, ast.Attribute)
        and argument.attr in _BROAD_ERRORS
        for argument in call.args
    )


def _except_label(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "except"
    return f"except {ast.unparse(node.type)}"


def _end_line(node: ast.ExceptHandler) -> int:
    return node.end_lineno if node.end_lineno is not None else node.lineno


# --- masking --------------------------------------------------------------

_SLASH_COMMENTS = r"//[^\n]*|/\*.*?\*/"
_HASH_COMMENTS = r"#[^\n]*"
_QUOTED = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
_TEMPLATE = r"`(?:\\.|[^`\\])*`"

_BRACE_NOISE = re.compile("|".join((_SLASH_COMMENTS, _QUOTED, _TEMPLATE)), re.S)
_HASH_NOISE = re.compile("|".join((_HASH_COMMENTS, _QUOTED)))


def mask(text: str, noise: re.Pattern[str]) -> str:
    """Blank comments and string contents, keeping every newline and offset
    so positions in the masked text match the original."""
    return noise.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)


def line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- brace languages -----------------------------------------------------

# ``catch`` blocks (Java, Kotlin, C#, PHP, Swift, JS/TS, Dart, C++) and Go
# error checks, including ``if err := f(); err != nil {`` and ``nil != err``.
# ``.catch(`` is a promise method and is handled separately.
_GO_ERROR = r"(?:\be|[\w.]*[eE]rr(?:or)?)"
_BRACE_HANDLER = re.compile(
    r"(?P<catch>(?<![.\w$])catch\b)[^{};]{0,300}\{"
    r"|(?P<check>\bif\s+(?:[^{};\n]*;\s*)?"
    rf"(?:{_GO_ERROR}\s*!=\s*nil|nil\s*!=\s*{_GO_ERROR})\s*\{{)"
)
# A statement that drops the error: a bare return, or ``void error;``.
_NOTHING = r"(?:return\s*(?:null|nil|undefined)?|void\s+[\w$]+)"
_NOTHING_BODY = re.compile(rf"\s*(?:{_NOTHING}\s*)?;?\s*")
# The callback may be async and its parameter list may carry TypeScript types.
_PROMISE_CATCH = re.compile(
    r"\.catch\(\s*(?:async\s+)?(?:"
    r"(?:\([^)]*\)|[\w$]+)\s*=>\s*"
    rf"(?:\{{\s*(?:{_NOTHING}\s*;?\s*)?\}}|null|undefined)"
    rf"|function\s*[\w$]*\s*\([^)]*\)\s*\{{\s*(?:{_NOTHING}\s*;?\s*)?\}}"
    r")\s*\)"
)


def brace_silent_handlers(text: str) -> list[SilentHandler]:
    """Empty ``catch``/``if err != nil`` blocks, blocks that only return
    nothing, and promise ``.catch`` callbacks that drop the error."""
    masked = mask(text, _BRACE_NOISE)
    handlers = list(_empty_blocks(text, masked))
    for match in _PROMISE_CATCH.finditer(masked):
        handlers.append(
            SilentHandler(
                ".catch()", line_at(text, match.start()), line_at(text, match.end())
            )
        )
    return handlers


def _empty_blocks(text: str, masked: str) -> Iterable[SilentHandler]:
    for match in _BRACE_HANDLER.finditer(masked):
        close = matching_brace(masked, match.end() - 1)
        if close < 0:
            continue
        if not _NOTHING_BODY.fullmatch(masked[match.end() : close]):
            continue
        kind = "catch" if match.group("catch") else "error check"
        yield SilentHandler(kind, line_at(text, match.start()), line_at(text, close))


def matching_brace(text: str, open_index: int) -> int:
    """Index of the brace closing the one at ``open_index``, or -1."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


_DISCARDED_ERROR = re.compile(r"^[ \t]*_\s*=\s*err\b", re.M)


def go_discarded_errors(text: str) -> list[SilentHandler]:
    """Go ``_ = err``: the error is assigned away on purpose."""
    masked = mask(text, _BRACE_NOISE)
    return [
        SilentHandler("_ = err", line, line)
        for line in (
            line_at(text, match.start()) for match in _DISCARDED_ERROR.finditer(masked)
        )
    ]


# --- Ruby -----------------------------------------------------------------

_RESCUE = re.compile(r"(?:^|;)\s*rescue\b[^;\n]*")
_RESCUE_THEN_NOTHING = re.compile(r";\s*(?:end|nil)\b")
_RESCUE_MODIFIER = re.compile(r"[^\s;]\s+rescue\s+nil\b")
_NOTHING_LINE = frozenset({"end", "nil"})


def ruby_silent_handlers(text: str) -> list[SilentHandler]:
    """``rescue`` clauses followed directly by ``end`` or ``nil`` (on the
    same line after ``;`` or on the next code line), and ``x rescue nil``."""
    lines = mask(text, _HASH_NOISE).split("\n")
    handlers: list[SilentHandler] = []
    for index, line in enumerate(lines):
        if _RESCUE_MODIFIER.search(line):
            handlers.append(SilentHandler("rescue nil", index + 1, index + 1))
        for match in _RESCUE.finditer(line):
            end = _rescue_end(lines, index, line[match.end() :])
            if end is not None:
                handlers.append(SilentHandler("rescue", index + 1, end))
    return handlers


def _rescue_end(lines: list[str], index: int, tail: str) -> int | None:
    """The last line of a silent rescue starting on ``lines[index]``."""
    if _RESCUE_THEN_NOTHING.match(tail):
        return index + 1
    if tail.strip():
        return None
    following = _next_code_line(lines, index + 1)
    if following is None or lines[following].strip() not in _NOTHING_LINE:
        return None
    return following + 1


def _next_code_line(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None
