"""Dead-code gate: functions that nothing in the repository references.

The gate reuses the function list lizard already produced for the complexity
gate and searches the repository's code-bearing text files (source, tests,
templates, configuration) for the bare function name. In an incremental scope
it judges only the change: functions the change added, and existing functions
whose name appears in a removed line or in a deleted file (the change may have
deleted their last caller). In repository scope every function is checked.

A plain text search cannot see dynamic dispatch, framework registration, or
callers outside the repository. Those cases are reported as informational
details instead of failures, so the gate only fails when a function is both
unreferenced and clearly private to this code base, or when the change itself
added an unreferenced function in a language without visibility markers.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

import source_files
from diff_scope import ChangeSet
from gate_dead_code_references import (
    ReferenceIndex,
    build_reference_index,
    identifiers,
    is_reference_path,
    name_position,
    split_by_tests,
)
from gate_types import (
    BLOCKED,
    FunctionInfo,
    GateItem,
    GateOutcome,
    outcome_for_items,
    plural,
)

KEY = "dead_code"
TITLE = "Dead code"

NEW = "new"
ORPHAN = "orphan"
REPOSITORY = "repository"

Visibility = Literal["exported", "private", "default"]
LinesFor = Callable[[str], Sequence[str]]

# Identifiers as most languages spell them. ``$`` belongs to the identifier so
# that ``$total`` (a PHP variable or a jQuery name) is not a call of ``total``.
# Ruby method names may end with one ``?``, ``!``, or ``=``.
_PLAIN_NAME = re.compile(r"^[A-Za-z_$][\w$]*[?!=]?$")
_RUBY_SUFFIXES = ("?", "!", "=")
_DUNDER = re.compile(r"^__\w+__$")
# Python's ``_missing_``-style hooks that Enum and similar classes call by name.
_SUNDER = re.compile(r"^_[A-Za-z0-9](?:\w*[A-Za-z0-9])?_$")
_ENTRY_NAMES = frozenset({"main", "init", "constructor", "new"})

# Text that can call a function: everything except prose, lockfiles and data.
_AT_ANNOTATION = re.compile(r"^\s*@[A-Za-z_]")
_AT_LANGUAGES = (
    "Python Java Kotlin Scala Groovy Dart Swift JavaScript TypeScript Vue".split()
)
_DECORATORS: dict[str, re.Pattern[str]] = {
    "C#": re.compile(r"^\s*\[\s*[A-Za-z_]"),
    "Rust": re.compile(r"^\s*#!?\["),
    "PHP": re.compile(r"^\s*#\["),
    "Elixir": re.compile(r"^\s*@impl\b"),
    **dict.fromkeys(_AT_LANGUAGES, _AT_ANNOTATION),
}
_DECORATOR_LOOKBACK = 10
_STATEMENT_ENDINGS = (";", "{", "}", ":")

# C declarations may put ``static int`` on the line above the name (GNU style).
_C_FAMILY = frozenset({"C", "C/C++", "C++", "Objective-C"})
_SIGNATURE_LOOKBACK = 3
_NOT_SIGNATURE_START = ("#", "//", "/*", "*")

_EXPORT_WORD = re.compile(r"\bexports?\b")
# ``pub`` alone exports; ``pub(crate)``, ``pub(super)`` and ``pub(in ...)`` do not.
_PUB_EXPORTED = re.compile(r"\bpub\b(?!\s*\()")
_PUBLIC_WORD = re.compile(r"\b(?:public|open|internal|protected)\b")
_PRIVATE_WORD = re.compile(r"\b(?:private|fileprivate)\b")
_STATIC_WORD = re.compile(r"\bstatic\b")


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A function the gate checks, and why it is in scope."""

    function: FunctionInfo
    reason: str


@dataclasses.dataclass(frozen=True)
class Verdict:
    """The result for one unreferenced function: a failure or a note."""

    candidate: Candidate
    fails: bool
    note: str
    tests_only: bool = False


@dataclasses.dataclass(frozen=True)
class DefinitionShape:
    """How a definition line spells the function, to recognise its other
    declarations (an old signature, a prototype, an overload) as non-calls."""

    prefix: str
    ending: str

    @classmethod
    def of(cls, definition: str, name: str) -> DefinitionShape | None:
        position = name_position(definition, name)
        if position is None:
            return None
        return cls(definition[:position].strip(), definition.rstrip()[-1:])

    def matches(self, text: str, name: str) -> bool:
        """True when ``text`` declares ``name`` the way the definition does.

        A bare name at the start of a line could be a call, so without a prefix
        the line must also end like the definition (``{`` or ``)``, not ``;``).
        """
        position = name_position(text, name)
        if position is None or text[:position].strip() != self.prefix:
            return False
        return bool(self.prefix) or text.rstrip()[-1:] == self.ending


