"""The "No shortcuts" gate: catches the ways an agent can make checks pass
without fixing the code.

It reports four kinds of finding:

* production files hidden from the gates by the project's ``source`` settings;
* new suppression, coverage-exclusion, skip, or focus markers on added lines
  of any text file (the whole repository in repository scope);
* new silent error handlers in production code;
* edits to the project's quality policy files without user approval.

Changed check-configuration files are listed for review but never fail.

The marker table below contains every marker as a regular expression, so
this file and its test are exempt from the marker scan by file name.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import source_files
from diff_scope import ChangeSet
from gate_shortcuts_handlers import SilentHandler, silent_handlers
from gate_shortcuts_markers import (
    PYTHON,
    MarkerHit,
    marker_in_line,
)
from gate_shortcuts_text import (
    PROSE_EXTENSIONS,
    in_string,
    inside_quotes,
    python_string_spans,
)
from gate_types import FAIL, PASS, GateItem, GateOutcome, plural

KEY = "shortcuts"
TITLE = "No shortcuts"


_EXEMPT_NAMES = frozenset(
    {"gate_shortcuts.py", "gate_shortcuts_markers.py", "test_gate_shortcuts.py"}
)

POLICY_FILES = (".quality/quality-gate.json", ".quality/quality-thresholds.json")
POLICY_METRIC = "project policy changed"
POLICY_VALUE = "changed without user approval"

CHECK_CONFIG_PATTERNS = (
    "**/tsconfig*.json",
    "**/.eslintrc*",
    "**/eslint.config.*",
    "**/.prettierrc*",
    "**/prettier.config.*",
    "**/ruff.toml",
    "**/.ruff.toml",
    "**/mypy.ini",
    "**/setup.cfg",
    "**/pyproject.toml",
    "**/tox.ini",
    "**/pytest.ini",
    "**/jest.config.*",
    "**/vitest.config.*",
    "**/.golangci.*",
    "**/clippy.toml",
    "**/rustfmt.toml",
    "**/phpunit.xml*",
    "**/.rubocop.yml",
    "**/.swiftlint.yml",
    "**/detekt.yml",
    "**/Directory.Build.props",
    "**/.editorconfig",
    "**/package.json",
)


def is_exempt_from_markers(relative: str) -> bool:
    return (
        Path(relative).name in _EXEMPT_NAMES
        or Path(relative).suffix.lower() in PROSE_EXTENSIONS
    )


def marker_scan_files(root: Path, changes: ChangeSet) -> list[Path]:
    """Text files in scope, plus every source file. A NUL byte or a large
    size makes the text check skip a file, but a source file with either
    still runs, so its markers still count."""
    result: list[Path] = []
    for path in source_files.walk_files(root):
        relative = source_files.normalize_path(path, root)
        if not changes.includes(relative):
            continue
        is_source = Path(relative).suffix.lower() in source_files.SOURCE_EXTENSIONS
        if is_source or source_files.is_text_file(path):
            result.append(path)
    return sorted(result)


def marker_items(root: Path, changes: ChangeSet) -> list[GateItem]:
    """Markers on added lines of every changed text file (every line of every
    text file in repository scope)."""
    items: list[GateItem] = []
    for path in marker_scan_files(root, changes):
        relative = source_files.normalize_path(path, root)
        if is_exempt_from_markers(relative):
            continue
        lines = source_files.read_text(path).split("\n")
        items.extend(_markers_in_lines(relative, lines, changes))
    return items


def _markers_in_lines(
    relative: str, lines: Sequence[str], changes: ChangeSet
) -> list[GateItem]:
    items: list[GateItem] = []
    spans = (
        python_string_spans("\n".join(lines))
        if Path(relative).suffix.lower() in PYTHON
        else None
    )
    for number in sorted(changes.added_lines(relative, len(lines))):
        if number > len(lines):
            continue
        line = lines[number - 1]
        hit = marker_in_line(line, relative)
        if hit is None:
            continue
        quoted = (
            in_string(spans, number, hit.column)
            if spans is not None
            else inside_quotes(line, hit.column)
        )
        if not quoted:
            items.append(_marker_item(relative, number, hit))
    return items


def _marker_item(relative: str, line: int, hit: MarkerHit) -> GateItem:
    return GateItem(
        relative,
        line,
        "",
        f"{hit.marker.kind} marker",
        repr(hit.text),
        "0",
    )


def hidden_file_items(root: Path, source_config: dict[str, Any]) -> list[GateItem]:
    return [
        GateItem(
            relative,
            None,
            "",
            "hidden from the gates by",
            reason,
            "standard test and tooling patterns only",
        )
        for relative, reason in source_files.hidden_production_files(
            root, source_config
        )
    ]


def silent_handler_items(
    root: Path, changes: ChangeSet, source_config: dict[str, Any]
) -> list[GateItem]:
    """Silent handlers the change introduced in production files."""
    items: list[GateItem] = []
    for path in source_files.discover_source_files(root, source_config, changes):
        relative = source_files.normalize_path(path, root)
        text = source_files.read_text(path)
        for handler in new_silent_handlers(root, changes, relative, text):
            items.append(_handler_item(relative, handler))
    return items


HandlerKey = tuple[str, str]


def new_silent_handlers(
    root: Path, changes: ChangeSet, relative: str, text: str
) -> list[SilentHandler]:
    """Handlers that touch added lines, plus handlers that became silent
    because the change deleted lines inside them (a pure deletion adds no
    line, so only a comparison with the base version shows it)."""
    suffix = Path(relative).suffix
    base_keys = _base_handler_keys(root, changes, relative, suffix)
    lines = text.split("\n")
    found: list[SilentHandler] = []
    for handler in silent_handlers(text, suffix):
        if changes.touches(relative, handler.start_line, handler.end_line):
            found.append(handler)
        elif base_keys is not None and not _take(
            base_keys, _handler_key(handler, lines)
        ):
            found.append(handler)
    return found


def _base_handler_keys(
    root: Path, changes: ChangeSet, relative: str, suffix: str
) -> Counter[HandlerKey] | None:
    """Silent handlers of the base version, or None when the change removed
    no line from the file (or the base cannot be read)."""
    change = changes.files.get(relative)
    if change is None or not change.removed:
        return None
    base = changes.base_text(root, relative)
    if base is None:
        return None
    base_lines = base.split("\n")
    return Counter(
        _handler_key(handler, base_lines) for handler in silent_handlers(base, suffix)
    )


def _handler_key(handler: SilentHandler, lines: Sequence[str]) -> HandlerKey:
    """The handler's kind and text with whitespace collapsed, so the same
    handler matches in both versions wherever it sits."""
    source = " ".join(lines[handler.start_line - 1 : handler.end_line])
    return handler.kind, " ".join(source.split())


def _take(counter: Counter[HandlerKey], key: HandlerKey) -> bool:
    if counter[key] <= 0:
        return False
    counter[key] -= 1
    return True


def _handler_item(relative: str, handler: SilentHandler) -> GateItem:
    return GateItem(
        relative,
        handler.start_line,
        handler.kind,
        "silent error handler",
        "swallows the error",
        "0",
    )


def touched_paths(changes: ChangeSet) -> set[str]:
    """Paths the change created, edited, deleted, or renamed away from."""
    renamed_from = {
        change.old_path for change in changes.files.values() if change.old_path
    }
    return {*changes.paths, *changes.deleted, *renamed_from}


def changed_policy_files(changes: ChangeSet) -> list[str]:
    """Policy files the change edited, created, deleted, or renamed."""
    if not changes.incremental:
        return []
    touched = touched_paths(changes)
    return [path for path in POLICY_FILES if path in touched]


def policy_items(policy_files: Sequence[str]) -> list[GateItem]:
    return [
        GateItem(
            path,
            None,
            "",
            POLICY_METRIC,
            POLICY_VALUE,
            "user approval",
        )
        for path in policy_files
    ]


def changed_check_configs(changes: ChangeSet) -> list[str]:
    """Check-configuration files the change edited, created, deleted, or
    renamed: deleting a config is the most direct way to weaken a check."""
    if not changes.incremental:
        return []
    return sorted(
        path
        for path in touched_paths(changes)
        if source_files.matches_any(path, CHECK_CONFIG_PATTERNS)
    )


def evaluate_shortcuts(
    root: Path,
    changes: ChangeSet,
    source_config: dict[str, Any],
    *,
    allow_policy_change: bool = False,
) -> GateOutcome:
    """Run every shortcut check and fail on any hidden file, new marker, new
    silent handler, or unapproved policy edit."""
    hidden = hidden_file_items(root, source_config)
    markers = marker_items(root, changes)
    handlers = silent_handler_items(root, changes, source_config)
    policy_files = changed_policy_files(changes)
    policy = [] if allow_policy_change else policy_items(policy_files)
    items = [*hidden, *markers, *handlers, *policy]
    counts = {
        "hidden_files": len(hidden),
        "markers": len(markers),
        "silent_handlers": len(handlers),
        "policy_changes": len(policy),
    }
    details = [item.describe() for item in items]
    details.extend(_informational_details(changes, policy_files, allow_policy_change))
    return GateOutcome(
        KEY,
        TITLE,
        FAIL if items else PASS,
        _summary(counts),
        details,
        items,
        measurements={
            **counts,
            "check_config_changes": len(changed_check_configs(changes)),
        },
    )


def _informational_details(
    changes: ChangeSet, policy_files: Sequence[str], allow_policy_change: bool
) -> list[str]:
    details = [
        f"Check configuration changed: {path} — confirm the change does not "
        "weaken checks."
        for path in changed_check_configs(changes)
    ]
    if allow_policy_change:
        details.extend(
            f"Project policy changed with user approval: {path}"
            for path in policy_files
        )
    return details


_COUNT_LABELS = (
    ("hidden_files", "hidden production file"),
    ("markers", "suppression or skip marker"),
    ("silent_handlers", "silent error handler"),
    ("policy_changes", "unapproved policy edit"),
)


def _summary(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total == 0:
        return (
            "No hidden production files, new suppression or skip markers, "
            "silent error handlers, or unapproved policy edits."
        )
    parts = [plural(counts[key], label) for key, label in _COUNT_LABELS if counts[key]]
    return "Shortcuts found: " + ", ".join(parts) + "."
