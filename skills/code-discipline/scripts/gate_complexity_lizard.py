"""Lizard analysis for the complexity gate: every function with its metrics.

Lizard runs in a separate interpreter (``ToolPython``) so the orchestrator can
point at its pinned tool cache (lizard 1.26.0). Source text is read here and
passed to the helper after lizard's forgiveness comments are neutralized,
because an agent could otherwise hide a function from the gate with a single
comment. For the same reason, Python files are cross-checked against ``ast``
(see ``gate_complexity_python``): functions lizard drops or ends early are
measured again from their normalized source.

Nesting depth is the deepest nesting level lizard's cognitive-complexity
counter reaches inside the function (control structures, ternaries and
lambdas), not lizard's ``nd`` extension: ``nd`` never closes blocks that are
not brace-delimited, so in Python, Ruby, Lua or a brace-less ``if (x) y();`` it
adds up sequential statements and reports six for six flat ``if`` statements.
The cognitive counter does not track nesting for Ruby, Lua, Erlang, Fortran,
PL/SQL and Structured Text, so for them nesting and the cognitive nesting
increments are measured from indentation (``gate_complexity_indent``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import gate_complexity_python
import source_files
from gate_complexity_indent import (
    INDENT_NESTING_SUFFIXES,
    function_body,
    measure_indent_nesting,
)
from gate_types import FunctionInfo, ToolPython

HELPER_TIMEOUT_SECONDS = 600

# Runs inside the tool interpreter, after the source of
# ``gate_complexity_python`` (see ``LIZARD_HELPER``). Reads
# {"files": [{"path", "text"}]} on stdin and prints {"functions",
# "unsupported", "errors"} as JSON on stdout; each error is {"path",
# "message"}. The probe wraps the counter that lizard 1.26's cognitive
# extension creates per file and records how many nesting frames it has open
# after each token.
_HELPER_DRIVER = r"""
import json
import sys

import lizard
import lizard_ext.lizardcognitive as cognitive
from lizard_languages import get_reader_for
from lizard_languages.python import PythonReader

create_counter = cognitive._counter_for


class NestingProbe:
    def __init__(self, counter):
        self.counter = counter

    def process(self, token):
        counter = self.counter
        counter.process(token)
        function = counter.function
        if counter.state is not None and function is not None:
            depth = len(counter.state.frames)
            if depth > getattr(function, "deepest_nesting", 0):
                function.deepest_nesting = depth


cognitive._counter_for = lambda reader: NestingProbe(create_counter(reader))
extensions = lizard.get_extensions(["cognitive"])


def measure(path, text):
    analysis = lizard.FileAnalyzer(extensions).analyze_source_code(path, text)
    return [
        {
            "path": path,
            "name": function.name,
            "long_name": function.long_name,
            "start_line": function.start_line,
            "end_line": function.end_line,
            "cyclomatic": function.cyclomatic_complexity,
            "cognitive": getattr(function, "cognitive_complexity", 0),
            "nesting": getattr(function, "deepest_nesting", 0),
            "parameters": function.parameter_count,
        }
        for function in analysis.function_list
    ]


def remeasure_python(path, text, rows, errors):
    measured = {}
    for row in rows:
        start = row["start_line"]
        measured[start] = max(measured.get(start, 0), row["end_line"])
    missed = missed_functions(text, measured)
    replaced = {function.start_line for function in missed}
    kept = [row for row in rows if row["start_line"] not in replaced]
    for function in missed:
        found = [
            row for row in measure(path, function.source)
            if row["name"] == function.name
        ]
        if not found:
            message = f"line {function.start_line}: lizard cannot measure {function.name}"
            errors.append({"path": path, "message": message})
            continue
        row = min(found, key=lambda row: row["start_line"])
        row.update(start_line=function.start_line, end_line=function.end_line)
        kept.append(row)
    return kept


def main():
    request = json.load(sys.stdin)
    result = {"functions": [], "unsupported": [], "errors": []}
    for entry in request["files"]:
        path, text = entry["path"], entry["text"]
        reader = get_reader_for(path)
        if reader is None:
            result["unsupported"].append(path)
            continue
        try:
            rows = measure(path, text)
            if reader is PythonReader:
                rows = remeasure_python(path, text, rows, result["errors"])
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            result["errors"].append({"path": path, "message": message})
            continue
        result["functions"].extend(rows)
    json.dump(result, sys.stdout)


