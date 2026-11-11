"""File size gate: production files must stay at or below a physical line limit.

In repository scope every production file over the limit fails. In an
incremental scope the gate judges the change, not the history: a changed file
fails when the change pushes it over the limit, or when it was already over
the limit and the change made it longer. A file that was already too long and
did not grow passes with a note, so an agent is never blocked by size debt it
did not add.

Two Git details matter here. ``git diff`` C-quotes unusual file names
(non-ASCII, ``"``, ``\\``), so change paths are decoded before they are matched
with real files; otherwise such a file would silently drop out of scope. And an
unstaged ``mv`` shows up as a deleted path plus an untracked file, so a new file
whose content matches a deleted one is judged as that file moved.
"""

from __future__ import annotations

import dataclasses
import io
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import diff_scope
import source_files
from gate_types import BLOCKED, PASS, GateItem, GateOutcome, outcome_for_items, plural

KEY = "file_loc"
TITLE = "File size"

# Git's own rename detection treats files that share half their lines as one.
MOVE_SIMILARITY = 0.5

_GIT_ESCAPE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)
_GIT_NAMED_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


@dataclasses.dataclass(frozen=True)
class FileSize:
    """One production file's line count now and, in incremental scope, before
    the change. ``base_lines`` is None when the file is new or the scope is the
    whole repository."""

    path: str
    lines: int
    base_lines: int | None
    is_new: bool = False
    moved_from: str | None = None

    def over(self, limit: int) -> bool:
        return self.lines > limit

    def grew(self) -> bool:
        return self.base_lines is not None and self.lines > self.base_lines

    def describe_value(self) -> str:
        """The item value: ``now (was base)`` when a base exists."""
        if self.is_new:
            return f"{self.lines} (new file)"
        if self.base_lines is None:
            return str(self.lines)
        if self.moved_from is not None:
            return f"{self.lines} (was {self.base_lines} as {self.moved_from})"
        return f"{self.lines} (was {self.base_lines})"


@dataclasses.dataclass(frozen=True)
class Measured:
    """Every in-scope file that could be measured, and the ones that could not
    be read (with the reason), which make the gate BLOCKED rather than PASS."""

    sizes: list[FileSize]
    unreadable: list[tuple[str, str]]


def unquote_git_path(value: str) -> str:
    """Undo Git's C-style path quoting (``caf\\303\\251.py`` -> ``café.py``).

    ``diff_scope`` strips the quotes but keeps the escapes. Git always quotes a
    name that holds a backslash, so any backslash left here starts an escape;
    unknown escapes are kept as written.
    """
    if "\\" not in value:
        return value
    decoded = bytearray()
    position = 0
    for match in _GIT_ESCAPE.finditer(value):
        decoded += value[position : match.start()].encode()
        decoded += _escape_bytes(match)
        position = match.end()
    decoded += value[position:].encode()
    return decoded.decode("utf-8", errors="replace")


def _escape_bytes(match: re.Match[str]) -> bytes:
    token = match.group(1)
    if len(token) == 3:
        return bytes([int(token, 8)])
    if token in _GIT_NAMED_ESCAPES:
        return bytes([_GIT_NAMED_ESCAPES[token]])
    return match.group(0).encode()


def decode_change_paths(changes: diff_scope.ChangeSet) -> diff_scope.ChangeSet:
    """The same change set keyed by real file names, so quoted names stay in
    scope and their base text can still be read from Git."""
    if not changes.incremental:
        return changes
    files = {
        unquote_git_path(key): dataclasses.replace(
            change,
            path=unquote_git_path(change.path),
            old_path=None
            if change.old_path is None
            else unquote_git_path(change.old_path),
        )
        for key, change in changes.files.items()
    }
    deleted = frozenset(unquote_git_path(path) for path in changes.deleted)
    return dataclasses.replace(changes, files=files, deleted=deleted)


def text_line_count(text: str) -> int:
    """Count lines the way ``source_files.physical_line_count`` reads a file
    (universal newlines), so the base and current counts are comparable."""
    return sum(1 for _ in io.StringIO(text, newline=None))


def measure_file(
    root: Path, path: Path, changes: diff_scope.ChangeSet
) -> FileSize | None:
    """Measure one production file; None when the change scope excludes it or
    it no longer exists. Raises OSError when the file cannot be read."""
    relative = source_files.normalize_path(path, root)
    if relative in changes.deleted or not changes.includes(relative):
        return None
    if not path.is_file():
        return None
    lines = source_files.physical_line_count(path)
    if not changes.incremental:
        return FileSize(relative, lines, None)
    base = changes.base_text(root, relative)
    if base is None:
        return FileSize(relative, lines, None, is_new=True)
    return FileSize(relative, lines, text_line_count(base))


def violates(size: FileSize, limit: int, incremental: bool) -> bool:
    """Repository scope: over the limit. Incremental scope: crossed the limit
    (base at or below it, or a new file) or was over and grew."""
    if not size.over(limit):
        return False
    if not incremental or size.base_lines is None:
        return True
    return size.base_lines <= limit or size.grew()


def similarity(old_text: str, new_text: str) -> float:
    """Share of lines two texts have in common, relative to the longer one."""
    old_lines = Counter(old_text.splitlines())
    new_lines = Counter(new_text.splitlines())
    longest = max(sum(old_lines.values()), sum(new_lines.values()))
    if longest == 0:
        return 1.0
    return sum((old_lines & new_lines).values()) / longest


