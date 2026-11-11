"""Dependency manifests: does a manifest declare dependencies, and is there a
lockfile that pins them?

The vulnerabilities gate can only check versions it can read from a lockfile.
These rules let it tell "this project has no dependencies" apart from "this
project's dependency versions are not locked", which must never pass.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import source_files
from portable_vulnerabilities import (
    REQUIREMENT_PIN,
    REQUIREMENTS_NAME,
    classify_lockfile,
)

# What a manifest without a lockfile means: the agent must add one ("fail"),
# the gate cannot inventory that ecosystem ("blocked"), or the missing
# lockfile is normal for this kind of project ("note").
UNLOCKED_FAILS = "fail"
UNLOCKED_BLOCKS = "blocked"
UNLOCKED_IS_NORMAL = "note"

_TOML_HEADER = re.compile(r"^\s*\[\[?([^\[\]]+)\]\]?\s*(?:#.*)?$", re.MULTILINE)
_TOML_KEY = re.compile(r"^\s*(?:\"[^\"]+\"|[A-Za-z0-9_.-]+)\s*=", re.MULTILINE)
_STRING_ARRAY = r"=\s*\[[\s,]*(?:#[^\n]*\n[\s,]*)*[\"']"
_POETRY_DEPENDENCIES = re.compile(
    r"tool\.poetry(?:\.group\.[^.]+)?\.(?:dev-)?dependencies"
)


def _toml_sections(text: str) -> dict[str, str]:
    """Section name to body text; a light reader for dependency tables only."""
    sections: dict[str, str] = {}
    headers = list(_TOML_HEADER.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        name = header.group(1).strip()
        sections[name] = sections.get(name, "") + text[header.end() : end]
    return sections


def _has_keys(body: str, ignored: frozenset[str] = frozenset()) -> bool:
    keys = (
        match.group(0).split("=")[0].strip().strip('"')
        for match in _TOML_KEY.finditer(body)
    )
    return any(key not in ignored for key in keys)


# pyproject sections whose value is a dependency array of strings, and the
# key that holds it (None: any key in the section).
_PYPROJECT_ARRAYS = (
    ("project", "dependencies"),
    ("project.optional-dependencies", None),
    ("dependency-groups", None),
    ("tool.pdm.dev-dependencies", None),
    ("tool.uv", "dev-dependencies"),
)


def _pyproject_declares(text: str) -> bool:
    """PEP 621, dependency groups, PDM, uv, or Poetry dependency tables."""
    sections = _toml_sections(text)
    for section, key in _PYPROJECT_ARRAYS:
        prefix = rf"^\s*{re.escape(key)}\s*" if key else ""
        body = sections.get(section, "")
        if re.search(prefix + _STRING_ARRAY, body, re.MULTILINE):
            return True
    return any(
        _has_keys(body, frozenset({"python"}))
        for name, body in sections.items()
        if _POETRY_DEPENDENCIES.fullmatch(name)
    )


def _cargo_declares(text: str) -> bool:
    for name, body in _toml_sections(text).items():
        parts = name.split(".")
        if parts[-1].endswith("dependencies") and _has_keys(body):
            return True
        if any(part.endswith("dependencies") for part in parts[:-1]):
            return True
    return False


def _pipfile_declares(text: str) -> bool:
    sections = _toml_sections(text)
    return any(
        _has_keys(sections.get(name, "")) for name in ("packages", "dev-packages")
    )


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _package_json_declares(text: str) -> bool:
    value = _json_object(text)
    sections = ("dependencies", "devDependencies", "optionalDependencies")
    return any(isinstance(value.get(key), dict) and value[key] for key in sections)


_COMPOSER_PLATFORM = re.compile(r"php|(?:ext|lib)-.+|composer(?:-plugin|-runtime)?-api")


def _composer_declares(text: str) -> bool:
    value = _json_object(text)
    for key in ("require", "require-dev"):
        section = value.get(key)
        if isinstance(section, dict) and any(
            not _COMPOSER_PLATFORM.fullmatch(name) for name in section
        ):
            return True
    return False


def _pubspec_declares(text: str) -> bool:
    """A hosted package under ``dependencies:``/``dev_dependencies:``; the
    Flutter SDK entries alone do not count."""
    in_section = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_section = line.rstrip() in {"dependencies:", "dev_dependencies:"}
            continue
        entry = re.match(r"^  ([A-Za-z0-9_]+):", line) if in_section else None
        if entry and not entry.group(1).startswith("flutter"):
            return True
    return False


def _pattern(expression: str) -> Callable[[str], bool]:
    compiled = re.compile(expression, re.MULTILINE)
    return lambda text: compiled.search(text) is not None


def _always(_manifest: Path, _text: str) -> str:
    return UNLOCKED_FAILS


def _cannot_inventory(_manifest: Path, _text: str) -> str:
    return UNLOCKED_BLOCKS


def _cargo_unlocked(manifest: Path, text: str) -> str:
    """Cargo.lock is optional for a library crate: consumers resolve its
    versions. Binaries and workspaces must commit it."""
    directory = manifest.parent
    binary = (
        "[[bin]]" in text
        or "[workspace]" in text
        or (directory / "src" / "main.rs").is_file()
        or (directory / "src" / "bin").is_dir()
    )
    return UNLOCKED_FAILS if binary else UNLOCKED_IS_NORMAL


def _never(_text: str) -> bool:
    return False


@dataclasses.dataclass(frozen=True)
class ManifestRule:
    """How to tell that a manifest declares dependencies, and which lockfiles
    lock them.

    ``lock_names`` and ``lock_pattern`` match lockfiles in the manifest's own
    folder. With ``search_parents`` a workspace lockfile in a parent folder
    also counts, but only when it covers the manifest's folder.
    ``locks_itself`` says whether the manifest's own text pins every version.
    """

    declares: Callable[[str], bool]
    lock_names: tuple[str, ...]
    lock_pattern: re.Pattern[str] | None = None
    search_parents: bool = False
    when_unlocked: Callable[[Path, str], str] = _always
    locks_itself: Callable[[str], bool] = _never


NODE_LOCKS = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
)
PYTHON_LOCKS = ("poetry.lock", "uv.lock", "pdm.lock", "pylock.toml", "Pipfile.lock")
_REQUIREMENT = re.compile(r"^\s*[A-Za-z0-9]")


def requirements_pinned(text: str) -> bool:
    """Every requirement line is an exact ``==`` pin or a direct reference
    (``name @ url``, a VCS or archive URL). Option lines such as ``-r`` or
    ``--hash`` are not requirements. One range is enough to be unlocked."""
    return all(
        REQUIREMENT_PIN.match(line) or "://" in line or " @ " in line
        for line in text.splitlines()
        if _REQUIREMENT.match(line)
    )


def _requirements_declares(text: str) -> bool:
    return any(_REQUIREMENT.match(line) for line in text.splitlines())


REQUIREMENTS_RULE = ManifestRule(
    _requirements_declares,
    PYTHON_LOCKS,
    search_parents=True,
    locks_itself=requirements_pinned,
)
DOTNET_RULE = ManifestRule(
    _pattern(r"<PackageReference\b"),
    ("packages.lock.json",),
    when_unlocked=_cannot_inventory,
)
GRADLE_RULE = ManifestRule(
    _pattern(
        r"^\s*(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"
        r"testRuntimeOnly|annotationProcessor|kapt|ksp|classpath)\b\s*[(\"']"
    ),
    (),
    re.compile(r".*gradle\.lockfile"),
    when_unlocked=_cannot_inventory,
)
MANIFEST_RULES: dict[str, ManifestRule] = {
    "package.json": ManifestRule(
        _package_json_declares,
        NODE_LOCKS,
        search_parents=True,
    ),
    "pyproject.toml": ManifestRule(
        _pyproject_declares, PYTHON_LOCKS, REQUIREMENTS_NAME, True
    ),
    "Pipfile": ManifestRule(_pipfile_declares, ("Pipfile.lock",)),
    "Gemfile": ManifestRule(
        _pattern(r"^\s*gem\s+[\"']"),
        ("Gemfile.lock", "gems.locked"),
    ),
    "gems.rb": ManifestRule(
        _pattern(r"^\s*gem\s+[\"']"),
        ("Gemfile.lock", "gems.locked"),
    ),
    "Cargo.toml": ManifestRule(
        _cargo_declares,
        ("Cargo.lock",),
        search_parents=True,
        when_unlocked=_cargo_unlocked,
    ),
    "go.mod": ManifestRule(
        _pattern(r"^\s*(?:require\s+)?[^\s/()]+\.\S*\s+v\d"),
        ("go.sum",),
    ),
    "composer.json": ManifestRule(
        _composer_declares,
        ("composer.lock",),
    ),
    "mix.exs": ManifestRule(_pattern(r"\{:\w+,"), ("mix.lock",)),
    "pubspec.yaml": ManifestRule(
        _pubspec_declares,
        ("pubspec.lock",),
        when_unlocked=_cannot_inventory,
    ),
    "Package.swift": ManifestRule(
        _pattern(r"\.package\s*\("),
        ("Package.resolved",),
        when_unlocked=_cannot_inventory,
    ),
    "pom.xml": ManifestRule(
        _pattern(r"<dependency>"),
        (),
        when_unlocked=_cannot_inventory,
    ),
    "build.gradle": GRADLE_RULE,
    "build.gradle.kts": GRADLE_RULE,
}


def manifest_rule(name: str) -> ManifestRule | None:
    rule = MANIFEST_RULES.get(name)
    if rule is None and REQUIREMENTS_NAME.fullmatch(name):
        return REQUIREMENTS_RULE
    if rule is None and PurePosixPath(name).suffix in {".csproj", ".fsproj", ".vbproj"}:
        return DOTNET_RULE
    return rule


# Files that pin versions without being a lockfile this gate parses.
CHECKSUM_FILES = frozenset({"go.sum"})


def is_dependency_file(name: str) -> bool:
    return (
        manifest_rule(name) is not None
        or classify_lockfile(name) is not None
        or name in CHECKSUM_FILES
    )


# Folders that hold sample projects for tests rather than the project's own
# dependencies. Test suites such as e2e/ or tests/ have real dependencies.
FIXTURE_FOLDERS = frozenset(
    {
        "fixtures",
        "__fixtures__",
        "__mocks__",
        "test-fixtures",
        "test_fixtures",
        "testdata",
    }
)


def dependency_files(root: Path) -> list[str]:
    """Repository-relative manifests, lockfiles, and checksum files outside
    fixture folders."""
    found = (
        source_files.normalize_path(path, root)
        for path in source_files.walk_files(root)
    )
    return sorted(
        relative
        for relative in found
        if is_dependency_file(PurePosixPath(relative).name)
        and not FIXTURE_FOLDERS.intersection(PurePosixPath(relative).parts[:-1])
    )


def _directory(relative: str) -> str:
    return PurePosixPath(relative).parent.as_posix()


def lock_search_directories(rule: ManifestRule, manifest: str) -> list[str]:
    """The folders whose dependency files decide whether ``manifest`` is
    locked: its own folder, and every parent folder for workspace ecosystems."""
    directory = PurePosixPath(manifest).parent
    if not rule.search_parents:
        return [directory.as_posix()]
    return [directory.as_posix(), *(parent.as_posix() for parent in directory.parents)]


@dataclasses.dataclass(frozen=True)
class LockIndex:
    """Which dependency files exist in each folder of the repository at
    ``root``. Lockfile text is read only when a parent-folder lockfile must
    prove that it covers a workspace member."""

    root: Path
    by_directory: Mapping[str, frozenset[str]]

    @classmethod
    def build(cls, root: Path, relatives: Iterable[str]) -> LockIndex:
        grouped: dict[str, set[str]] = {}
        for relative in relatives:
            grouped.setdefault(_directory(relative), set()).add(
                PurePosixPath(relative).name
            )
        directories = {key: frozenset(names) for key, names in grouped.items()}
        return cls(root, directories)

    def has_lock(self, rule: ManifestRule, manifest: str) -> bool:
        own, *parents = lock_search_directories(rule, manifest)
        if any(self._locks(rule, own, name) for name in self._names(own)):
            return True
        return any(
            self._covers(_join(parent, name), manifest)
            for parent in parents
            for name in self._names(parent)
            if name in rule.lock_names
        )

    def _names(self, directory: str) -> list[str]:
        return sorted(self.by_directory.get(directory, frozenset()))

    def _locks(self, rule: ManifestRule, directory: str, name: str) -> bool:
        """A same-folder lockfile. A requirements file locks a pyproject only
        when it pins every line, as pip-compile writes it."""
        if name in rule.lock_names:
            return True
        if rule.lock_pattern is None or not rule.lock_pattern.fullmatch(name):
            return False
        if not REQUIREMENTS_NAME.fullmatch(name):
            return True
        return requirements_pinned(self.read(_join(directory, name)))

    def _covers(self, lock: str, manifest: str) -> bool:
        """Whether a parent-folder lockfile includes the workspace member
        that ``manifest`` belongs to; any other lockfile up the tree belongs to
        a different project."""
        lock_path = PurePosixPath(lock)
        member = PurePosixPath(manifest).parent.relative_to(lock_path.parent)
        text = self.read(lock)
        if lock_path.name == "Cargo.lock":
            return _cargo_lists_member(text, self.read(manifest))
        if lock_path.name == "yarn.lock" and "__metadata:" not in text:
            package_json = _join(lock_path.parent.as_posix(), "package.json")
            return _declares_workspace(self.read(package_json), member.as_posix())
        return _mentions_path(text, member.as_posix())

    def read(self, relative: str) -> str:
        """File text, or "" when the file cannot be read (then it proves
        nothing)."""
        try:
            return (self.root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _join(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def _mentions_path(text: str, member: str) -> bool:
    """A workspace path as a quoted value or a key: ``"packages/a"`` in
    package-lock, bun.lock, and uv.lock, ``packages/a:`` under pnpm
    ``importers``, and ``@workspace:packages/a`` in Yarn Berry."""
    path = re.escape(member)
    pattern = rf"(?:^|[\s\"':]){path}(?:[\"']|:[ \t]*$)"
    return re.search(pattern, text, re.MULTILINE) is not None


_CARGO_NAME = re.compile(r"^\s*name\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _cargo_lists_member(lock_text: str, manifest_text: str) -> bool:
    """Cargo.lock lists every workspace member as a ``[[package]]`` by name."""
    package = _toml_sections(manifest_text).get("package", "")
    name = _CARGO_NAME.search(package)
    if name is None:
        return False
    return (
        re.search(
            rf"^name\s*=\s*\"{re.escape(name.group(1))}\"\s*$", lock_text, re.MULTILINE
        )
        is not None
    )


def _declares_workspace(package_json: str, member: str) -> bool:
    """Yarn classic lockfiles do not name workspaces, so the root
    package.json ``workspaces`` globs decide."""
    try:
        value = json.loads(package_json)
    except ValueError:
        return False
    workspaces = value.get("workspaces") if isinstance(value, dict) else None
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if not isinstance(workspaces, list):
        return False
    return any(
        isinstance(pattern, str) and fnmatch.fnmatchcase(member, pattern.rstrip("/"))
        for pattern in workspaces
    )


@dataclasses.dataclass(frozen=True)
class ManifestCheck:
    """The verdict for one manifest: ``locked``, ``no dependencies``, or one of
    the ``UNLOCKED_*`` values."""

    path: str
    verdict: str


def check_manifest(root: Path, relative: str, index: LockIndex) -> ManifestCheck:
    """Raises OSError when the manifest cannot be read and ValueError when a
    JSON manifest is malformed."""
    path = root / relative
    rule = manifest_rule(path.name)
    if rule is None:
        raise ValueError(f"{relative} is not a dependency manifest")
    text = path.read_text(encoding="utf-8", errors="replace")
    if not rule.declares(text):
        return ManifestCheck(relative, "no dependencies")
    if rule.locks_itself(text) or index.has_lock(rule, relative):
        return ManifestCheck(relative, "locked")
    return ManifestCheck(relative, rule.when_unlocked(path, text))
