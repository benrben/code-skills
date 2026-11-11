"""Where function names occur: the reference search behind the dead-code gate.

Only production files count as callers. Test files are indexed separately so
the gate can say "only tests call it" instead of passing the function.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

import source_files

_IDENTIFIER = re.compile(r"[\w$]+")

C_FAMILY = frozenset({"C", "C/C++", "C++", "Objective-C"})
# The words before a declared name: types, qualifiers, ``::`` scopes, pointer
# and reference marks, and template brackets. No ``=``, ``(``, ``.``, ``->``.
_C_DECLARATOR_PREFIX = re.compile(
    r"(?:(?:~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*(?:::)?|[*&<>,])\s*)+"
)
_C_STATEMENT_WORDS = frozenset(
    "return else case goto throw delete new sizeof co_return co_yield co_await".split()
)

_NON_CODE_SUFFIXES = frozenset(
    {".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc", ".org"}
    | {".csv", ".tsv", ".log", ".svg", ".map", ".lock", ".sum"}
)
_LOCKFILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "bun.lockb"}
)
_PROSE_STEMS = frozenset(
    {"license", "licence", "copying", "notice", "authors", "changelog"}
)


# Decorators, annotations and attributes that may register a function with a
# framework, spelled per language so that a JavaScript array literal or a Ruby
# ``@ivar`` assignment is not mistaken for one.
@dataclasses.dataclass
class ReferenceIndex:
    """How often each candidate name occurs across the reference files."""

    counts: Counter[str]
    indexed_paths: frozenset[str]
    unreadable: tuple[str, ...] = ()
    # name -> path -> occurrences on C-family declaration lines (prototypes),
    # which name a function without calling it.
    declarations: dict[str, Counter[str]] = dataclasses.field(default_factory=dict)

    def declared_elsewhere(self, name: str, path: str) -> int:
        found = self.declarations.get(name, Counter())
        return sum(count for where, count in found.items() if where != path)


def is_reference_path(relative: str) -> bool:
    """Whether a file can call code: source, templates, scripts, configuration."""
    path = PurePosixPath(relative.lower())
    if path.suffix in source_files.SOURCE_EXTENSIONS:
        return True
    if path.suffix in _NON_CODE_SUFFIXES or path.name in _LOCKFILES:
        return False
    return path.stem not in _PROSE_STEMS


def discover_reference_files(root: Path) -> list[Path]:
    """Every code-bearing text file, production and tests alike, as absolute
    paths. ``evaluate_dead_code`` counts only production files as callers."""
    root = root.resolve()
    return [
        path
        for path in source_files.text_files(root)
        if is_reference_path(source_files.normalize_path(path, root))
    ]


def identifiers(text: str) -> list[str]:
    return _IDENTIFIER.findall(text)


def name_position(text: str, name: str) -> int | None:
    """Where ``name`` first occurs as a whole identifier in ``text``."""
    match = re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text)
    return match.start() if match else None


def is_c_declaration(line: str, name: str) -> bool:
    """True when ``line`` declares ``name`` (``int f(int);``, ``static
    char *f(void)``) rather than calling it (``f(x);``, ``y = f(x);``,
    ``return f(x);``, ``Widget w(f(3));``, ``out << f(x);``)."""
    position = name_position(line, name)
    if position is None:
        return False
    prefix = line[:position].strip()
    after = line[position + len(name) :].lstrip()
    return (
        after.startswith("(")
        and bool(prefix)
        and "<<" not in prefix
        and prefix.split()[0] not in _C_STATEMENT_WORDS
        and _C_DECLARATOR_PREFIX.fullmatch(prefix) is not None
    )


def _c_declarations(
    text: str, wanted: frozenset[str], relative: str, into: dict[str, Counter[str]]
) -> None:
    for line in text.splitlines():
        for name in set(identifiers(line)) & wanted:
            if is_c_declaration(line, name):
                into.setdefault(name, Counter())[relative] += identifiers(line).count(
                    name
                )


def build_reference_index(
    root: Path, files: Iterable[Path], names: Iterable[str]
) -> ReferenceIndex:
    """Count whole-identifier occurrences of ``names`` in ``files``.

    Relative paths are taken relative to ``root``. Files that cannot be read
    are listed, because a missing caller would turn into a false failure.
    """
    root = root.resolve()
    wanted = frozenset(names)
    counts: Counter[str] = Counter()
    indexed: set[str] = set()
    unreadable: list[str] = []
    declarations: dict[str, Counter[str]] = {}
    for path in files:
        absolute = path if path.is_absolute() else root / path
        relative = source_files.normalize_path(absolute, root)
        try:
            text = source_files.read_text(absolute)
        except OSError:
            unreadable.append(relative)
            continue
        indexed.add(relative)
        counts.update(name for name in identifiers(text) if name in wanted)
        if source_files.language_for(relative) in C_FAMILY:
            _c_declarations(text, wanted, relative, declarations)
    return ReferenceIndex(
        counts, frozenset(indexed), tuple(sorted(unreadable)), declarations
    )


def split_by_tests(root: Path, files: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """Production files and test files. A call from a test does not make a
    function used: production code must call it."""
    root = root.resolve()
    production: list[Path] = []
    tests: list[Path] = []
    for path in files:
        absolute = path if path.is_absolute() else root / path
        relative = source_files.normalize_path(absolute, root)
        (tests if source_files.is_test_path(relative) else production).append(path)
    return production, tests
