"""Vulnerable dependencies gate: locked package versions checked against OSV.

The gate reads lockfiles as text and never installs or restores anything. It
refuses to pass when it cannot see the versions: a manifest that declares
dependencies without a lockfile fails ("add a lockfile"), a lockfile whose
packages cannot be read fails, and a lockfile format without a parser blocks.

In an incremental scope the gate runs only when a manifest or lockfile
changed, and it queries only the packages the change added or moved to a new
version, so advisories against untouched dependencies never fail an
unrelated change.
"""

from __future__ import annotations

import dataclasses
import http.client
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from diff_scope import ChangeSet
from gate_types import (
    BLOCKED,
    FAIL,
    NOT_APPLICABLE,
    PASS,
    GateItem,
    GateOutcome,
    plural,
)
from gate_vulnerabilities_manifests import (
    UNLOCKED_BLOCKS,
    UNLOCKED_FAILS,
    UNLOCKED_IS_NORMAL,
    LockIndex,
    check_manifest,
    dependency_files,
    is_dependency_file,
    lock_search_directories,
    manifest_rule,
)
from portable_vulnerabilities import (
    LockedPackage,
    VulnerabilityFinding,
    classify_lockfile,
    query_osv,
    read_lockfile,
)

KEY = "vulnerabilities"
TITLE = "Vulnerable dependencies"

OsvQuery = Callable[[tuple[LockedPackage, ...]], tuple[VulnerabilityFinding, ...]]

# Errors an OSV query raises for network, protocol, curl, size, and
# response-shape failures. Each one blocks the gate instead of crashing it.
QUERY_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    TimeoutError,
    http.client.HTTPException,
    subprocess.SubprocessError,
)


@dataclasses.dataclass
class DependencyReview:
    """Everything the gate learned before asking OSV."""

    problems: list[GateItem] = dataclasses.field(default_factory=list)
    blocked: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    lockfiles: list[str] = dataclasses.field(default_factory=list)
    unsupported: list[str] = dataclasses.field(default_factory=list)
    packages: list[LockedPackage] = dataclasses.field(default_factory=list)
    findings: tuple[VulnerabilityFinding, ...] = ()
    queried: bool = False


def evaluate_vulnerabilities(
    root: Path,
    changes: ChangeSet,
    query: OsvQuery = query_osv,
    offline: bool = False,
) -> GateOutcome:
    """Check locked dependency versions in scope against OSV.

    ``query`` is injected so tests never reach the network; ``offline`` skips
    the query and blocks when there is anything to check.
    """
    files = dependency_files(root)
    empty = _measurements(DependencyReview())
    if not files:
        return GateOutcome(
            KEY,
            TITLE,
            NOT_APPLICABLE,
            "No dependency manifest or lockfile in this repository.",
            measurements=empty,
        )
    changed = changed_dependency_files(changes)
    if changes.incremental and not changed:
        return GateOutcome(
            KEY, TITLE, PASS, "No dependency changes.", measurements=empty
        )
    review = review_dependencies(root, files, changes, changed)
    query_packages(review, query, offline)
    return build_outcome(review, changes.incremental)


def changed_dependency_files(changes: ChangeSet) -> frozenset[str]:
    """Changed or deleted manifests, lockfiles, and checksum files."""
    touched = {*changes.paths, *changes.deleted}
    return frozenset(
        path for path in touched if is_dependency_file(PurePosixPath(path).name)
    )


def review_dependencies(
    root: Path,
    files: Sequence[str],
    changes: ChangeSet,
    changed: frozenset[str],
) -> DependencyReview:
    """Read the lockfiles and manifests in scope.

    In an incremental scope a lockfile is in scope when it changed, and a
    manifest is in scope when a dependency file changed or was deleted in any
    folder its lock search reaches: its own folder, and for workspace
    ecosystems every parent folder (a removed workspace lockfile unlocks all
    of its members).
    """
    review = DependencyReview()
    changed_folders = {PurePosixPath(path).parent.as_posix() for path in changed}
    index = LockIndex.build(root, files)
    for relative in files:
        name = PurePosixPath(relative).name
        if not changes.incremental or relative in changes.files:
            _review_lockfile(root, relative, name, changes, review)
        rule = manifest_rule(name)
        if rule is None:
            continue
        searched = lock_search_directories(rule, relative)
        if not changes.incremental or changed_folders.intersection(searched):
            _review_manifest(root, relative, index, review)
    return review


def _review_lockfile(
    root: Path, relative: str, name: str, changes: ChangeSet, review: DependencyReview
) -> None:
    kind = classify_lockfile(name)
    if kind == "unsupported":
        review.unsupported.append(relative)
        review.blocked.append(
            f"{relative}: this lockfile format is not read by the built-in "
            "inventory, so its versions were not checked."
        )
        return
    if kind is None:
        return
    try:
        text = (root / relative).read_text(encoding="utf-8", errors="replace")
        reading = read_lockfile(name, text, relative)
    except OSError as error:
        review.blocked.append(f"{relative}: could not be read ({error}).")
        return
    except ValueError as error:
        review.problems.append(_unreadable_lockfile(relative, str(error)))
        return
    review.lockfiles.append(relative)
    if reading.unreadable:
        review.problems.append(_unreadable_lockfile(relative, "no package versions"))
        return
    review.packages.extend(
        _new_packages(root, name, relative, reading.packages, changes)
    )


