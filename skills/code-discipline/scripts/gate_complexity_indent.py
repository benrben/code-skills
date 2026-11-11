"""Nesting from indentation, for languages whose blocks lizard's cognitive
counter treats as flat (Ruby, Lua, Erlang, Fortran, PL/SQL, Structured Text).

Lizard reports nesting 0 for these languages and adds no nesting increments
to their cognitive complexity. Formatters for all of them indent each block,
so the indentation of a line that starts with a control keyword (``if``,
``while``, ``for``, ``case``, ...) gives its depth: a structure at depth ``d``
sits inside ``d`` enclosing blocks. Only control-keyword lines are measured,
so continuation lines and data literals add nothing.

* nesting = the deepest control structure's depth + 1 (its body's level);
* cognitive gains ``d`` for each control structure at depth ``d``, the
  nesting increment of the cognitive-complexity definition.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import NamedTuple

# Lizard 1.26 ``_FLAT`` profiles: the cognitive counter tracks no nesting.
INDENT_NESTING_SUFFIXES = frozenset(
    ".rb .lua .erl .hrl .es .escript .f .f70 .f90 .f95 .f03 .f08 .for .ftn .fpp "
    ".sql .pks .pkb .pls .plb .pck .st".split()
)

# Structures that nest: conditionals, loops, switches and error handlers.
# Branch continuations (else, elsif, elseif, when) sit at their structure's
# depth and are left out; so are statements such as SELECT.
CONTROL_KEYWORDS = frozenset(
    "if unless while until for foreach loop repeat do case switch rescue "
    "catch receive".split()
)

_FIRST_WORD = re.compile(r"[ \t]*([A-Za-z_]+)\b")
_TAB_WIDTH = 4


class IndentNesting(NamedTuple):
    nesting: int
    cognitive_increment: int


def indent_width(line: str) -> int:
    stripped = line.lstrip(" \t")
    return len(line[: len(line) - len(stripped)].expandtabs(_TAB_WIDTH))


def indent_unit(widths: Sequence[int]) -> int:
    """The usual indentation step: the most common positive increase from
    one line to the next (aligned continuation lines are rarer)."""
    steps = Counter(
        after - before for before, after in zip(widths, widths[1:]) if after > before
    )
    if not steps:
        return 1
    return max(steps, key=lambda step: (steps[step], -step))


def measure_indent_nesting(lines: Sequence[str]) -> IndentNesting:
    """Nesting and cognitive nesting increments of one function's body
    lines (the lines between its header and its closing line)."""
    code = [line for line in lines if line.strip()]
    if not code:
        return IndentNesting(0, 0)
    widths = [indent_width(line) for line in code]
    base = widths[0]
    unit = indent_unit(widths)
    depths = [
        max(0, round((width - base) / unit))
        for line, width in zip(code, widths)
        if _is_control_line(line)
    ]
    if not depths:
        return IndentNesting(0, 0)
    return IndentNesting(max(depths) + 1, sum(depths))


def _is_control_line(line: str) -> bool:
    match = _FIRST_WORD.match(line)
    return match is not None and match.group(1).lower() in CONTROL_KEYWORDS


def function_body(
    text_lines: Sequence[str], start_line: int, end_line: int
) -> list[str]:
    """The lines strictly between a function's first and last line."""
    return list(text_lines[start_line : max(start_line, end_line - 1)])
