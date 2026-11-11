"""Line-level change sets: which lines an agent added and which lines it removed.

Generic gates judge the change, not the history. ``ChangeSet`` records, per
file, the line numbers that exist in the new version because of the change and
the text of lines the change removed. A repository-wide change set treats
every line as in scope.

Line numbers always refer to the current working-tree file. For ``--commit``
scope the working tree is expected to match the commit, as elsewhere in the
gate.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

REPOSITORY = "repository"
LOCAL_CHANGES = "local_changes"
COMMIT = "commit"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclasses.dataclass(frozen=True)
class FileChange:
    """Changes to one file; ``added`` holds new-file line numbers."""

    path: str
    added: frozenset[int] = frozenset()
    removed: tuple[str, ...] = ()
    old_path: str | None = None
    is_new: bool = False
    all_lines: bool = False


@dataclasses.dataclass(frozen=True)
class ChangeSet:
    """The lines a change touched, or the whole repository."""

    kind: str
    base: str | None = None
    files: Mapping[str, FileChange] = dataclasses.field(default_factory=dict)
    deleted: frozenset[str] = frozenset()

    @property
    def incremental(self) -> bool:
        return self.kind != REPOSITORY

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.files))

    def includes(self, path: str) -> bool:
        return not self.incremental or path in self.files

    def is_new_file(self, path: str) -> bool:
        change = self.files.get(path)
        return bool(change and change.is_new)

    def is_added(self, path: str, line: int) -> bool:
        if not self.incremental:
            return True
        change = self.files.get(path)
        if change is None:
            return False
        return change.all_lines or line in change.added

    def touches(self, path: str, start: int, end: int) -> bool:
        """True when any line in ``start..end`` (inclusive) was added or changed."""
        if not self.incremental:
            return True
        change = self.files.get(path)
        if change is None:
            return False
        if change.all_lines:
            return True
        return any(start <= line <= end for line in change.added)

    def added_lines(self, path: str, line_count: int) -> frozenset[int]:
        """Added line numbers, expanding whole-file changes to ``1..line_count``."""
        change = self.files.get(path)
        if not self.incremental or (change is not None and change.all_lines):
            return frozenset(range(1, line_count + 1))
        return change.added if change is not None else frozenset()

    def removed_text(self) -> Iterable[tuple[str, str]]:
        for path, change in self.files.items():
            for line in change.removed:
                yield path, line

    def base_path(self, path: str) -> str:
        change = self.files.get(path)
        return change.old_path if change and change.old_path else path

    def base_text(self, root: Path, path: str) -> str | None:
        """The file's text before the change, or None when it did not exist."""
        if not self.incremental or self.base is None or self.is_new_file(path):
            return None
        return git_show(root, self.base, self.base_path(path))


def repository_changes() -> ChangeSet:
    return ChangeSet(REPOSITORY)


def local_changes(root: Path) -> ChangeSet:
    """Staged, unstaged, and untracked changes compared with HEAD."""
    base = _revision(root, "HEAD")
    tree = base or _EMPTY_TREE
    diff = _git(root, ["diff", "--no-color", "--no-ext-diff", "-M", "-U0", tree])
    files = dict(parse_unified_diff(diff))
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    for raw in untracked.split("\0"):
        if raw:
            path = Path(raw).as_posix()
            files[path] = FileChange(path, is_new=True, all_lines=True)
    deleted = frozenset(path for path, change in files.items() if _deleted(change))
    kept = {path: change for path, change in files.items() if path not in deleted}
    return ChangeSet(LOCAL_CHANGES, base, kept, deleted)


def commit_changes(root: Path, reference: str = "HEAD") -> ChangeSet:
    """The changes introduced by one commit compared with its first parent."""
    revision = _revision(root, reference)
    if revision is None:
        raise ValueError(f"Git commit does not exist: {reference}")
    parent = _revision(root, f"{revision}^1")
    diff = _git(
        root,
        [
            "diff",
            "--no-color",
            "--no-ext-diff",
            "-M",
            "-U0",
            parent or _EMPTY_TREE,
            revision,
        ],
    )
    files = dict(parse_unified_diff(diff))
    deleted = frozenset(path for path, change in files.items() if _deleted(change))
    kept = {path: change for path, change in files.items() if path not in deleted}
    return ChangeSet(COMMIT, parent, kept, deleted)