def _move_source(
    new_text: str, deleted_texts: dict[str, str]
) -> tuple[str, str] | None:
    """The deleted file most like ``new_text``, when it is similar enough to
    count as the same file moved."""
    scored = [
        (similarity(old_text, new_text), path, old_text)
        for path, old_text in sorted(deleted_texts.items())
    ]
    best = max(scored, default=None, key=lambda entry: entry[0])
    if best is None or best[0] < MOVE_SIMILARITY:
        return None
    return best[1], best[2]


def _deleted_base_texts(root: Path, changes: diff_scope.ChangeSet) -> dict[str, str]:
    if changes.base is None:
        return {}
    texts = {
        path: diff_scope.git_show(root, changes.base, path)
        for path in sorted(changes.deleted)
    }
    return {path: text for path, text in texts.items() if text is not None}


def recognize_moves(
    root: Path, sizes: Sequence[FileSize], changes: diff_scope.ChangeSet, limit: int
) -> list[FileSize]:
    """Judge a new oversized file that matches a deleted file as that file
    moved, so moving size debt is not mistaken for adding it.

    Git only pairs moves it can see in the index; an unstaged ``mv`` arrives
    as a deletion plus an untracked file.
    """
    candidates = [size for size in sizes if size.is_new and size.over(limit)]
    if not candidates or not changes.deleted:
        return list(sizes)
    deleted_texts = _deleted_base_texts(root, changes)
    return [
        _as_move(root, size, deleted_texts) if size in candidates else size
        for size in sizes
    ]


def _as_move(root: Path, size: FileSize, deleted_texts: dict[str, str]) -> FileSize:
    try:
        new_text = source_files.read_text(root / size.path)
    except OSError:
        return size
    source = _move_source(new_text, deleted_texts)
    if source is None:
        return size
    old_path, old_text = source
    return dataclasses.replace(
        size,
        base_lines=text_line_count(old_text),
        is_new=False,
        moved_from=old_path,
    )


def _item(size: FileSize, limit: int) -> GateItem:
    return GateItem(
        path=size.path,
        line=None,
        name="",
        metric="lines",
        value=size.describe_value(),
        limit=str(limit),
    )


def _summaries(
    failures: int, checked: int, limit: int, incremental: bool
) -> tuple[str, str]:
    if incremental:
        return (
            f"No changed production file crossed or grew past {limit} lines "
            f"({checked} checked).",
            f"{failures} of {plural(checked, 'changed production file')} crossed "
            "or grew "
            f"past {limit} lines.",
        )
    return (
        f"Production files at or below {limit} lines: {checked} of {checked}.",
        f"Production files over {limit} lines: {failures} of {checked}.",
    )


def _debt_notes(sizes: Sequence[FileSize], limit: int) -> list[str]:
    """Files that were already over the limit and did not grow: reported, not
    failed."""
    return [
        f"{size.path} is over the limit ({size.describe_value()} lines, limit "
        f"{limit}) but the change did not make it longer."
        for size in sizes
        if size.over(limit) and not violates(size, limit, incremental=True)
    ]


def evaluate_file_size(
    root: Path,
    files: Sequence[Path],
    changes: diff_scope.ChangeSet,
    max_lines: int,
) -> GateOutcome:
    """Judge production ``files`` (absolute, or relative to ``root``) against
    ``max_lines`` within the ``changes`` scope."""
    changes = decode_change_paths(changes)
    measured = measure_files(root, files, changes)
    sizes = recognize_moves(root, measured.sizes, changes, max_lines)
    failing = _failures(sizes, max_lines, changes.incremental)
    passing, failing_summary = _summaries(
        len(failing), len(sizes), max_lines, changes.incremental
    )
    items = [_item(size, max_lines) for size in failing]
    outcome = outcome_for_items(
        KEY,
        TITLE,
        items,
        passing,
        failing_summary,
        measurements=_measurements(sizes, max_lines),
    )
    if changes.incremental:
        outcome.details.extend(_debt_notes(sizes, max_lines))
    if measured.unreadable:
        _block_on_unreadable(outcome, measured.unreadable)
    return outcome


def _block_on_unreadable(
    outcome: GateOutcome, unreadable: Sequence[tuple[str, str]]
) -> None:
    """A file the gate could not read was never measured, so a run that found
    nothing else wrong is BLOCKED, not PASS. Real failures stay FAIL."""
    names = ", ".join(path for path, _ in unreadable)
    outcome.details.extend(
        f"Could not read {path}: {reason}" for path, reason in unreadable
    )
    outcome.measurements["unreadable"] = [path for path, _ in unreadable]
    if outcome.status == PASS:
        outcome.status = BLOCKED
        outcome.summary = (
            f"Could not read {plural(len(unreadable), 'production file')}: {names}."
        )


def measure_files(
    root: Path, files: Sequence[Path], changes: diff_scope.ChangeSet
) -> Measured:
    """Measure every in-scope file, largest first, and collect the files that
    could not be read instead of crashing on them."""
    sizes: list[FileSize] = []
    unreadable: list[tuple[str, str]] = []
    for path in files:
        try:
            size = measure_file(root, root / path, changes)
        except OSError as error:
            relative = source_files.normalize_path(root / path, root)
            unreadable.append((relative, error.strerror or str(error)))
            continue
        if size is not None:
            sizes.append(size)
    sizes.sort(key=lambda size: (-size.lines, size.path))
    return Measured(sizes, unreadable)


def _failures(
    sizes: Sequence[FileSize], limit: int, incremental: bool
) -> list[FileSize]:
    return [size for size in sizes if violates(size, limit, incremental)]


def _measurements(sizes: Sequence[FileSize], limit: int) -> dict[str, Any]:
    """Every checked file with its counts, for the report's file size table."""
    rows = [dataclasses.asdict(size) for size in sizes]
    return {"max_lines": limit, "files_checked": len(sizes), "files": rows}
