"""Secrets gate: high-confidence credential patterns in every file Git would
publish.

Only the kind of secret and its line are ever recorded. The matched value
never enters a finding, a detail line, or the report, so the gate cannot leak
the credential it found.

Every pattern is plain ASCII, so files are read as bytes and scanned whatever
their encoding: binary files, files with NUL bytes, UTF-16 files, and very
large files are all checked rather than skipped. A file that cannot be opened
blocks a clean result instead of passing silently.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

import source_files
from diff_scope import ChangeSet
from gate_types import BLOCKED, GateItem, GateOutcome, outcome_for_items, plural

KEY = "secrets"
TITLE = "Secrets"

_BEFORE = r"(?<![A-Za-z0-9])"
_AFTER = r"(?![A-Za-z0-9])"


def _anchored(body: str) -> re.Pattern[str]:
    """Require a non-alphanumeric neighbour on both sides so a token embedded
    in a longer identifier or hash does not count."""
    return re.compile(_BEFORE + body + _AFTER)


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", _anchored(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("GitHub token", _anchored(r"gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("GitHub fine-grained token", _anchored(r"github_pat_[A-Za-z0-9_]{22,255}")),
    ("GitLab token", _anchored(r"glpat-[A-Za-z0-9_-]{20,}")),
    # No "xoxo-": it is an English word that turns up in URLs and prose.
    ("Slack token", _anchored(r"xox[abeprs]-[A-Za-z0-9-]{10,}")),
    ("Slack app token", _anchored(r"xapp-[0-9]-[A-Za-z0-9-]{10,}")),
    (
        "Slack webhook",
        _anchored(
            r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"
        ),
    ),
    ("Stripe live key", _anchored(r"(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("Anthropic API key", _anchored(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI project key", _anchored(r"sk-proj-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", _anchored(r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}")),
    ("Google API key", _anchored(r"AIza[0-9A-Za-z_-]{35}")),
    ("npm token", _anchored(r"npm_[A-Za-z0-9]{36}")),
    ("PyPI token", _anchored(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}")),
    ("Hugging Face token", _anchored(r"hf_[A-Za-z0-9]{34,}")),
    (
        "SendGrid API key",
        _anchored(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"),
    ),
)

# Provider documentation uses values such as AWS's AKIAIOSFODNN7EXAMPLE. A
# random credential never ends in this word, so such matches are placeholders.
PLACEHOLDER_SUFFIX = "EXAMPLE"

# Twilio key SIDs look like any 34-character hex string, so they only count
# when the same line names Twilio.
TWILIO_KIND = "Twilio API key"
_TWILIO_PATTERN = _anchored(r"SK[0-9a-fA-F]{32}")
_TWILIO_CONTEXT = re.compile("twilio", re.IGNORECASE)

# A PEM header alone is not a secret: parsers, validators, and docs name it.
# It counts only when key material follows, either on one of the next few
# lines (room for Proc-Type, DEK-Info, Version, Comment and a blank line) or
# on the same line after an escaped newline, as in JSON and .env values.
PRIVATE_KEY_KIND = "private key"
KEY_MATERIAL_WINDOW = 5
_PEM_HEADER = (
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?"
    r"PRIVATE KEY(?: BLOCK)?-----"
)
_KEY_HEADER = _anchored(_PEM_HEADER)
_INLINE_KEY = re.compile(_BEFORE + _PEM_HEADER + r".{0,200}?\\n[A-Za-z0-9+/]{40,}")
_KEY_MATERIAL_LINE = re.compile(
    r"[\s\"'`]*[A-Za-z0-9+/]{40,}={0,2}(?:\\n)?(?:[\s\"'`,;][\s\"'`,;+]*)?"
)

# One combined search rejects the vast majority of lines, which hold no
# candidate at all, before the per-kind patterns run.
_ANY_CANDIDATE = re.compile(
    "|".join(
        pattern.pattern
        for pattern in (
            *(pattern for _, pattern in SECRET_PATTERNS),
            _TWILIO_PATTERN,
            _KEY_HEADER,
        )
    )
)

_KIND_ORDER = {
    kind: index
    for index, kind in enumerate(
        (PRIVATE_KEY_KIND, *(kind for kind, _ in SECRET_PATTERNS), TWILIO_KIND)
    )
}

# Untracked folders that hold installed dependencies or tool caches. They are
# rebuilt locally rather than committed, and scanning a forgotten
# node_modules or virtualenv would be slow and report other people's fixtures.
# Tracked files are always scanned, wherever they live.
UNTRACKED_SKIPPED_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".gradle",
        ".dart_tool",
        ".next",
        ".nuxt",
        "Pods",
    }
)


def scan_line(line: str) -> list[str]:
    """The kinds of secret one line proves on its own, in report order.

    A private-key header on its own line needs the following lines as well,
    so ``scan_lines`` handles that case.
    """
    if not _ANY_CANDIDATE.search(line):
        return []
    kinds = [PRIVATE_KEY_KIND] if _INLINE_KEY.search(line) else []
    kinds.extend(
        kind for kind, pattern in SECRET_PATTERNS if _has_real_match(pattern, line)
    )
    if _TWILIO_PATTERN.search(line) and _TWILIO_CONTEXT.search(line):
        kinds.append(TWILIO_KIND)
    return kinds


def _has_real_match(pattern: re.Pattern[str], line: str) -> bool:
    return any(
        not match.group().endswith(PLACEHOLDER_SUFFIX)
        for match in pattern.finditer(line)
    )


def scan_lines(lines: Iterable[str]) -> list[tuple[str, int]]:
    """Every ``(kind, line number)`` in ``lines``, ordered by line; never the
    matched value. A private key is reported on its header line."""
    findings: list[tuple[str, int]] = []
    waiting_headers: list[int] = []
    for number, line in enumerate(lines, start=1):
        findings.extend((kind, number) for kind in scan_line(line))
        waiting_headers = [
            header
            for header in waiting_headers
            if number - header <= KEY_MATERIAL_WINDOW
        ]
        if waiting_headers and _KEY_MATERIAL_LINE.fullmatch(line):
            findings.extend((PRIVATE_KEY_KIND, header) for header in waiting_headers)
            waiting_headers = []
        if _KEY_HEADER.search(line) and not _INLINE_KEY.search(line):
            waiting_headers.append(number)
    return sorted(findings, key=lambda finding: (finding[1], _KIND_ORDER[finding[0]]))


def scan_text(text: str) -> list[tuple[str, int]]:
    """Every ``(kind, line number)`` found in ``text``; never the matched value.

    Lines are split on ``\\n`` only, so numbers match Git, grep and editors
    even when the text holds form feeds or Unicode line separators.
    """
    return scan_lines(text.split("\n"))


def scan_file(path: Path) -> list[tuple[str, int]]:
    """Scan one file of any encoding or size; raises ``OSError`` when it
    cannot be read."""
    with path.open("rb") as handle:
        return scan_lines(_ascii_view(raw) for raw in handle)


def _ascii_view(raw: bytes) -> str:
    """One raw line as text the ASCII patterns can match.

    Dropping NUL bytes turns UTF-16 and UTF-32 text into its ASCII letters;
    other bytes outside ASCII become replacement characters, which no pattern
    accepts.
    """
    return raw.replace(b"\0", b"").decode("utf-8", errors="replace").rstrip("\r\n")


def scan_files(root: Path, paths: Sequence[Path]) -> tuple[list[GateItem], list[str]]:
    """Findings for ``paths`` plus the relative paths that could not be read."""
    items: list[GateItem] = []
    unreadable: list[str] = []
    for path in paths:
        relative = source_files.normalize_path(path, root)
        try:
            found = scan_file(path)
        except OSError:
            unreadable.append(relative)
            continue
        items.extend(_finding(relative, kind, line) for kind, line in found)
    return items, unreadable


def _finding(relative: str, kind: str, line: int) -> GateItem:
    return GateItem(relative, line, kind, "secret", "found", "none")


def publishable_files(root: Path, changes: ChangeSet) -> list[Path]:
    """The in-scope files a commit could publish.

    Inside Git this is every tracked file, including ones under build/, out/,
    .vscode/ or other folders the code-quality gates ignore, plus untracked
    files that .gitignore does not exclude. Outside Git it falls back to the
    shared repository walk.
    """
    listed = _git_files(root)
    paths = list(source_files.walk_files(root)) if listed is None else listed
    return sorted(
        path
        for path in paths
        if changes.includes(source_files.normalize_path(path, root))
    )


def _git_files(root: Path) -> list[Path] | None:
    """Tracked plus non-ignored untracked files, or None when Git cannot list."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    tracked = _ls_files(root, "--cached")
    untracked = _ls_files(root, "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    relatives = tracked + [
        relative for relative in untracked if not _in_skipped_dir(relative)
    ]
    paths = [root / relative for relative in relatives]
    return [path for path in paths if source_files.is_file_within(path, root)]