_DELETED_MARKER = "\0deleted"


def _deleted(change: FileChange) -> bool:
    return change.old_path == _DELETED_MARKER


def parse_unified_diff(text: str) -> Iterable[tuple[str, FileChange]]:
    """Parse ``git diff -U0`` output into per-file changes.

    Deleted files are reported with a marker that the callers drop. Binary
    files are reported as whole-file changes without line details.
    """
    for block in _file_blocks(text):
        change = _parse_block(block)
        if change is not None:
            yield change.path, change


def _file_blocks(text: str) -> Iterable[list[str]]:
    block: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git ") and block:
            yield block
            block = []
        block.append(line)
    if block:
        yield block


def _parse_block(lines: list[str]) -> FileChange | None:
    parser = _BlockParser()
    for line in lines:
        parser.feed(line)
    return parser.result()


class _BlockParser:
    """Reads one file's section of ``git diff -U0`` output, line by line."""

    def __init__(self) -> None:
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.rename_from: str | None = None
        self.is_new = False
        self.is_deleted = False
        self.is_binary = False
        self.added: set[int] = set()
        self.removed: list[str] = []
        self.next_line = 0
        self.in_hunk = False

    def feed(self, line: str) -> None:
        if self.in_hunk and line[:1] in {"+", "-", " ", "\\"}:
            self._hunk_line(line)
            return
        hunk = _HUNK.match(line)
        self.in_hunk = hunk is not None
        if hunk:
            self.next_line = int(hunk.group(3))
            return
        for prefix, handle in self._HEADERS:
            if line.startswith(prefix):
                handle(self, line.removeprefix(prefix))
                return

    def _hunk_line(self, line: str) -> None:
        if line.startswith("-"):
            self.removed.append(line[1:])
            return
        if line.startswith("+"):
            self.added.add(self.next_line)
        if line[:1] in {"+", " "}:
            self.next_line += 1

    def _new_file(self, _: str) -> None:
        self.is_new = True

    def _deleted_file(self, _: str) -> None:
        self.is_deleted = True

    def _rename_from(self, value: str) -> None:
        self.rename_from = value

    def _rename_to(self, value: str) -> None:
        self.new_path = value

    def _old_header(self, value: str) -> None:
        self.old_path = _diff_path(value)

    def _new_header(self, value: str) -> None:
        self.new_path = _diff_path(value) or self.new_path

    def _binary(self, _: str) -> None:
        self.is_binary = True

    def _diff_header(self, value: str) -> None:
        if self.new_path is None:
            self.new_path = _header_path("diff --git " + value)

    _HEADERS = (
        ("new file mode", _new_file),
        ("deleted file mode", _deleted_file),
        ("rename from ", _rename_from),
        ("rename to ", _rename_to),
        ("--- ", _old_header),
        ("+++ ", _new_header),
        ("Binary files ", _binary),
        ("diff --git ", _diff_header),
    )

    def result(self) -> FileChange | None:
        path = self.new_path or self.old_path
        if path is None:
            return None
        if self.is_deleted:
            return FileChange(path, old_path=_DELETED_MARKER)
        old_path = self.old_path
        if self.rename_from and self.rename_from != path:
            old_path = self.rename_from
        elif old_path == path:
            old_path = None
        return FileChange(
            path,
            frozenset(self.added),
            tuple(self.removed),
            old_path,
            is_new=self.is_new,
            all_lines=self.is_new or self.is_binary,
        )


def _diff_path(value: str) -> str | None:
    value = value.strip().strip('"')
    if value == "/dev/null":
        return None
    if value[:2] in {"a/", "b/"}:
        value = value[2:]
    return value


def _header_path(line: str) -> str | None:
    parts = line.split(" b/", 1)
    return parts[1].strip('"') if len(parts) == 2 else None


def git_show(root: Path, revision: str, path: str) -> str | None:
    if not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{revision}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _revision(root: Path, reference: str) -> str | None:
    if not shutil.which("git"):
        raise ValueError("Git is required for an incremental quality-gate scope.")
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", reference],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"},
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise ValueError(message or "Git could not determine the changes.")
    return result.stdout.decode("utf-8", errors="replace")