@dataclasses.dataclass(frozen=True)
class RemovedLines:
    """Removed lines of reference files, grouped by the identifiers they hold."""

    by_name: dict[str, tuple[str, ...]]

    def mentioning(self, name: str) -> tuple[str, ...]:
        return self.by_name.get(name, ())


def search_name(function: FunctionInfo) -> str:
    """The name callers write: the bare name without Ruby's ``?``/``!``/``=``."""
    name = function.bare_name
    return name[:-1] if name.endswith(_RUBY_SUFFIXES) else name


def removed_lines(root: Path, changes: ChangeSet) -> RemovedLines:
    """Lines the change removed from reference files, plus every line of each
    deleted reference file, since deleting a file removes its calls too."""
    texts = [line for path, line in changes.removed_text() if is_reference_path(path)]
    for path in sorted(changes.deleted):
        if is_reference_path(path):
            texts.extend((changes.base_text(root, path) or "").splitlines())
    by_name: defaultdict[str, list[str]] = defaultdict(list)
    for text in texts:
        for name in set(identifiers(text)):
            by_name[name].append(text)
    return RemovedLines({name: tuple(lines) for name, lines in by_name.items()})


def select_candidates(
    functions: Sequence[FunctionInfo],
    changes: ChangeSet,
    removed: RemovedLines,
    lines_for: LinesFor,
) -> list[Candidate]:
    """Functions the change is responsible for, or every function repo-wide."""
    if not changes.incremental:
        return [Candidate(function, REPOSITORY) for function in functions]
    candidates: list[Candidate] = []
    for function in functions:
        reason = _incremental_reason(function, changes, removed, lines_for)
        if reason is not None:
            candidates.append(Candidate(function, reason))
    return candidates


def _incremental_reason(
    function: FunctionInfo,
    changes: ChangeSet,
    removed: RemovedLines,
    lines_for: LinesFor,
) -> str | None:
    """NEW for an added definition that did not exist before, ORPHAN when the
    change removed a line that called it, otherwise None.

    An added definition line whose old form appears among the removed lines
    is an edited or moved function, not a new one.
    """
    name = search_name(function)
    mentions = removed.mentioning(name)
    added = changes.is_added(function.path, function.start_line)
    if not added and not mentions:
        return None
    definition = _line(lines_for(function.path), function.start_line)
    shape = DefinitionShape.of(definition, name)
    declarations = [text for text in mentions if shape and shape.matches(text, name)]
    if added and not declarations:
        return NEW
    if len(mentions) > len(declarations):
        return ORPHAN
    return None


def is_constructor(function: FunctionInfo) -> bool:
    """``Cart::Cart`` or ``Cart.Cart``: a method named after its class."""
    for separator in ("::", "."):
        parts = function.name.split(separator)
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            return True
    return False


def exclusion_reason(function: FunctionInfo) -> str | None:
    """Why a function is never reported, or None when it must be checked."""
    if not _PLAIN_NAME.match(function.bare_name):
        return "not a plain identifier"
    name = search_name(function)
    if len(name) < 3:
        return "name too short to search"
    if name in _ENTRY_NAMES or _DUNDER.match(name) or _SUNDER.match(name):
        return "entry point or language hook"
    if is_constructor(function):
        return "constructor"
    if source_files.is_test_path(function.path):
        return "in a test file"
    return None


def has_decorator(lines: Sequence[str], start_line: int, language: str | None) -> bool:
    """True when an annotation sits on the definition line or directly above it.

    The search walks up through the contiguous block above the definition and
    stops at a blank line or a line that ends a statement or opens a block, so a
    previous function's decorator never counts.
    """
    pattern = _DECORATORS.get(language or "")
    if pattern is None:
        return False
    if pattern.match(_line(lines, start_line)):
        return True
    for number in range(start_line - 1, start_line - 1 - _DECORATOR_LOOKBACK, -1):
        text = _line(lines, number).strip()
        if not text or text.endswith(_STATEMENT_ENDINGS):
            return False
        if pattern.match(text):
            return True
    return False