main()
"""

# The cross-check module's source comes first so its ``__future__`` import
# stays at the top of the program.
LIZARD_HELPER = (
    Path(gate_complexity_python.__file__).read_text(encoding="utf-8") + _HELPER_DRIVER
)


# Lizard's forgiveness comments (``#lizard`` followed by ``forgive``,
# ``forgives(metrics)`` or ``forgive global``) drop functions from its output,
# and ``_GENERATED_MARKER`` in a comment makes it stop reading the rest of the
# file. Rewriting them keeps every line in place.
_FORGIVENESS = re.compile(r"lizard[ \t]*forgive", re.IGNORECASE)
_GENERATED_MARKER = "GENERATED CODE"

_OWNER_MODIFIERS = (
    "export|default|public|private|protected|internal|fileprivate|open|abstract|"
    "final|static|sealed|data|enum|case|inner|companion|partial|readonly|ref|"
    r"unsafe|value|inline|annotation|declare|pub(?:\([^)]*\))?"
)
_OWNER_KEYWORDS = (
    "class|impl|struct|union|interface|trait|object|extension|enum|record|"
    "module|protocol|actor|implementation"
)
# A type declaration at the start of the line that encloses a definition, such
# as ``public sealed class Cart`` or ``impl<T> Cart``. Anchoring it to the
# start keeps ``function render(record) {`` from counting as an owner.
_OWNER_DECLARATION = re.compile(
    rf"(?:(?:{_OWNER_MODIFIERS})\s+)*@?(?:{_OWNER_KEYWORDS})\b(?=\s*[\w<])"
)
_PYTHON_CLASS = re.compile(r"class\b")
_NAMESPACE = re.compile(r"\bnamespace\s+(\w+(?:\s*(?:::|\.)\s*\w+)*)")
_QUALIFIER = re.compile(r"::|\.")
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--")
_NOT_OWNERS = frozenset({"", "{"})


def neutralize_forgiveness(text: str) -> str:
    """Disarm comments that would make lizard skip code, without moving lines."""
    text = _FORGIVENESS.sub("lizard-ignored", text)
    return text.replace(_GENERATED_MARKER, "generated-code")


@dataclasses.dataclass(frozen=True)
class AnalysisError:
    """Why lizard could not measure a file (``path``) or the whole run (None).

    The gate blocks only on errors for files in the change, plus run errors.
    """

    path: str | None
    message: str

    def __str__(self) -> str:
        return self.message if self.path is None else f"{self.path}: {self.message}"


@dataclasses.dataclass(frozen=True)
class Analysis:
    """Lizard's view of a set of files."""

    functions: list[FunctionInfo]
    unsupported: list[str]
    errors: list[AnalysisError]


def analyze_functions(
    tool_python: ToolPython,
    root: Path,
    paths: Sequence[Path | str],
    *,
    timeout_seconds: int = HELPER_TIMEOUT_SECONDS,
) -> tuple[list[FunctionInfo], list[str], list[AnalysisError]]:
    """Measure every function in ``paths`` (absolute or root-relative).

    Returns the functions, the repo-relative paths lizard has no reader for,
    and the files or runs that could not be analyzed.
    """
    texts, errors = _read_sources(root, paths)
    if not texts:
        return [], [], errors
    payload = [
        {"path": path, "text": neutralize_forgiveness(text)}
        for path, text in texts.items()
    ]
    result, run_errors = _run_helper(tool_python, root, payload, timeout_seconds)
    errors.extend(run_errors)
    if result is None:
        return [], [], errors
    analysis = _analysis_from_result(result, texts)
    return analysis.functions, analysis.unsupported, errors + analysis.errors


