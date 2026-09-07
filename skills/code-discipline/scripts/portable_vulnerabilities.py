"""Read locked dependency versions and query OSV without restoring a project."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, cast
from urllib.request import Request, urlopen

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
}
KNOWN_UNSUPPORTED = {
    "bun.lock",
    "bun.lockb",
    "cabal.project.freeze",
    "conan.lock",
    "gems.locked",
    "gradle.lockfile",
    "mix.lock",
    "pdm.lock",
    "pnpm-lock.yaml",
    "pubspec.lock",
    "renv.lock",
    "stack.yaml.lock",
    "yarn.lock",
}


@dataclass(frozen=True, slots=True)
class LockedPackage:
    ecosystem: str
    name: str
    version: str
    source: str


@dataclass(frozen=True, slots=True)
class DependencyInventory:
    packages: tuple[LockedPackage, ...]
    lockfiles: tuple[str, ...]
    unsupported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VulnerabilityFinding:
    vulnerability_id: str
    package: LockedPackage


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


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _walk(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            continue
        yield path


def _locked(
    ecosystem: str, name: Any, version: Any, source: str
) -> LockedPackage | None:
    if not isinstance(name, str):
        return None
    if not isinstance(version, str):
        return None
    name = name.strip()
    version = _normalize_version(ecosystem, version)
    if not name or not version or version in {"*", "latest"}:
        return None
    return LockedPackage(ecosystem, name, version, source)


def _normalize_version(ecosystem: str, version: str) -> str:
    normalized = version.strip()
    return normalized.lstrip("v") if ecosystem == "Go" else normalized


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _package_lock_item(location: Any, data: Any, source: str) -> LockedPackage | None:
    if not isinstance(location, str) or not location or not isinstance(data, dict):
        return None
    marker = "node_modules/"
    name = location.rsplit(marker, 1)[-1] if marker in location else ""
    return _locked("npm", name, data.get("version"), source)


def _package_lock(path: Path, root: Path) -> list[LockedPackage]:
    value = _json(path)
    packages = value.get("packages", {}) if isinstance(value, dict) else {}
    if not isinstance(packages, dict):
        return []
    source = _relative(path, root)
    return [
        item
        for location, data in packages.items()
        if (item := _package_lock_item(location, data, source)) is not None
    ]


def _requirements(path: Path, root: Path) -> list[LockedPackage]:
    result = []
    source = _relative(path, root)
    pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;#]+)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            item = _locked("PyPI", match.group(1), match.group(2), source)
            if item:
                result.append(item)
    return result


def _toml_packages(
    path: Path, root: Path, ecosystem: str, key: str = "package"
) -> list[LockedPackage]:
    source = _relative(path, root)
    result = []
    current: dict[str, str] = {}
    section = re.compile(rf"^\s*\[\[{re.escape(key)}s?\]\]\s*$")
    assignment = re.compile(r'^\s*(name|version)\s*=\s*["\']([^"\']+)["\']')
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if section.match(line):
            item = _locked(
                ecosystem, current.get("name"), current.get("version"), source
            )
            if item:
                result.append(item)
            current = {}
            continue
        match = assignment.match(line)
        if match:
            current[match.group(1)] = match.group(2)
    item = _locked(ecosystem, current.get("name"), current.get("version"), source)
    if item:
        result.append(item)
    return result


def _cargo_lock(path: Path, root: Path) -> list[LockedPackage]:
    return _toml_packages(path, root, "crates.io")


def _python_toml_lock(path: Path, root: Path) -> list[LockedPackage]:
    return _toml_packages(path, root, "PyPI")


def _pipfile(path: Path, root: Path) -> list[LockedPackage]:
    value = _json(path)
    source = _relative(path, root)
    if not isinstance(value, dict):
        return []
    result: list[LockedPackage] = []
    for section in ("default", "develop"):
        rows = value.get(section, {})
        if not isinstance(rows, dict):
            continue
        for name, data in rows.items():
            item = _pipfile_item(name, data, source)
            if item:
                result.append(item)
    return result


def _pipfile_item(name: Any, data: Any, source: str) -> LockedPackage | None:
    version = data.get("version") if isinstance(data, dict) else data
    if not isinstance(version, str) or not version.startswith("=="):
        return None
    return _locked("PyPI", name, version[2:], source)


def _composer(path: Path, root: Path) -> list[LockedPackage]:
    value = _json(path)
    source = _relative(path, root)
    if not isinstance(value, dict):
        return []
    result: list[LockedPackage] = []
    for section in ("packages", "packages-dev"):
        rows = value.get(section, [])
        if isinstance(rows, list):
            result.extend(_composer_rows(rows, source))
    return result


def _composer_rows(rows: list[Any], source: str) -> list[LockedPackage]:
    return [
        item
        for row in rows
        if isinstance(row, dict)
        if (item := _locked("Packagist", row.get("name"), row.get("version"), source))
        is not None
    ]


def _nuget(path: Path, root: Path) -> list[LockedPackage]:
    value = _json(path)
    source = _relative(path, root)
    dependencies = value.get("dependencies", {}) if isinstance(value, dict) else {}
    if not isinstance(dependencies, dict):
        return []
    result: list[LockedPackage] = []
    for framework in dependencies.values():
        if isinstance(framework, dict):
            result.extend(_nuget_framework(framework, source))
    return result


def _nuget_framework(framework: dict[Any, Any], source: str) -> list[LockedPackage]:
    return [
        item
        for name, data in framework.items()
        if (item := _nuget_item(name, data, source)) is not None
    ]


def _nuget_item(name: Any, data: Any, source: str) -> LockedPackage | None:
    version = data.get("resolved") if isinstance(data, dict) else None
    return _locked("NuGet", name, version, source)


def _go_mod(path: Path, root: Path) -> list[LockedPackage]:
    source = _relative(path, root)
    return [
        item
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if (item := _go_mod_line(line, source)) is not None
    ]


def _go_mod_line(line: str, source: str) -> LockedPackage | None:
    stripped = line.strip()
    ignored = ("//", "module ", "go ", "require (", ")")
    if not stripped or stripped.startswith(ignored):
        return None
    stripped = stripped.removeprefix("require ").strip()
    fields = stripped.split()
    if len(fields) < 2 or not fields[1].startswith("v"):
        return None
    return _locked("Go", fields[0], fields[1], source)


def _gemfile_match(
    line: str, in_specs: bool, pattern: re.Pattern[str]
) -> tuple[bool, re.Match[str] | None]:
    if line == "  specs:":
        return True, None
    if in_specs and line and not line.startswith("    "):
        in_specs = False
    return in_specs, pattern.match(line) if in_specs else None


def _gemfile(path: Path, root: Path) -> list[LockedPackage]:
    source = _relative(path, root)
    result: list[LockedPackage] = []
    in_specs = False
    pattern = re.compile(r"^    ([^\s(]+) \(([^)]+)\)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        in_specs, match = _gemfile_match(line, in_specs, pattern)
        item = _gemfile_item(match, source)
        if item:
            result.append(item)
    return result


def _gemfile_item(match: re.Match[str] | None, source: str) -> LockedPackage | None:
    if match is None:
        return None
    return _locked("RubyGems", match.group(1), match.group(2), source)


LOCKFILE_PARSERS: dict[str, Callable[[Path, Path], list[LockedPackage]]] = {
    "Cargo.lock": _cargo_lock,
    "Gemfile.lock": _gemfile,
    "Pipfile.lock": _pipfile,
    "composer.lock": _composer,
    "go.mod": _go_mod,
    "package-lock.json": _package_lock,
    "packages.lock.json": _nuget,
    "poetry.lock": _python_toml_lock,
    "pylock.toml": _python_toml_lock,
    "uv.lock": _python_toml_lock,
}


def _parser_for(name: str) -> Callable[[Path, Path], list[LockedPackage]] | None:
    parser = LOCKFILE_PARSERS.get(name)
    if parser is None and re.fullmatch(
        r"requirements(?:[-_.][^/]*)?\.txt", name, re.IGNORECASE
    ):
        return _requirements
    return parser


def discover_locked_packages(root: Path) -> DependencyInventory:
    packages: list[LockedPackage] = []
    lockfiles: list[str] = []
    unsupported: list[str] = []
    for path in sorted(_walk(root)):
        name = path.name
        relative = _relative(path, root)
        if name in KNOWN_UNSUPPORTED:
            unsupported.append(_relative(path, root))
        parser = _parser_for(name)
        if parser:
            lockfiles.append(relative)
            packages.extend(parser(path, root))
    unique = sorted(
        set(packages),
        key=lambda item: (item.ecosystem, item.name, item.version, item.source),
    )
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
    curl = shutil.which("curl")
    if curl is None:
        raise OSError("HTTPS failed and curl is unavailable")
    completed = subprocess.run(
        [
            curl,
            "-fsS",
            "--max-time",
            str(timeout),
            "--max-filesize",
            "10000000",
            "--header",
            "Content-Type: application/json",
            "--header",
            "User-Agent: code-discipline",
            "--data-binary",
            "@-",
            request.full_url,
        ],
        input=cast(bytes, request.data),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout + 5,
        check=False,
    )
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
    except OSError:
        if opener is not urlopen:
            raise
        return _curl_osv(request, timeout)


def _vulnerability_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    identifier = item.get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def _vulnerability_ids(result: Any) -> tuple[str, ...]:
    vulnerabilities = result.get("vulns", []) if isinstance(result, dict) else []
    if not isinstance(vulnerabilities, list):
        return ()
    return tuple(
        identifier
        for item in vulnerabilities
        if (identifier := _vulnerability_id(item)) is not None
    )


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
            key=lambda item: (
                item.vulnerability_id,
                item.package.ecosystem,
                item.package.name,
                item.package.version,
                item.package.source,
            ),
        )
    )