def definition_text(function: FunctionInfo, lines: Sequence[str]) -> str:
    """The definition line, joined in C-family code with the return-type lines
    above it, where GNU style puts ``static``."""
    definition = _line(lines, function.start_line)
    if source_files.language_for(function.path) not in _C_FAMILY:
        return definition
    head: list[str] = []
    for number in range(function.start_line - 1, 0, -1):
        text = _line(lines, number).strip()
        if len(head) == _SIGNATURE_LOOKBACK or not text:
            break
        if text.endswith((";", "{", "}")) or text.startswith(_NOT_SIGNATURE_START):
            break
        head.insert(0, text)
    return " ".join([*head, definition])


def modifiers(definition: str, name: str) -> str:
    """The text before the name, where visibility keywords live; parameter names
    such as ``private=True`` never count."""
    position = name_position(definition, name)
    return definition if position is None else definition[:position]


def visibility(function: FunctionInfo, definition: str) -> Visibility:
    """Best-effort visibility from naming rules and the definition's modifiers."""
    name = search_name(function)
    if name.startswith("_"):
        return "private"
    rule = _VISIBILITY_RULES.get(source_files.language_for(function.path) or "")
    return rule(name, modifiers(definition, name)) if rule else "default"


def is_private_member(function: FunctionInfo, definition: str) -> bool:
    """A method marked private by name (``_helper``) or keyword (``private``,
    ``fileprivate``); no interface can call it from outside its class."""
    name = search_name(function)
    if name.startswith("_"):
        return True
    return bool(_PRIVATE_WORD.search(modifiers(definition, name)))


def _go_visibility(name: str, prefix: str) -> Visibility:
    return "exported" if name[:1].isupper() else "private"


def _rust_visibility(name: str, prefix: str) -> Visibility:
    return "exported" if _PUB_EXPORTED.search(prefix) else "private"


def _script_visibility(name: str, prefix: str) -> Visibility:
    return "exported" if _EXPORT_WORD.search(prefix) else "private"


def _modifier_visibility(name: str, prefix: str) -> Visibility:
    if _PRIVATE_WORD.search(prefix):
        return "private"
    return "exported" if _PUBLIC_WORD.search(prefix) else "default"


def _c_visibility(name: str, prefix: str) -> Visibility:
    return "private" if _STATIC_WORD.search(prefix) else "default"


_VISIBILITY_RULES: dict[str, Callable[[str, str], Visibility]] = {
    "Go": _go_visibility,
    "Rust": _rust_visibility,
    **dict.fromkeys(("JavaScript", "TypeScript", "Vue"), _script_visibility),
    **dict.fromkeys(
        ("Java", "Kotlin", "C#", "Swift", "Scala", "PHP", "Dart", "Groovy", "Ruby"),
        _modifier_visibility,
    ),
    **dict.fromkeys(_C_FAMILY, _c_visibility),
}


def reference_count(
    index: ReferenceIndex, function: FunctionInfo, lines: Sequence[str]
) -> int:
    """Occurrences of the function's name outside the function itself.

    Mentions inside its own body (recursion, a docstring) and other
    declarations in its own file (a prototype, an overload) are not callers.
    """
    name = search_name(function)
    total = index.counts.get(name, 0)
    if function.path not in index.indexed_paths:
        return total
    own_lines = lines[function.start_line - 1 : function.end_line]
    total -= sum(identifiers(line).count(name) for line in own_lines)
    shape = DefinitionShape.of(_line(lines, function.start_line), name)
    if total <= 0 or shape is None:
        return max(total, 0)
    declarations = sum(
        identifiers(line).count(name)
        for number, line in enumerate(lines, start=1)
        if name in line and not function.contains(number) and shape.matches(line, name)
    )
    return max(total - declarations, 0)


def judge(candidate: Candidate, lines: Sequence[str], incremental: bool) -> Verdict:
    """Decide whether an unreferenced function fails the gate or is only noted."""
    function = candidate.function
    language = source_files.language_for(function.path)
    if has_decorator(lines, function.start_line, language):
        return Verdict(candidate, False, "may be registered dynamically")
    definition = definition_text(function, lines)
    if function.is_method:
        if is_private_member(function, definition):
            return Verdict(candidate, True, "private")
        return Verdict(candidate, False, "method; may be called through an interface")
    kind = visibility(function, definition)
    if kind == "private":
        return Verdict(candidate, True, "private")
    if kind == "exported":
        return Verdict(candidate, False, "exported; may be used by other projects")
    if incremental:
        return Verdict(candidate, True, "added or orphaned by this change")
    return Verdict(candidate, False, "public; may be used by other projects")


