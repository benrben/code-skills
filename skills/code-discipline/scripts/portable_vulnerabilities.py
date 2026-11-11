"""Read locked dependency versions and query OSV without restoring a project.

Every lockfile parser works on text, so the same parser reads the working-tree
file and the version of the file before a change. This module imports only the
standard library because older runners inline it into a single-file script.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, cast
from urllib.request import Request, urlopen

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
EXCLUDED_DIRECTORIES = frozenset(
    ".git .venv venv node_modules vendor target dist build".split()
)
# Lockfiles that pin versions in a format this module has no parser for.
KNOWN_UNSUPPORTED = frozenset(
    "Package.resolved bun.lock bun.lockb cabal.project.freeze conan.lock gems.locked "
    "mix.lock pdm.lock pubspec.lock renv.lock stack.yaml.lock".split()
)


@dataclass(frozen=True, slots=True)
class LockedPackage:
    ecosystem: str
    name: str
    version: str
    source: str

    @property
    def coordinate(self) -> tuple[str, str, str]:
        """Identity without the lockfile path, for comparing two lockfile
        versions."""
        return (self.ecosystem, self.name, self.version)


@dataclass(frozen=True, slots=True)
class DependencyInventory:
    packages: tuple[LockedPackage, ...]
    lockfiles: tuple[str, ...]
    unsupported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VulnerabilityFinding:
    vulnerability_id: str
    package: LockedPackage


@dataclass(frozen=True, slots=True)
class LockfileReading:
    """The packages read from one lockfile, and whether the file visibly lists
    dependencies. A file that lists dependencies but yields no packages was not
    understood, which must never pass as "no vulnerabilities"."""

    source: str
    packages: tuple[LockedPackage, ...]
    lists_dependencies: bool

    @property
    def unreadable(self) -> bool:
        return self.lists_dependencies and not self.packages


class BytesResponse:
    """Small response wrapper used by deterministic adapters and tests."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> BytesResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int = -1) -> bytes:
        return self.payload if maximum < 0 else self.payload[:maximum]


