"""Repository file discovery shared by every generic gate.

Globs are anchored at the repository root. ``**/`` matches zero or more
directories, ``**`` matches anything, ``*`` and ``?`` never cross a ``/``.
Matching ignores case so ``Tests/`` and ``tests/`` behave the same.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".swift",
        ".scala",
        ".lua",
        ".ex",
        ".exs",
        ".dart",
        ".groovy",
        ".sol",
        ".zig",
        ".m",
        ".mm",
        ".erl",
        ".f90",
        ".gd",
        ".pl",
        ".r",
        ".vue",
    }
)

EXTENSION_LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".scala": "Scala",
    ".lua": "Lua",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".dart": "Dart",
    ".groovy": "Groovy",
    ".sol": "Solidity",
    ".zig": "Zig",
    ".m": "Objective-C",
    ".mm": "Objective-C",
    ".erl": "Erlang",
    ".f90": "Fortran",
    ".gd": "GDScript",
    ".pl": "Perl",
    ".r": "R",
    ".vue": "Vue",
}

# Third-party code, tool caches, and generated build output. ``bin`` is not
# here: committed scripts in ``bin/`` are production code.
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        # Agent tooling, including this skill when installed in a repository.
        ".agents",
        ".claude",
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "target",
        "dist",
        "build",
        "out",
        "obj",
        "coverage",
        "htmlcov",
        ".coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        ".next",
        ".nuxt",
        ".gradle",
        ".dart_tool",
        ".build",
        "Pods",
    }
)

# Gate output that is never source text.
GENERATED_DIRS = frozenset({".quality"})

DEFAULT_TEST_PATTERNS = (
    "tests/**",
    "test/**",
    "spec/**",
    "e2e/**",
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/__mocks__/**",
    "**/*_test.*",
    "**/test_*.*",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*_spec.*",
    "**/*.test-d.*",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*Test.kt",
    "**/*Tests.kt",
    "**/*Tests.cs",
    "**/*Tests.swift",
)

TOOLING_FILE_PATTERNS = (
    "**/*.config.*",
    "**/.*rc.*",
    "**/*.d.ts",
    "**/conftest.py",
    "**/setup.py",
)

MAX_TEXT_BYTES = 2_000_000


class ChangeScope(Protocol):
    """The part of ``diff_scope.ChangeSet`` that file discovery needs."""

    @property
    def incremental(self) -> bool: ...

    def includes(self, path: str) -> bool: ...


@functools.lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts), re.IGNORECASE)


def glob_matches(relative: str, pattern: str) -> bool:
    """True when the root-relative POSIX path matches the root-anchored glob."""
    return _glob_regex(pattern.lstrip("/")).fullmatch(relative) is not None


def matches_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(glob_matches(relative, str(pattern)) for pattern in patterns)


def first_match(relative: str, patterns: Sequence[str]) -> str | None:
    for pattern in patterns:
        if glob_matches(relative, str(pattern)):
            return str(pattern)
    return None


def is_test_path(relative: str) -> bool:
    return matches_any(relative, DEFAULT_TEST_PATTERNS)


def is_tooling_path(relative: str) -> bool:
    return matches_any(relative, TOOLING_FILE_PATTERNS)


def normalize_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def is_file_within(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _excluded(relative_parts: Sequence[str]) -> bool:
    return any(
        part in DEFAULT_EXCLUDED_DIRS or part in GENERATED_DIRS
        for part in relative_parts[:-1]
    )


def walk_files(root: Path) -> Iterable[Path]:
    """Every repository file: Git's tracked and untracked non-ignored files,
    or a directory walk outside Git. Third-party and generated folders are
    skipped."""
    listed = _git_listed_files(root)
    if listed is not None:
        for relative in listed:
            path = root / relative
            if not _excluded(relative.parts) and is_file_within(path, root):
                yield path
        return
    yield from _walked_files(root)


def _git_listed_files(root: Path) -> list[Path] | None:
    """Tracked and untracked non-ignored files, or None outside a Git checkout."""
    if not (root / ".git").exists() or not shutil.which("git"):
        return None
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if listed.returncode != 0:
        return None
    return [Path(os.fsdecode(raw)) for raw in listed.stdout.split(b"\0") if raw]


def _walked_files(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in DEFAULT_EXCLUDED_DIRS
            and directory not in GENERATED_DIRS
        ]
        for filename in files:
            path = Path(current) / filename
            if is_file_within(path, root):
                yield path


def is_production_candidate(
    relative: str,
    includes: Sequence[str],
    excludes: Sequence[str],
    extensions: frozenset[str],
) -> bool:
    if includes:
        if not matches_any(relative, includes):
            return False
        if Path(relative).suffix.lower() not in extensions:
            return False
    elif Path(relative).suffix.lower() not in extensions:
        return False
    return not matches_any(relative, excludes)


def source_settings(
    source_config: dict[str, Any],
) -> tuple[list[str], list[str], frozenset[str]]:
    includes = [str(pattern) for pattern in source_config.get("include", [])]
    excludes = [
        str(pattern)
        for pattern in source_config.get("exclude", list(DEFAULT_TEST_PATTERNS))
    ]
    extensions = frozenset(
        str(extension).lower()
        for extension in source_config.get("extensions", SOURCE_EXTENSIONS)
    )
    return includes, excludes, extensions


def discover_source_files(
    root: Path,
    source_config: dict[str, Any],
    scope: ChangeScope | None = None,
    skip_names: frozenset[str] = frozenset(),
) -> list[Path]:
    """Production source files, optionally limited to the files a change touched."""
    includes, excludes, extensions = source_settings(source_config)
    result: list[Path] = []
    for path in walk_files(root):
        relative = normalize_path(path, root)
        if scope is not None and not scope.includes(relative):
            continue
        if path.name in skip_names:
            continue
        if is_production_candidate(relative, includes, excludes, extensions):
            result.append(path)
    return sorted(result)


def is_text_file(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return False
        with path.open("rb") as handle:
            return b"\0" not in handle.read(8192)
    except OSError:
        return False


def text_files(root: Path, scope: ChangeScope | None = None) -> list[Path]:
    """Every text file in the repository, including tests and configuration."""
    result: list[Path] = []
    for path in walk_files(root):
        relative = normalize_path(path, root)
        if scope is not None and not scope.includes(relative):
            continue
        if is_text_file(path):
            result.append(path)
    return sorted(result)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def physical_line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace", newline=None) as source:
        return sum(1 for _ in source)


def text_line_count(text: str) -> int:
    return len(text.splitlines())


def language_for(relative: str) -> str | None:
    return EXTENSION_LANGUAGES.get(Path(relative).suffix.lower())


def hidden_production_files(
    root: Path, source_config: dict[str, Any]
) -> list[tuple[str, str]]:
    """Production files that the project's ``source`` settings keep out of the
    gates: an extra ``exclude`` pattern, an ``include`` list that leaves them
    out, or an ``extensions`` list that omits their language. Standard test
    patterns and tooling files may be excluded. Returns (path, reason) pairs."""
    includes, excludes, extensions = source_settings(source_config)
    extra = [pattern for pattern in excludes if pattern not in DEFAULT_TEST_PATTERNS]
    hidden: list[tuple[str, str]] = []
    for path in walk_files(root):
        relative = normalize_path(path, root)
        if not is_production_candidate(
            relative, [], list(DEFAULT_TEST_PATTERNS), SOURCE_EXTENSIONS
        ) or is_tooling_path(relative):
            continue
        reason = _hiding_reason(relative, includes, extra, extensions)
        if reason is not None:
            hidden.append((relative, reason))
    return hidden


def _hiding_reason(
    relative: str,
    includes: Sequence[str],
    extra_excludes: Sequence[str],
    extensions: frozenset[str],
) -> str | None:
    pattern = first_match(relative, extra_excludes)
    if pattern is not None:
        return f"source.exclude pattern {pattern!r}"
    if includes and not matches_any(relative, includes):
        return "not matched by source.include"
    if Path(relative).suffix.lower() not in extensions:
        return "extension missing from source.extensions"
    return None