def evaluate_dead_code(
    root: Path,
    functions_all: Sequence[FunctionInfo],
    changes: ChangeSet,
    reference_files: Sequence[Path],
) -> GateOutcome:
    """Fail on in-scope functions that no reference file mentions."""
    root = root.resolve()
    file_lines = _FileLines(root)
    removed = removed_lines(root, changes) if changes.incremental else RemovedLines({})
    selected = select_candidates(functions_all, changes, removed, file_lines.get)
    candidates = [c for c in selected if exclusion_reason(c.function) is None]
    names = {search_name(candidate.function) for candidate in candidates}
    production, tests = split_by_tests(root, reference_files)
    index = build_reference_index(root, production, names)
    test_index = build_reference_index(root, tests, names)
    verdicts: list[Verdict] = []
    for candidate in candidates:
        lines = file_lines.get(candidate.function.path)
        if reference_count(index, candidate.function, lines) == 0:
            verdict = judge(candidate, lines, changes.incremental)
            if test_index.counts.get(search_name(candidate.function), 0):
                verdict = dataclasses.replace(verdict, tests_only=True)
            verdicts.append(verdict)
    outcome = _outcome(candidates, verdicts, changes)
    outcome.measurements["excluded_functions"] = len(selected) - len(candidates)
    unreadable = tuple(sorted({*index.unreadable, *test_index.unreadable}))
    return _with_unreadable(outcome, unreadable)


class _FileLines:
    """Reads each function's file once."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, list[str]] = {}

    def get(self, path: str) -> list[str]:
        if path not in self._cache:
            try:
                text = source_files.read_text(self._root / path)
            except OSError:
                text = ""
            self._cache[path] = text.splitlines()
        return self._cache[path]


def _line(lines: Sequence[str], number: int) -> str:
    return lines[number - 1] if 0 < number <= len(lines) else ""


def _item(verdict: Verdict) -> GateItem:
    function = verdict.candidate.function
    value = "0"
    if verdict.tests_only:
        value = "0 (only tests call it)"
    elif verdict.candidate.reason == ORPHAN:
        value = "0 (the change removed its last reference)"
    return GateItem(
        path=function.path,
        line=function.start_line,
        name=function.name,
        metric="references",
        value=value,
        limit=">= 1",
    )


def _note(verdict: Verdict) -> str:
    function = verdict.candidate.function
    found = "only tests call it" if verdict.tests_only else "no references found"
    return (
        f"{function.path}:{function.start_line} {function.name}: {found}, "
        f"not failed ({verdict.note})."
    )


def _outcome(
    candidates: Sequence[Candidate], verdicts: Sequence[Verdict], changes: ChangeSet
) -> GateOutcome:
    failing = [verdict for verdict in verdicts if verdict.fails]
    notes = [verdict for verdict in verdicts if not verdict.fails]
    items = sorted(
        (_item(verdict) for verdict in failing),
        key=lambda item: (item.path, item.line or 0),
    )
    outcome = outcome_for_items(
        KEY,
        TITLE,
        items,
        _passing_summary(candidates, changes),
        f"Functions that no production code calls: {len(items)}.",
        measurements=_measurements(candidates, failing, notes),
    )
    outcome.details.extend(sorted(_note(verdict) for verdict in notes))
    return outcome


def _with_unreadable(outcome: GateOutcome, unreadable: Sequence[str]) -> GateOutcome:
    """List unreadable reference files; with failures, the result is BLOCKED
    because the missing text may hold the callers."""
    if not unreadable:
        return outcome
    outcome.details.extend(
        f"Could not read reference file {path}." for path in unreadable
    )
    if outcome.items:
        outcome.status = BLOCKED
        outcome.summary = (
            f"{plural(len(unreadable), 'reference file')} could not be read, so "
            f"{plural(len(outcome.items), 'apparently unused function')} cannot "
            "be confirmed."
        )
    return outcome


def _passing_summary(candidates: Sequence[Candidate], changes: ChangeSet) -> str:
    if not candidates:
        if changes.incremental:
            return "The change adds no functions and removes no callers."
        return "No functions to check."
    count = len(candidates)
    return f"Checked functions with a production caller: {count} of {count}."


def _measurements(
    candidates: Sequence[Candidate],
    failing: Sequence[Verdict],
    notes: Sequence[Verdict],
) -> dict[str, Any]:
    reasons = Counter(candidate.reason for candidate in candidates)
    return {
        "functions_checked": len(candidates),
        "new_functions": reasons[NEW],
        "orphan_candidates": reasons[ORPHAN],
        "unused": len(failing),
        "unreferenced_not_failed": len(notes),
    }
