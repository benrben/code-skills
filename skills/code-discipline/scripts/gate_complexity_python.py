"""Python functions that lizard drops or cuts short, found with ``ast``.

Lizard reads Python by indentation, and three kinds of valid code fool it:

- a ``def`` whose whole body sits on the ``def`` line is not reported;
- a lambda assigned to a name is never reported;
- a function ends at the first line that is not indented deeper than its
  ``def``, even when that line only continues an open bracket or string, so
  the rest of the body is never measured.

Each would let complex code pass the complexity gate unseen. The lizard
helper runs this module's source inside the tool interpreter: after lizard
reads a Python file, ``missed_functions`` lists the functions it missed or
ended early, each with a normalized definition (``ast.unparse``) that lizard
can measure instead. Only the standard library may be imported here, and the
module must start with its docstring and ``__future__`` import, because its
source is placed at the top of the helper program.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator, Mapping
from typing import Union

DefinitionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda]


@dataclasses.dataclass(frozen=True)
class MissedFunction:
    """A function lizard did not measure, with source lizard can read."""

    name: str
    start_line: int
    end_line: int
    source: str


def missed_functions(text: str, measured: Mapping[int, int]) -> list[MissedFunction]:
    """Functions that lizard did not report, or ended before their last line.

    ``measured`` maps each start line lizard reported to the end line it
    reported. A file that this interpreter cannot parse (for example, syntax
    newer than it knows) returns nothing, so lizard's reading stands.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    missed = []
    for name, node in _definitions(tree):
        end_line = node.end_lineno or node.lineno
        if measured.get(node.lineno, 0) < end_line:
            source = _normalized_source(name, node)
            missed.append(MissedFunction(name, node.lineno, end_line, source))
    return missed


def _definitions(tree: ast.AST) -> Iterator[tuple[str, DefinitionNode]]:
    """Every def, plus lambdas assigned to a name at module or class level."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        if isinstance(node, (ast.Module, ast.ClassDef)):
            for statement in node.body:
                named = _named_lambda(statement)
                if named is not None:
                    yield named


def _named_lambda(statement: ast.stmt) -> tuple[str, ast.Lambda] | None:
    target: ast.expr
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value = statement.targets[0], statement.value
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        target, value = statement.target, statement.value
    else:
        return None
    if isinstance(target, ast.Name) and isinstance(value, ast.Lambda):
        return target.id, value
    return None


def _normalized_source(name: str, node: DefinitionNode) -> str:
    """One statement per line, every body line indented under its def."""
    if isinstance(node, ast.Lambda):
        parameters = ast.unparse(node.args)
        return f"def {name}({parameters}):\n    return {ast.unparse(node.body)}\n"
    return ast.unparse(node) + "\n"