def _ls_files(root: Path, *options: str) -> list[str] | None:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", *options, "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if listed.returncode != 0:
        return None
    return [os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw]


def _in_skipped_dir(relative: str) -> bool:
    return any(part in UNTRACKED_SKIPPED_DIRS for part in relative.split("/")[:-1])


def evaluate_secrets(root: Path, changes: ChangeSet) -> GateOutcome:
    """Scan every publishable file in scope.

    In an incremental scope every line of each changed file is scanned, so a
    secret that was already in a touched file still fails the change.
    """
    paths = publishable_files(root, changes)
    items, unreadable = scan_files(root, paths)
    outcome = outcome_for_items(
        KEY,
        TITLE,
        items,
        f"No high-confidence secrets in {plural(len(paths), 'file')}.",
        f"High-confidence secrets found: {len(items)} in "
        f"{plural(len(paths), 'scanned file')}; limit 0. Secret values are never "
        "shown.",
        measurements=_measurements(paths, items),
    )
    if unreadable:
        _mark_unreadable(outcome, unreadable)
    return outcome


def _measurements(paths: Sequence[Path], items: Sequence[GateItem]) -> dict[str, int]:
    measurements = {"files_scanned": len(paths), "findings": len(items)}
    for item in items:
        key = "kind:" + item.name
        measurements[key] = measurements.get(key, 0) + 1
    return measurements


def _mark_unreadable(outcome: GateOutcome, unreadable: Sequence[str]) -> None:
    """A file the gate could not read was not checked, so a clean result
    cannot be trusted."""
    outcome.details.extend(f"{path}: could not be read" for path in unreadable)
    outcome.measurements["files_unreadable"] = len(unreadable)
    if outcome.passed:
        outcome.status = BLOCKED
        outcome.summary = (
            f"{plural(len(unreadable), 'file')} could not be read for the secrets scan."
        )
