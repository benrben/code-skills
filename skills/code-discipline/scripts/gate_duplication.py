"""Duplication gate: token-based clone detection that works for any language.

``gate_duplication_tokens`` turns each production file into normalized
Pygments tokens, so renamed or reformatted copies compare equal (a "Type-2"
clone). Equal runs of ``min_tokens`` tokens are found with rolling-hash
windows, grouped by hash, and extended to the longest common run; nothing is
compared pairwise file by file.

The corpus is every production file, so new code that copies old code is
caught. In an incremental scope only clones with at least one side touching an
added line are reported; in repository scope every clone is.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import diff_scope
import source_files
from gate_duplication_tokens import (
    DuplicationToolError,
    Token,
    TokenizedSources,
    code_line_flags,
    has_body_line,
    indented_lines,
    tokenize_sources,
    whole_lines,
)
from gate_types import (
    BLOCKED,
    GateItem,
    GateOutcome,
    ToolPython,
    outcome_for_items,
    plural,
)

__all__ = [
    "Clone",
    "DuplicationLimits",
    "DuplicationToolError",
    "blocked_outcome",
    "detect_clones",
    "evaluate_duplication",
    "find_clones",
    "merge_overlapping",
    "relevant_clones",
    "run_duplication",
    "unmeasured_outcome",
]

KEY = "duplication"
TITLE = "Duplication"

DEFAULT_MIN_TOKENS = 50
DEFAULT_MIN_LINES = 5
DEFAULT_MAX_NEW_CLONES = 0
ITEM_LIMIT = 50


# Buckets with more occurrences than this pair neighbors only (see
# ``_candidate_pairs``), so heavily repeated code cannot make pairing
# quadratic.
_MAX_ALL_PAIRS_BUCKET = 16
_HASH_BASE = 1_000_003
_HASH_MODULUS = (1 << 61) - 1

# A clone must hold at least this share of ``min_tokens`` on code lines, so
# a table with a one-line header is not reported, while a copied module that
# also contains tables still is.
MIN_CODE_SHARE_OF_WINDOW = 0.5


@dataclasses.dataclass(frozen=True)
class Clone:
    """Two code regions with the same normalized tokens; 1-based inclusive
    line ranges."""

    a_path: str
    a_start: int
    a_end: int
    b_path: str
    b_start: int
    b_end: int
    tokens: int

    def touches(self, changes: diff_scope.ChangeSet) -> bool:
        return changes.touches(
            self.a_path, self.a_start, self.a_end
        ) or changes.touches(self.b_path, self.b_start, self.b_end)

    def oriented(self, changes: diff_scope.ChangeSet) -> Clone:
        """The clone with a side the change touched first, so reports name the
        new copy and point at the code it repeats. Call it only for clones
        that ``touches`` the change."""
        if changes.touches(self.a_path, self.a_start, self.a_end):
            return self
        return Clone(
            self.b_path,
            self.b_start,
            self.b_end,
            self.a_path,
            self.a_start,
            self.a_end,
            self.tokens,
        )


# --- Clone detection -----------------------------------------------------


class _Stream(NamedTuple):
    path: str
    ids: list[int]
    lines: list[int]
    # code_before[i] = tokens on code lines among the first i tokens.
    code_before: list[int]

    def code_tokens(self, position: int, length: int) -> int:
        return self.code_before[position + length] - self.code_before[position]


class _Occurrence(NamedTuple):
    stream: int
    position: int


class _Match(NamedTuple):
    first: _Occurrence
    second: _Occurrence
    length: int


def detect_clones(
    sources: Mapping[str, Sequence[Token]], min_tokens: int, min_lines: int
) -> list[Clone]:
    """Find maximal runs of at least ``min_tokens`` equal tokens that span at
    least ``min_lines`` lines on each side, one clone per copied region."""
    streams = _encode(sources)
    finder = _MatchFinder(streams, min_tokens)
    found: list[Clone] = []
    for occurrences in _window_buckets(streams, min_tokens):
        for first, second in _candidate_pairs(occurrences):
            match = finder.match(first, second)
            if match is not None and _has_enough_code(streams, match, min_tokens):
                clone = _clone(streams, match, min_tokens, min_lines)
                found.extend([] if clone is None else [clone])
    return sorted(merge_overlapping(found), key=_clone_order)


def _clone_order(clone: Clone) -> tuple[str, int, str, int]:
    return (clone.a_path, clone.a_start, clone.b_path, clone.b_start)


def merge_overlapping(clones: Sequence[Clone]) -> list[Clone]:
    """Keep one clone per copied region. Repeated statement shapes inside a
    copy also match at shifted offsets; such a clone overlaps a longer one on
    both sides, so only the longest is kept."""
    kept: dict[tuple[str, str], list[Clone]] = {}
    for clone in sorted(clones, key=lambda item: (-item.tokens, _clone_order(item))):
        same_files = kept.setdefault((clone.a_path, clone.b_path), [])
        if not any(_overlaps_on_both_sides(clone, other) for other in same_files):
            same_files.append(clone)
    return [clone for group in kept.values() for clone in group]


def _overlaps_on_both_sides(one: Clone, other: Clone) -> bool:
    return _ranges_overlap(
        one.a_start, one.a_end, other.a_start, other.a_end
    ) and _ranges_overlap(one.b_start, one.b_end, other.b_start, other.b_end)


def _ranges_overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start <= other_end and other_start <= end


def _encode(sources: Mapping[str, Sequence[Token]]) -> list[_Stream]:
    """Map token texts to small integers so comparisons and hashing are cheap."""
    vocabulary: dict[str, int] = {}
    streams: list[_Stream] = []
    for path in sorted(sources):
        tokens = sources[path]
        ids = [vocabulary.setdefault(token.text, len(vocabulary)) for token in tokens]
        lines = [token.line for token in tokens]
        code = itertools.accumulate(code_line_flags(tokens), initial=0)
        streams.append(_Stream(path, ids, lines, list(code)))
    return streams


def window_hashes(ids: Sequence[int], size: int) -> Iterator[tuple[int, int]]:
    """Yield ``(position, hash)`` for every window of ``size`` ids, updating a
    polynomial rolling hash in constant time per step."""
    if size <= 0 or len(ids) < size:
        return
    leading_weight = pow(_HASH_BASE, size - 1, _HASH_MODULUS)
    value = 0
    for token in ids[:size]:
        value = (value * _HASH_BASE + token + 1) % _HASH_MODULUS
    yield 0, value
    for position in range(1, len(ids) - size + 1):
        dropped = (ids[position - 1] + 1) * leading_weight
        added = ids[position + size - 1] + 1
        value = ((value - dropped) * _HASH_BASE + added) % _HASH_MODULUS
        yield position, value


def _window_buckets(
    streams: Sequence[_Stream], size: int
) -> Iterator[list[_Occurrence]]:
    """Group windows by hash; only groups with two or more occurrences matter.
    Occurrences stay in (file, position) order."""
    buckets: dict[int, list[_Occurrence]] = {}
    for index, stream in enumerate(streams):
        for position, value in window_hashes(stream.ids, size):
            buckets.setdefault(value, []).append(_Occurrence(index, position))
    return (group for group in buckets.values() if len(group) > 1)


def _candidate_pairs(
    occurrences: Sequence[_Occurrence],
) -> Iterator[tuple[_Occurrence, _Occurrence]]:
    """Every pair in small buckets. A large bucket comes from a long run of
    repeated statements or a block copied many times; there each occurrence
    is paired with the previous one in its file (one alignment covers a
    whole run), and each file's first occurrence with the previous file's,
    which keeps the work linear."""
    if len(occurrences) <= _MAX_ALL_PAIRS_BUCKET:
        yield from itertools.combinations(occurrences, 2)
        return
    firsts: list[_Occurrence] = [occurrences[0]]
    for previous, current in zip(occurrences, occurrences[1:]):
        if previous.stream == current.stream:
            yield previous, current
        else:
            firsts.append(current)
    yield from zip(firsts, firsts[1:])


class _MatchFinder:
    """Extends equal windows to maximal matches, once per alignment.

    Every window inside a long clone lands in some bucket; remembering the
    extended interval per (file, file, offset) alignment keeps the work linear
    in the clone length instead of quadratic.
    """

    def __init__(self, streams: Sequence[_Stream], min_tokens: int) -> None:
        self._streams = streams
        self._min_tokens = min_tokens
        self._covered: dict[tuple[int, int, int], list[tuple[int, int]]] = {}

    def match(self, first: _Occurrence, second: _Occurrence) -> _Match | None:
        alignment = (first.stream, second.stream, second.position - first.position)
        intervals = self._covered.setdefault(alignment, [])
        if any(start <= first.position < end for start, end in intervals):
            return None
        match = self._extend(first, second)
        intervals.append((match.first.position, match.first.position + match.length))
        if match.length < self._min_tokens:  # only after a hash collision
            return None
        if first.stream == second.stream:
            return self._separate_regions(match)
        return match

    def _separate_regions(self, match: _Match) -> _Match | None:
        """A match inside one file. When everything from the first occurrence
        to the end of the second repeats a unit shorter than ``min_tokens``
        (an argument list, a switch, a run of assignments), the two
        occurrences are one region of similar statements, not a copy.
        Otherwise the match is cut where it would overlap itself."""
        ids = self._streams[match.first.stream].ids
        start = match.first.position
        end = match.second.position + match.length
        period = smallest_period(ids[start : start + match.length])
        if (
            period < self._min_tokens
            and ids[start : end - period] == ids[start + period : end]
        ):
            return None
        gap = match.second.position - match.first.position
        match = match._replace(length=min(match.length, gap))
        return match if match.length >= self._min_tokens else None

    def _extend(self, first: _Occurrence, second: _Occurrence) -> _Match:
        a = self._streams[first.stream].ids
        b = self._streams[second.stream].ids
        back = 0
        while (
            first.position - back > 0
            and second.position - back > 0
            and a[first.position - back - 1] == b[second.position - back - 1]
        ):
            back += 1
        forward = 0
        while (
            first.position + forward < len(a)
            and second.position + forward < len(b)
            and a[first.position + forward] == b[second.position + forward]
        ):
            forward += 1
        return _Match(
            _Occurrence(first.stream, first.position - back),
            _Occurrence(second.stream, second.position - back),
            back + forward,
        )


def smallest_period(ids: Sequence[int]) -> int:
    """The smallest ``p`` with ``ids[i] == ids[i + p]`` for every ``i``
    (Knuth-Morris-Pratt failure function)."""
    border = [0] * len(ids)
    for index in range(1, len(ids)):
        length = border[index - 1]
        while length and ids[index] != ids[length]:
            length = border[length - 1]
        border[index] = length + 1 if ids[index] == ids[length] else length
    return len(ids) - border[-1] if ids else 0


def _clone(
    streams: Sequence[_Stream], match: _Match, min_tokens: int, min_lines: int
) -> Clone | None:
    a = streams[match.first.stream]
    b = streams[match.second.stream]
    skip, length = whole_lines(
        (a.lines, match.first.position), (b.lines, match.second.position), match.length
    )
    if length < min_tokens:  # a copy inside long lines keeps its exact tokens
        skip, length = 0, match.length
    a_start, a_end = _line_span(a, match.first.position + skip, length)
    b_start, b_end = _line_span(b, match.second.position + skip, length)
    if min(a_end - a_start, b_end - b_start) + 1 < min_lines:
        return None
    return Clone(a.path, a_start, a_end, b.path, b_start, b_end, length)


def _has_enough_code(
    streams: Sequence[_Stream], match: _Match, min_tokens: int
) -> bool:
    """Data tables are not clones: the match needs code lines worth half a
    window. A longer match never has fewer code tokens than a part of it, so
    judging the maximal match loses no shorter clone."""
    stream = streams[match.first.stream]
    code = stream.code_tokens(match.first.position, match.length)
    return code >= min_tokens * MIN_CODE_SHARE_OF_WINDOW


def _line_span(stream: _Stream, position: int, length: int) -> tuple[int, int]:
    return stream.lines[position], stream.lines[position + length - 1]


def find_clones(
    tool_python: ToolPython,
    root: Path,
    files: Sequence[Path],
    min_tokens: int,
    min_lines: int,
) -> list[Clone]:
    """Tokenize every production file and return all clones among them.

    Raises ``DuplicationToolError`` when Pygments cannot run."""
    sources = tokenize_sources(tool_python, root, files)
    return detect_clones(sources.tokens, min_tokens, min_lines)


# --- Evaluation ----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DuplicationLimits:
    min_tokens: int = DEFAULT_MIN_TOKENS
    min_lines: int = DEFAULT_MIN_LINES
    max_new_clones: int = DEFAULT_MAX_NEW_CLONES

    @classmethod
    def from_thresholds(cls, thresholds: Mapping[str, Any]) -> DuplicationLimits:
        """Accept either the whole thresholds file or its ``duplication``
        section; missing values fall back to the defaults."""
        nested = thresholds.get(KEY)
        section = nested if isinstance(nested, Mapping) else thresholds
        return cls(
            int(section.get("min_tokens", DEFAULT_MIN_TOKENS)),
            int(section.get("min_lines", DEFAULT_MIN_LINES)),
            int(section.get("max_new_clones", DEFAULT_MAX_NEW_CLONES)),
        )


def relevant_clones(
    clones: Sequence[Clone], changes: diff_scope.ChangeSet
) -> list[Clone]:
    """Clones the change is responsible for, new copy first; every clone in
    repository scope."""
    return [clone.oriented(changes) for clone in clones if clone.touches(changes)]


def evaluate_duplication(
    clones: Sequence[Clone],
    changes: diff_scope.ChangeSet,
    thresholds: Mapping[str, Any],
    *,
    skipped: Sequence[tuple[str, str]] = (),
) -> GateOutcome:
    """FAIL when more clones touch the change than ``max_new_clones`` allows."""
    limits = DuplicationLimits.from_thresholds(thresholds)
    reported = relevant_clones(clones, changes)
    failing = len(reported) > limits.max_new_clones
    shown = reported[:ITEM_LIMIT] if failing else []
    outcome = outcome_for_items(
        KEY,
        TITLE,
        [_item(clone, limits) for clone in shown],
        _passing_summary(changes, limits, len(reported)),
        _failing_summary(changes, limits, len(reported)),
        measurements=_measurements(clones, reported, limits),
    )
    outcome.details.extend(_notes(reported, limits, failing))
    outcome.details.extend(
        f"Not analyzed ({reason}): {path}" for path, reason in skipped
    )
    return outcome


def _notes(
    reported: Sequence[Clone], limits: DuplicationLimits, failing: bool
) -> list[str]:
    """Clones within an allowance are listed as notes; past the item limit a
    failing outcome says how many more there are."""
    if not failing:
        return _allowed_details(reported, limits)
    hidden = len(reported) - ITEM_LIMIT
    return [f"... and {plural(hidden, 'more clone')}."] if hidden > 0 else []


def _allowed_details(reported: Sequence[Clone], limits: DuplicationLimits) -> list[str]:
    return [
        _item(clone, limits).describe() + " (allowed by max_new_clones)"
        for clone in reported[:ITEM_LIMIT]
    ]


def _passing_summary(
    changes: diff_scope.ChangeSet, limits: DuplicationLimits, count: int
) -> str:
    if count:
        return (
            f"{plural(count, 'duplicated block')} found, within the limit of "
            f"{limits.max_new_clones}."
        )
    if changes.incremental:
        return "The change adds no duplicated blocks."
    return (
        f"No duplicated blocks of {limits.min_tokens}+ tokens across "
        f"{limits.min_lines}+ lines."
    )


def _failing_summary(
    changes: diff_scope.ChangeSet, limits: DuplicationLimits, count: int
) -> str:
    where = "touching the change" if changes.incremental else "in the repository"
    return (
        f"{plural(count, 'duplicated block')} of {limits.min_tokens}+ tokens {where} "
        f"(limit {limits.max_new_clones})."
    )


def _item(clone: Clone, limits: DuplicationLimits) -> GateItem:
    lines = clone.a_end - clone.a_start + 1
    return GateItem(
        path=clone.a_path,
        line=clone.a_start,
        name=f"repeats {clone.b_path}:{clone.b_start}-{clone.b_end}",
        metric="duplicate block",
        value=f"{clone.tokens} tokens over {lines} lines",
        limit=f"under {limits.min_tokens} tokens or {limits.min_lines} lines",
    )


def _measurements(
    clones: Sequence[Clone], reported: Sequence[Clone], limits: DuplicationLimits
) -> dict[str, Any]:
    return {
        "clones": len(reported),
        "clones_total": len(clones),
        "duplicated_lines": sum(clone.a_end - clone.a_start + 1 for clone in reported),
        "min_tokens": limits.min_tokens,
        "min_lines": limits.min_lines,
        "max_new_clones": limits.max_new_clones,
        "pairs": [_pair_row(clone) for clone in reported[:ITEM_LIMIT]],
    }


def _pair_row(clone: Clone) -> dict[str, Any]:
    return {
        "first": {
            "path": clone.a_path,
            "start_line": clone.a_start,
            "end_line": clone.a_end,
        },
        "second": {
            "path": clone.b_path,
            "start_line": clone.b_start,
            "end_line": clone.b_end,
        },
        "tokens": clone.tokens,
        "line_count": clone.a_end - clone.a_start + 1,
    }


def blocked_outcome(reason: str) -> GateOutcome:
    """The gate could not measure duplication; never treated as a pass."""
    return GateOutcome(
        KEY, TITLE, BLOCKED, f"Duplication could not be measured: {reason}"
    )


def unmeasured_outcome(
    sources: TokenizedSources, files: Sequence[Path]
) -> GateOutcome | None:
    """BLOCKED when some files could not be read, or when files were given
    but none could be tokenized: a clone in them would go unseen."""
    unreadable = sources.unreadable()
    if unreadable:
        listed = ", ".join(unreadable[:5])
        return blocked_outcome(
            f"{plural(len(unreadable), 'file')} could not be read ({listed})."
        )
    if files and not sources.tokens:
        return blocked_outcome(
            "no file could be tokenized (no Pygments lexer for any of them)."
        )
    return None


def run_duplication(
    tool_python: ToolPython,
    root: Path,
    files: Sequence[Path],
    changes: diff_scope.ChangeSet,
    thresholds: Mapping[str, Any],
) -> GateOutcome:
    """Tokenize ``files`` (all production files), detect clones, and judge
    the ones the change touches. BLOCKED when Pygments cannot run or a file
    cannot be measured."""
    limits = DuplicationLimits.from_thresholds(thresholds)
    try:
        sources = tokenize_sources(tool_python, root, files)
    except DuplicationToolError as error:
        return blocked_outcome(str(error))
    unmeasured = unmeasured_outcome(sources, files)
    if unmeasured is not None:
        return unmeasured
    clones = detect_clones(sources.tokens, limits.min_tokens, limits.min_lines)
    logic = logic_clones(root, clones)
    return evaluate_duplication(logic, changes, thresholds, skipped=sources.skipped)


def logic_clones(root: Path, clones: Sequence[Clone]) -> list[Clone]:
    """Clones whose both copies reach into a body (see ``has_body_line``)."""
    indented: dict[str, frozenset[int]] = {}

    def body(path: str, start: int, end: int) -> bool:
        if path not in indented:
            indented[path] = indented_lines(source_files.read_text(root / path))
        return has_body_line(indented[path], start, end)

    return [
        clone
        for clone in clones
        if body(clone.a_path, clone.a_start, clone.a_end)
        and body(clone.b_path, clone.b_start, clone.b_end)
    ]