def _walk(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        folders = path.relative_to(root).parts[:-1]
        if path.is_file() and not any(part in EXCLUDED_DIRECTORIES for part in folders):
            yield path


def _locked(
    ecosystem: str, name: Any, version: Any, source: str
) -> LockedPackage | None:
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    name, version = name.strip(), version.strip()
    version = version.lstrip("v") if ecosystem == "Go" else version
    if not name or not version or version in {"*", "latest"}:
        return None
    return LockedPackage(ecosystem, name, version, source)


def _present(items: Iterable[LockedPackage | None]) -> list[LockedPackage]:
    return [item for item in items if item is not None]


def _package_lock_item(location: Any, data: Any, source: str) -> LockedPackage | None:
    if not isinstance(location, str) or not location or not isinstance(data, dict):
        return None
    marker = "node_modules/"
    name = location.rsplit(marker, 1)[-1] if marker in location else ""
    return _locked("npm", name, data.get("version"), source)


def _package_lock_tree(tree: Any, source: str) -> Iterator[LockedPackage | None]:
    """Walk lockfile v1's nested ``dependencies`` tree."""
    if not isinstance(tree, dict):
        return
    for name, data in tree.items():
        if not isinstance(data, dict):
            continue
        yield _locked("npm", name, data.get("version"), source)
        yield from _package_lock_tree(data.get("dependencies"), source)


def _package_lock_parts(text: str) -> tuple[dict[Any, Any] | None, Any]:
    """The v2/v3 ``packages`` map when it lists more than the root project,
    else None, and the v1 ``dependencies`` tree. The map wins when present."""
    value = json.loads(text)
    if not isinstance(value, dict):
        return None, None
    packages = value.get("packages")
    if isinstance(packages, dict) and any(key != "" for key in packages):
        return packages, None
    return None, value.get("dependencies")


def _package_lock(text: str, source: str) -> list[LockedPackage]:
    packages, tree = _package_lock_parts(text)
    if packages is not None:
        return _present(
            _package_lock_item(location, data, source)
            for location, data in packages.items()
        )
    return _present(_package_lock_tree(tree, source))


def _lists_npm_packages(text: str) -> bool:
    """Third-party entries in a package-lock. Workspace links (``"link":
    true``) and the workspace folders themselves are not packages."""
    packages, tree = _package_lock_parts(text)
    if packages is not None:
        return any(
            "node_modules/" in str(location)
            and isinstance(data, dict)
            and not data.get("link")
            for location, data in packages.items()
        )
    return isinstance(tree, dict) and bool(tree)


_YARN_VERSION = re.compile(r'^\s+version:?\s+"?([^"\s]+)"?\s*$')
_LOCAL_YARN_PROTOCOLS = ("@workspace:", "@link:", "@portal:", "@file:")


def _yarn_name(header: str) -> str | None:
    """The package name from an entry header such as
    ``"@scope/pkg@^1.0.0", "@scope/pkg@^1.1.0":`` or ``"pkg@npm:^1.0.0":``."""
    first = header.rstrip(":").split(",")[0].strip().strip('"')
    if first == "__metadata" or any(p in first for p in _LOCAL_YARN_PROTOCOLS):
        return None
    separator = first.find("@", 1)
    return first[:separator] if separator > 0 else None


def _yarn_lock(text: str, source: str) -> list[LockedPackage]:
    """Yarn classic (v1) and Berry lockfiles: an unindented header names the
    package, and the indented ``version`` line holds the locked version."""
    result: list[LockedPackage | None] = []
    name: str | None = None
    for line in text.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            name = _yarn_name(line)
            continue
        match = _YARN_VERSION.match(line)
        if name and match:
            result.append(_locked("npm", name, match.group(1), source))
            name = None
    return _present(result)


_PNPM_KEY = re.compile(r"^  ['\"]?/?([^'\"\s]+?)['\"]?:\s*$")
_PNPM_AT_FORM = re.compile(r"^((?:@[^@/]+/)?[^@/]+)@([^@/]+)$")
_PNPM_SLASH_FORM = re.compile(r"^(@[^/]+/[^/]+|[^@/][^/]*)/([^/_]+)(?:_.*)?$")


def _pnpm_package(key: str, source: str) -> LockedPackage | None:
    """Read ``name@1.0.0(peer@2)`` (v6, v9) or ``name/1.0.0_peer@2`` (v5)."""
    match = _PNPM_AT_FORM.match(key.split("(", 1)[0])
    match = match or _PNPM_SLASH_FORM.match(key)
    return _locked("npm", match.group(1), match.group(2), source) if match else None


def _pnpm_lock(text: str, source: str) -> list[LockedPackage]:
    result: list[LockedPackage | None] = []
    in_packages = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_packages = line.rstrip() == "packages:"
            continue
        key = _PNPM_KEY.match(line) if in_packages else None
        if key:
            result.append(_pnpm_package(key.group(1), source))
    return _present(result)


def _gradle_lock(text: str, source: str) -> list[LockedPackage]:
    """``group:artifact:version=configurations`` lines, named for OSV's Maven
    ecosystem as ``group:artifact``."""
    result: list[LockedPackage | None] = []
    for line in text.splitlines():
        coordinate = line.split("=", 1)[0].strip()
        parts = coordinate.split(":")
        if len(parts) == 3 and not coordinate.startswith("#"):
            result.append(_locked("Maven", f"{parts[0]}:{parts[1]}", parts[2], source))
    return _present(result)


# One exact pin: ``name==1.0``, ``name[extra] == 1.0``, or ``name===1.0``.
# A wildcard (``==1.*``) or a second clause (``==1.0,!=1.0.1``) is not a pin.
REQUIREMENT_PIN = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)\s*(?:\[[^]]*\])?\s*===?\s*([^\s;#,*\\]+)(?=[\s;#\\]|$)",
    re.MULTILINE,
)


def _requirements(text: str, source: str) -> list[LockedPackage]:
    return _present(
        _locked("PyPI", match.group(1), match.group(2), source)
        for match in REQUIREMENT_PIN.finditer(text)
    )


_TOML_TABLE = re.compile(r"^\s*\[")
_TOML_PACKAGE = re.compile(r"^\s*\[\[packages?\]\]\s*(?:#.*)?$")
_TOML_NAME_OR_VERSION = re.compile(r'^\s*(name|version)\s*=\s*["\']([^"\']+)["\']')


def _toml_packages(text: str, source: str, ecosystem: str) -> list[LockedPackage]:
    """``name``/``version`` keys directly inside each ``[[package]]`` or
    ``[[packages]]`` table. Any other table header ends the package, so a
    wheel's file name (``[[packages.wheels]]``) or a ``[[patch.unused]]``
    entry never replaces the package it follows."""
    tables: list[dict[str, str] | None] = []
    for line in text.splitlines():
        if _TOML_TABLE.match(line):
            tables.append({} if _TOML_PACKAGE.match(line) else None)
            continue
        match = _TOML_NAME_OR_VERSION.match(line)
        if match and tables and tables[-1] is not None:
            tables[-1].setdefault(match.group(1), match.group(2))
    return _present(
        _locked(ecosystem, table.get("name"), table.get("version"), source)
        for table in tables
        if table is not None
    )


def _cargo_lock(text: str, source: str) -> list[LockedPackage]:
    return _toml_packages(text, source, "crates.io")


def _python_toml_lock(text: str, source: str) -> list[LockedPackage]:
    return _toml_packages(text, source, "PyPI")


def _pipfile(text: str, source: str) -> list[LockedPackage]:
    value = json.loads(text)
    if not isinstance(value, dict):
        return []
    result: list[LockedPackage | None] = []
    for section in ("default", "develop"):
        rows = value.get(section, {})
        if isinstance(rows, dict):
            result.extend(
                _pipfile_item(name, data, source) for name, data in rows.items()
            )
    return _present(result)


def _pipfile_item(name: Any, data: Any, source: str) -> LockedPackage | None:
    version = data.get("version") if isinstance(data, dict) else data
    if not isinstance(version, str) or not version.startswith("=="):
        return None
    return _locked("PyPI", name, version[2:], source)


def _composer(text: str, source: str) -> list[LockedPackage]:
    value = json.loads(text)
    if not isinstance(value, dict):
        return []
    result: list[LockedPackage] = []
    for section in ("packages", "packages-dev"):
        result.extend(_composer_rows(value.get(section, []), source))
    return result


def _composer_rows(rows: Any, source: str) -> list[LockedPackage]:
    if not isinstance(rows, list):
        return []
    return _present(
        _locked("Packagist", row.get("name"), row.get("version"), source)
        for row in rows
        if isinstance(row, dict)
    )


def _nuget(text: str, source: str) -> list[LockedPackage]:
    value = json.loads(text)
    dependencies = value.get("dependencies", {}) if isinstance(value, dict) else {}
    if not isinstance(dependencies, dict):
        return []
    return _present(
        _nuget_item(name, data, source)
        for framework in dependencies.values()
        if isinstance(framework, dict)
        for name, data in framework.items()
    )


def _nuget_item(name: Any, data: Any, source: str) -> LockedPackage | None:
    version = data.get("resolved") if isinstance(data, dict) else None
    return _locked("NuGet", name, version, source)


def _go_directives(text: str) -> Iterator[tuple[str, list[str]]]:
    """``(directive, arguments)`` for each go.mod line, inside or outside a
    ``require (`` / ``replace (`` / ``exclude (`` block, without comments."""
    block = ""
    for line in text.splitlines():
        fields = line.split("//", 1)[0].split()
        if fields == [")"]:
            block = ""
        elif fields and fields[-1].endswith("("):
            block = fields[0].rstrip("(")
        elif fields:
            yield (block, fields) if block else (fields[0], fields[1:])


def _go_mod(text: str, source: str) -> list[LockedPackage]:
    """Required module versions, after ``replace`` directives. ``exclude`` and
    ``retract`` lines name versions the build does not use."""
    required: list[tuple[str, str]] = []
    replaced: dict[str, list[str]] = {}
    for directive, arguments in _go_directives(text):
        if directive == "require" and len(arguments) >= 2:
            required.append((arguments[0], arguments[1]))
        elif directive == "replace" and "=>" in arguments:
            replaced[arguments[0]] = arguments
    return _present(
        _go_replaced(module, version, replaced.get(module), source)
        for module, version in required
        if version.startswith("v")
    )


def _go_replaced(
    module: str, version: str, replace: list[str] | None, source: str
) -> LockedPackage | None:
    """The module the build really uses: the replacement module and version,
    or nothing for a local directory replacement (that is project code)."""
    arrow = replace.index("=>") if replace else 0
    if not replace or (arrow == 2 and replace[1] != version):
        return _locked("Go", module, version, source)
    target = replace[arrow + 1 :]
    if len(target) < 2:
        return None
    return _locked("Go", target[0], target[1], source)


def _gemfile(text: str, source: str) -> list[LockedPackage]:
    """Gemfile.lock ``specs:`` entries, indented exactly four spaces."""
    result: list[LockedPackage | None] = []
    in_specs = False
    pattern = re.compile(r"^    ([^\s(]+) \(([^)]+)\)")
    for line in text.splitlines():
        if line == "  specs:":
            in_specs = True
            continue
        if line and not line.startswith("    "):
            in_specs = False
        match = pattern.match(line) if in_specs else None
        if match:
            result.append(_locked("RubyGems", match.group(1), match.group(2), source))
    return _present(result)


LockfileParser = Callable[[str, str], list[LockedPackage]]

REQUIREMENTS_NAME = re.compile(r"requirements(?:[-_.][^/]*)?\.txt", re.IGNORECASE)

LOCKFILE_PARSERS: dict[str, LockfileParser] = {
    "Cargo.lock": _cargo_lock,
    "Gemfile.lock": _gemfile,
    "Pipfile.lock": _pipfile,
    "composer.lock": _composer,
    "go.mod": _go_mod,
    "npm-shrinkwrap.json": _package_lock,
    "package-lock.json": _package_lock,
    "packages.lock.json": _nuget,
    "pnpm-lock.yaml": _pnpm_lock,
    "poetry.lock": _python_toml_lock,
    "pylock.toml": _python_toml_lock,
    "uv.lock": _python_toml_lock,
    "yarn.lock": _yarn_lock,
}


def _lists_yarn_packages(text: str) -> bool:
    return any(
        _yarn_name(line)
        for line in text.splitlines()
        if line and not line[0].isspace() and line.endswith(":")
    )


# Cheap shape checks that say "this file lists dependencies" without parsing
# versions. They catch a lockfile whose packages the parser could not read.
_LISTING_CHECKS: dict[LockfileParser, Callable[[str], object]] = {
    _cargo_lock: re.compile(r"^\s*\[\[package\]\]", re.MULTILINE).search,
    _python_toml_lock: re.compile(r"^\s*\[\[packages?\]\]", re.MULTILINE).search,
    _gemfile: re.compile(r"^ {4}\S", re.MULTILINE).search,
    _pipfile: re.compile(r'"(?:default|develop)"\s*:\s*\{\s*"').search,
    _composer: re.compile(r'"packages(?:-dev)?"\s*:\s*\[\s*\{').search,
    _go_mod: re.compile(r"^\s*require\b", re.MULTILINE).search,
    _package_lock: _lists_npm_packages,
    _nuget: re.compile(r'"resolved"\s*:').search,
    _pnpm_lock: re.compile(r"^packages:\s*\n {2}\S", re.MULTILINE).search,
    _gradle_lock: re.compile(r"^[^#\s=]+:[^:\s=]+:[^:\s=]+=", re.MULTILINE).search,
    _requirements: REQUIREMENT_PIN.search,
    _yarn_lock: _lists_yarn_packages,
}


def _parser_for(name: str) -> LockfileParser | None:
    parser = LOCKFILE_PARSERS.get(name)
    if parser is None and name.endswith("gradle.lockfile"):
        return _gradle_lock
    if parser is None and REQUIREMENTS_NAME.fullmatch(name):
        return _requirements
    return parser


def classify_lockfile(name: str) -> str | None:
    """``"supported"``, ``"unsupported"``, or None when ``name`` is not a
    lockfile this module knows."""
    if _parser_for(name) is not None:
        return "supported"
    return "unsupported" if name in KNOWN_UNSUPPORTED else None


def read_lockfile(name: str, text: str, source: str) -> LockfileReading:
    """Parse one supported lockfile's text. Raises ValueError for a file name
    without a parser and for malformed JSON."""
    parser = _parser_for(name)
    if parser is None:
        raise ValueError(f"{source}: no parser for {name}")
    packages = tuple(sorted(set(parser(text, source)), key=_package_order))
    lists = bool(_LISTING_CHECKS[parser](text))
    return LockfileReading(source, packages, lists)


def _package_order(item: LockedPackage) -> tuple[str, str, str, str]:
    return (item.ecosystem, item.name, item.version, item.source)


def discover_locked_packages(root: Path) -> DependencyInventory:
    packages: list[LockedPackage] = []
    lockfiles: list[str] = []
    unsupported: list[str] = []
    for path in sorted(_walk(root)):
        relative = path.relative_to(root).as_posix()
        kind = classify_lockfile(path.name)
        if kind == "unsupported":
            unsupported.append(relative)
        elif kind == "supported":
            lockfiles.append(relative)
            text = path.read_text(encoding="utf-8", errors="replace")
            packages.extend(read_lockfile(path.name, text, relative).packages)
    unique = sorted(set(packages), key=_package_order)
    return DependencyInventory(tuple(unique), tuple(lockfiles), tuple(unsupported))


def _osv_results(
    batch: tuple[LockedPackage, ...], opener: Callable[..., Any], timeout: int
) -> list[Any]:
    queries = [
        {
            "version": item.version,
            "package": {"ecosystem": item.ecosystem, "name": item.name},
        }
        for item in batch
    ]
    request = Request(
        OSV_BATCH_URL,
        data=json.dumps({"queries": queries}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "code-discipline"},
        method="POST",
    )
    raw = _request_osv_bytes(request, opener, timeout)
    if len(raw) > 10_000_000:
        raise ValueError("OSV response exceeds 10000000 bytes")
    value = json.loads(raw)
    results = value.get("results") if isinstance(value, dict) else None
    if not isinstance(results, list) or len(results) != len(batch):
        raise ValueError("OSV result count does not match the package query count")
    return results


def _curl_osv(request: Request, timeout: int) -> bytes:
    """POST with the system curl, for hosts whose Python lacks CA certificates.
    Every failure, including a hung process, is raised as OSError."""
    curl = shutil.which("curl")
    if curl is None:
        raise OSError("HTTPS failed and curl is unavailable")
    command = [curl, "-fsS", "--max-time", str(timeout), "--max-filesize", "10000000"]
    for header in ("Content-Type: application/json", "User-Agent: code-discipline"):
        command += ["--header", header]
    command += ["--data-binary", "@-", request.full_url]
    try:
        completed = subprocess.run(
            command,
            input=cast(bytes, request.data),
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
    except subprocess.SubprocessError as error:
        raise OSError(f"curl did not finish: {error}") from error
    if completed.returncode:
        message = completed.stderr.decode(errors="replace").strip()
        raise OSError(message or f"curl exited {completed.returncode}")
    return bytes(completed.stdout)


def _request_osv_bytes(
    request: Request, opener: Callable[..., Any], timeout: int
) -> bytes:
    try:
        with opener(request, timeout=timeout) as response:
            return bytes(response.read(10_000_001))
    except (OSError, http.client.HTTPException):
        if opener is not urlopen:
            raise
        return _curl_osv(request, timeout)


def _vulnerability_ids(result: Any) -> tuple[str, ...]:
    vulnerabilities = result.get("vulns", []) if isinstance(result, dict) else []
    if not isinstance(vulnerabilities, list):
        return ()
    identifiers = (item.get("id") for item in vulnerabilities if isinstance(item, dict))
    return tuple(item for item in identifiers if isinstance(item, str) and item)


def _findings_for_batch(
    batch: tuple[LockedPackage, ...], results: list[Any]
) -> list[VulnerabilityFinding]:
    return [
        VulnerabilityFinding(identifier, package)
        for package, result in zip(batch, results, strict=False)
        for identifier in _vulnerability_ids(result)
    ]


def query_osv(
    packages: tuple[LockedPackage, ...],
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: int = 20,
) -> tuple[VulnerabilityFinding, ...]:
    findings: list[VulnerabilityFinding] = []
    for start in range(0, len(packages), 1000):
        batch = packages[start : start + 1000]
        findings.extend(
            _findings_for_batch(batch, _osv_results(batch, opener, timeout))
        )
    return tuple(
        sorted(
            set(findings),
            key=lambda item: (item.vulnerability_id, *_package_order(item.package)),
        )
    )
