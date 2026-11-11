"""Where a marker sits in a file: documentation and string literals only
describe markers, so the "No shortcuts" gate ignores matches there."""

from __future__ import annotations

import io
import tokenize
from collections.abc import Sequence

# Documentation describes markers; it cannot switch a check off.
PROSE_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})


StringSpan = tuple[tuple[int, int], tuple[int, int]]


def python_string_spans(text: str) -> list[StringSpan]:
    """(start, end) row/column spans of every Python string token, including
    multi-line triple-quoted strings. Unreadable source yields no spans."""
    spans: list[StringSpan] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.STRING or token.type in _FSTRING_TOKENS:
                spans.append((token.start, token.end))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file the tokenizer cannot finish keeps the spans found so far.
        return spans
    return spans


_FSTRING_TOKENS = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_MIDDLE", "FSTRING_START", "FSTRING_END")
    if hasattr(tokenize, name)
)


def inside_quotes(line: str, column: int) -> bool:
    """True when ``column`` sits inside a quoted string on this one line."""
    quote = ""
    escaped = False
    for character in line[:column]:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quote and character == quote:
            quote = ""
        elif not quote and character in "\"'`":
            quote = character
    return bool(quote)


def in_string(spans: Sequence[StringSpan], row: int, column: int) -> bool:
    return any(start <= (row, column) < end for start, end in spans)
