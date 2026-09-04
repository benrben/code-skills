#!/usr/bin/env python3
"""Refresh a repository's pinned engine and render its saved quality measurements."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence, cast, get_args, get_origin, get_type_hints

import quality_loop

import repo_quality_gate as gate

UPSTREAM = "https://github.com/benrben/code-skills.git"
SKILL_PATH = "skills/code-discipline"


def repository_root(target: Path) -> Path | None:
    if target.parent.name != "skills" or target.parent.parent.name != ".agents":
        return None
    root = target.parent.parent.parent.resolve()
    if (root / ".quality").is_dir():
        return root
    return None


def git(directory: Path, *arguments: str) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(directory), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Toolchain Git {arguments[0]} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def read_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Toolchain manifest must be an object: {path}")
    if (
        manifest.get("repository") != UPSTREAM
        or manifest.get("skillPath") != SKILL_PATH
    ):
        raise ValueError(f"Unsupported toolchain repository or skillPath: {path}")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", str(manifest.get("revision", ""))):
        raise ValueError(f"Toolchain revision must be a full commit: {path}")
    return manifest


def validate_cache(cache: Path, revision: str) -> None:
    validate_checkout_root(cache)
    if git(cache, "config", "--get", "remote.origin.url") != UPSTREAM:
        raise ValueError(f"Toolchain origin does not match: {cache}")
    if git(cache, "rev-parse", "HEAD") != revision.lower():
        raise ValueError(f"Toolchain revision does not match its manifest: {cache}")
    if git(cache, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(
            f"Toolchain has local changes; preserve them before updating: {cache}"
        )
    validate_entrypoint(cache)


def validate_checkout_root(cache: Path) -> None:
    if cache.is_symlink() or not (cache / ".git").is_dir():
        raise ValueError(f"Toolchain must be an isolated checkout: {cache}")
    if Path(git(cache, "rev-parse", "--show-toplevel")).resolve() != cache.resolve():
        raise ValueError(f"Toolchain checkout root does not match: {cache}")


def validate_entrypoint(cache: Path) -> None:
    script = cache / SKILL_PATH / "scripts/quality_loop.py"
    if not script.is_file() or not script.resolve().is_relative_to(cache.resolve()):
        raise ValueError(
            f"Toolchain entry point is missing or outside its checkout: {script}"
        )


def stage_toolchain(staging: Path, commit: str) -> None:
    git(staging, "init", "--quiet")
    git(staging, "remote", "add", "origin", UPSTREAM)
    git(staging, "fetch", "--no-tags", "--depth=1", "origin", commit)
    git(staging, "checkout", "--detach", "--quiet", commit)
    validate_cache(staging, commit)


def replace_toolchain(
    cache: Path, staging: Path, manifest_path: Path, manifest: dict[str, Any]
) -> None:
    backup = staging.parent / "previous"
    had_cache = cache.exists()
    if had_cache:
        cache.rename(backup)
    try:
        staging.rename(cache)
        gate.write_json_atomic(manifest_path, manifest)
    except Exception:
        if cache.exists():
            cache.rename(staging)
        if had_cache:
            backup.rename(cache)
        raise


def update_toolchain(root: Path, commit: str) -> None:
    manifest_path = root / ".quality/toolchain.json"
    if not manifest_path.exists():
        return
    manifest = read_manifest(manifest_path)
    cache = manifest_path.with_suffix("")
    if os.path.lexists(cache):
        validate_cache(cache, manifest["revision"])
    if cache.exists() and manifest["revision"].lower() == commit:
        return
    temporary = Path(
        tempfile.mkdtemp(prefix="toolchain-staging-", dir=manifest_path.parent)
    )
    try:
        staging = temporary / "next"
        staging.mkdir()
        stage_toolchain(staging, commit)
        replace_toolchain(
            cache, staging, manifest_path, {**manifest, "revision": commit}
        )
    except Exception as error:
        raise RuntimeError(
            f"Toolchain refresh failed; staging retained at {temporary}: {error}"
        ) from error
    shutil.rmtree(temporary)
    print(f"Updated pinned toolchain at {cache} to {commit}")


def restore_value(annotation: Any, value: Any) -> Any:
    if dataclasses.is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return cast(Any, annotation)(
            **{key: restore_value(hints[key], item) for key, item in value.items()}
        )
    origin = get_origin(annotation)
    if origin in (list, tuple):
        element_type = get_args(annotation)[0]
        return origin(restore_value(element_type, item) for item in value)
    return value


def refresh_report(root: Path) -> None:
    state_path = root / ".quality/quality-gate-state.json"
    if not state_path.is_file():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if "report" not in state:
        print(f"Existing HTML needs one quality-loop run to save render data: {root}")
        return
    report = restore_value(gate.AnalysisReport, state["report"])
    if Path(report.root).resolve() != root.resolve():
        raise ValueError(f"Saved report belongs to another repository: {state_path}")
    destination = root / ".quality/quality-gate-report.html"
    rendered = gate.html_report(report)
    with tempfile.TemporaryDirectory(
        prefix=".report-update-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "report.html"
        staging.write_text(rendered, encoding="utf-8")
        os.replace(staging, destination)
    print(
        f"Refreshed HTML at {destination} (measurements unchanged: {report.generated_at})"
    )


def update_repository(target: Path, commit: str) -> None:
    root = repository_root(target)
    if root is None:
        return
    with quality_loop.repository_run_lock(root):
        update_toolchain(root, commit)
        refresh_report(root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, type=Path)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-f0-9]{40}", args.ref):
            raise ValueError("Update requires a full commit")
        update_repository(args.skill, args.ref)
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Repository update failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