def _new_packages(
    root: Path,
    name: str,
    relative: str,
    packages: Sequence[LockedPackage],
    changes: ChangeSet,
) -> list[LockedPackage]:
    """Packages that are new or have a new version compared with the lockfile
    before the change; every package when there is no earlier version."""
    base = changes.base_text(root, relative)
    if base is None:
        return list(packages)
    try:
        before = {
            item.coordinate for item in read_lockfile(name, base, relative).packages
        }
    except ValueError:
        return list(packages)
    return [item for item in packages if item.coordinate not in before]


def _review_manifest(
    root: Path, relative: str, index: LockIndex, review: DependencyReview
) -> None:
    try:
        check = check_manifest(root, relative, index)
    except OSError as error:
        review.blocked.append(f"{relative}: could not be read ({error}).")
        return
    except ValueError as error:
        review.problems.append(
            GateItem(
                relative,
                None,
                "",
                "manifest",
                f"unreadable ({error})",
                "valid",
            )
        )
        return
    if check.verdict == UNLOCKED_FAILS:
        review.problems.append(_unlocked_manifest(relative))
    elif check.verdict == UNLOCKED_BLOCKS:
        review.blocked.append(
            f"{relative}: dependency versions cannot be read without a lockfile."
        )
    elif check.verdict == UNLOCKED_IS_NORMAL:
        review.notes.append(
            f"{relative}: library without a lockfile; each consumer chooses its "
            "dependency versions, so they were not checked here."
        )


def _unlocked_manifest(relative: str) -> GateItem:
    return GateItem(
        relative,
        None,
        "",
        "dependency versions",
        "not locked",
        "a committed lockfile",
    )


def _unreadable_lockfile(relative: str, reason: str) -> GateItem:
    return GateItem(
        relative,
        None,
        "",
        "packages read",
        f"none ({reason})",
        "every listed package",
    )


def query_packages(review: DependencyReview, query: OsvQuery, offline: bool) -> None:
    """Ask OSV about each distinct package version once."""
    packages = unique_packages(review.packages)
    if not packages:
        return
    if offline:
        review.blocked.append(
            f"Offline: OSV was not queried for {len(packages)} locked package versions."
        )
        return
    try:
        review.findings = query(packages)
    except QUERY_ERRORS as error:
        review.blocked.append(
            f"OSV could not verify {len(packages)} locked package versions: {error}"
        )
        return
    review.queried = True


def unique_packages(packages: Iterable[LockedPackage]) -> tuple[LockedPackage, ...]:
    """One package per ecosystem, name, and version; the first lockfile that
    lists it is reported as its source."""
    seen: dict[tuple[str, str, str], LockedPackage] = {}
    for item in packages:
        seen.setdefault(item.coordinate, item)
    return tuple(seen.values())


def vulnerability_item(finding: VulnerabilityFinding) -> GateItem:
    package = finding.package
    advisory = finding.vulnerability_id
    return GateItem(
        package.source,
        None,
        f"{package.name}@{package.version} ({package.ecosystem})",
        "known vulnerability",
        advisory,
        "none",
    )


def build_outcome(review: DependencyReview, incremental: bool) -> GateOutcome:
    vulnerable = [vulnerability_item(finding) for finding in review.findings]
    items = [*review.problems, *vulnerable]
    status = FAIL if items else BLOCKED if review.blocked else PASS
    details = [item.describe() for item in items] + review.blocked + review.notes
    return GateOutcome(
        KEY,
        TITLE,
        status,
        _summary(review, len(vulnerable), incremental),
        details,
        items,
        _measurements(review),
    )


def _summary(review: DependencyReview, vulnerable: int, incremental: bool) -> str:
    checked = len(unique_packages(review.packages))
    scope = "new or changed " if incremental else ""
    if review.problems or vulnerable:
        return (
            f"{plural(vulnerable, 'known vulnerability', 'known vulnerabilities')} "
            f"and {plural(len(review.problems), 'unlocked or unreadable dependency file')}"
            "; limit 0."
        )
    if review.blocked:
        return review.blocked[0]
    if not review.queried:
        return f"No {scope}locked package versions to check."
    return (
        "OSV found no known vulnerabilities in "
        f"{plural(checked, scope + 'locked package version')} from "
        f"{plural(len(review.lockfiles), 'lockfile')}."
    )


def _measurements(review: DependencyReview) -> dict[str, Any]:
    return {
        "packages_checked": len(unique_packages(review.packages))
        if review.queried
        else 0,
        "lockfiles": list(review.lockfiles),
        "unsupported": list(review.unsupported),
        "findings": len(review.findings),
        "vulnerabilities": [
            {
                "id": finding.vulnerability_id,
                "ecosystem": finding.package.ecosystem,
                "package": finding.package.name,
                "version": finding.package.version,
                "source": finding.package.source,
            }
            for finding in review.findings
        ],
    }