def _relative(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return source_files.normalize_path(candidate, root)
    return candidate.as_posix()


def _read_sources(
    root: Path, paths: Sequence[Path | str]
) -> tuple[dict[str, str], list[AnalysisError]]:
    texts: dict[str, str] = {}
    errors: list[AnalysisError] = []
    for path in paths:
        relative = _relative(root, path)
        try:
            texts[relative] = source_files.read_text(root / relative)
        except OSError as error:
            message = f"cannot read file: {error.strerror or error}"
            errors.append(AnalysisError(relative, message))
    return texts, errors


def _run_helper(
    tool_python: ToolPython,
    root: Path,
    payload: list[dict[str, str]],
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, list[AnalysisError]]:
    command = [tool_python.executable, "-c", LIZARD_HELPER]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            input=json.dumps({"files": payload}),
            capture_output=True,
            text=True,
            env={**os.environ, **tool_python.env},
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, [_run_error(f"lizard timed out after {timeout_seconds} seconds")]
    except OSError as error:
        return None, [_run_error(f"cannot start {tool_python.executable}: {error}")]
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip()[-2000:]
        return None, [_run_error(f"lizard exited with {completed.returncode}: {tail}")]
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, [_run_error("lizard helper printed output that is not JSON")]
    if not isinstance(result, dict):
        return None, [
            _run_error("lizard helper printed output that is not a JSON object")
        ]
    return result, []


def _run_error(message: str) -> AnalysisError:
    return AnalysisError(None, message)


def _analysis_from_result(result: dict[str, Any], texts: Mapping[str, str]) -> Analysis:
    outlines: dict[str, SourceOutline] = {}
    functions: list[FunctionInfo] = []
    for row in result.get("functions", []):
        path = str(row["path"])
        if path not in outlines:
            outlines[path] = SourceOutline.of(path, texts.get(path, ""))
        function = _function_from_row(row, outlines[path])
        functions.append(_with_indent_nesting(function, texts.get(path, "")))
    functions.sort(key=lambda function: (function.path, function.start_line))
    return Analysis(
        functions,
        [str(path) for path in result.get("unsupported", [])],
        [
            AnalysisError(error.get("path"), str(error.get("message", "")))
            for error in result.get("errors", [])
        ],
    )


def _with_indent_nesting(function: FunctionInfo, text: str) -> FunctionInfo:
    """Nesting from indentation where lizard's counter is flat."""
    if Path(function.path).suffix.lower() not in INDENT_NESTING_SUFFIXES:
        return function
    body = function_body(text.split("\n"), function.start_line, function.end_line)
    measured = measure_indent_nesting(body)
    return dataclasses.replace(
        function,
        nesting=measured.nesting,
        cognitive=function.cognitive + measured.cognitive_increment,
    )


def _function_from_row(row: Mapping[str, Any], outline: SourceOutline) -> FunctionInfo:
    name = str(row["name"])
    long_name = str(row["long_name"])
    start_line = int(row["start_line"])
    return FunctionInfo(
        path=str(row["path"]),
        name=name,
        long_name=long_name,
        start_line=start_line,
        end_line=int(row["end_line"]),
        cyclomatic=int(row["cyclomatic"]),
        cognitive=int(row["cognitive"]),
        nesting=int(row["nesting"]),
        parameters=int(row["parameters"]),
        is_method=is_method(name, long_name, outline, start_line),
    )


# --- Methods ----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SourceOutline:
    """What ``is_method`` needs from one file, computed once per file."""

    suffix: str
    owners: tuple[str | None, ...]
    namespaces: frozenset[str]

    @classmethod
    def of(cls, path: str, text: str) -> SourceOutline:
        namespaces = {
            segment
            for declared in _NAMESPACE.findall(text)
            for segment in _QUALIFIER.split(declared.replace(" ", ""))
        }
        return cls(
            suffix=Path(path).suffix.lower(),
            owners=tuple(_owner_lines(text.splitlines())),
            namespaces=frozenset(namespaces),
        )

    def owner(self, line: int) -> str | None:
        """The nearest earlier code line indented less than ``line``."""
        return self.owners[line - 1] if 1 <= line <= len(self.owners) else None


def is_method(
    name: str, long_name: str, outline: SourceOutline, start_line: int
) -> bool:
    """Best-effort: is this function a member of a class or type?

    Lizard's names differ by language, so the rules do too:

    - Go: only a receiver (long name ``(c*Cart)Total``) makes a method.
    - Python: lizard joins nested functions with ``.`` (``outer.inner``), so
      only a definition indented directly inside a ``class`` counts.
    - Others: a name qualified by a type (``Cart::total``, ``obj.save``, but
      not a declared namespace such as ``util::parse``), or a definition
      indented directly inside a class, impl, struct or similar declaration.
    """
    if outline.suffix == ".go":
        return long_name.startswith("(")
    owner = outline.owner(start_line)
    if outline.suffix == ".py":
        return owner is not None and _PYTHON_CLASS.match(owner) is not None
    if _qualified_by_type(name, outline.namespaces):
        return True
    return owner is not None and _OWNER_DECLARATION.match(owner) is not None


def _qualified_by_type(name: str, namespaces: frozenset[str]) -> bool:
    qualifiers = _QUALIFIER.split(name)[:-1]
    return bool(qualifiers) and qualifiers[-1] not in namespaces


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _owner_lines(lines: Sequence[str]) -> list[str | None]:
    """For each line, the nearest earlier code line indented less than it.

    One pass with a stack of open, less-indented lines, so a class with
    thousands of methods costs no more than its length.
    """
    owners: list[str | None] = []
    open_lines: list[tuple[int, str]] = []
    for line in lines:
        stripped = line.strip()
        indent = _indent(line)
        if stripped in _NOT_OWNERS or stripped.startswith(_COMMENT_PREFIXES):
            # Not an owner itself (an Allman-style ``{`` belongs to the line
            # above), but may still start a definition, such as a JavaScript
            # ``#private()`` method.
            owners.append(_nearest_less_indented(open_lines, indent))
            continue
        while open_lines and open_lines[-1][0] >= indent:
            open_lines.pop()
        owners.append(open_lines[-1][1] if open_lines else None)
        open_lines.append((indent, stripped))
    return owners


def _nearest_less_indented(
    open_lines: Sequence[tuple[int, str]], indent: int
) -> str | None:
    return next((text for level, text in reversed(open_lines) if level < indent), None)
