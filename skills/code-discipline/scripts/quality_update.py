#!/usr/bin/env python3
"""Refresh a repository's pinned toolchain and re-render its saved HTML report.

The installer runs this after an update. The HTML is rendered again from the
saved state file, so measurements, timestamps, and pass/fail results stay as
they were. A state file from an older quality loop is left alone: one loop run
writes a current one.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from quality_config import QUALITY_DIRECTORY, REPORT_NAME, STATE_NAME
from quality_model import read_state, write_json_atomic
from quality_report import is_current_state, render_html

UPSTREAM = "https://github.com/benrben/code-skills.git"
SKILL_PATH = "skills/code-discipline"
LEGACY_STATE_MESSAGE = (
    "Saved state comes from an older quality loop; run the loop once to "
    "refresh the HTML report"
)

RunLock = Callable[[Path], contextlib.AbstractContextManager[None]]


def repository_root(target: Path) -> Path | None:
    """The repository that holds a repo-local skill install, if it has .quality/."""
    if target.parent.name != "skills" or target.parent.parent.name != ".agents":
        return None
    root = target.parent.parent.parent.resolve()
    if (root / QUALITY_DIRECTORY).is_dir():
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
    manifest = read_state(path)
    if manifest is None:
        raise ValueError(f"Toolchain manifest must be a JSON object: {path}")
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
        write_json_atomic(manifest_path, manifest)
    except Exception:
        if cache.exists():
            cache.rename(staging)
        if had_cache:
            backup.rename(cache)
        raise


def update_toolchain(root: Path, commit: str) -> None:
    manifest_path = root / QUALITY_DIRECTORY / "toolchain.json"
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


def write_text_atomic(destination: Path, text: str) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".report-update-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / destination.name
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, destination)


def refresh_report(root: Path) -> int:
    """Re-render the HTML report from the saved state; 0 unless it is foreign."""
    quality = root / QUALITY_DIRECTORY
    state_path = quality / STATE_NAME
    if not state_path.is_file():
        return 0
    state = read_state(state_path)
    if not is_current_state(state):
        print(f"{LEGACY_STATE_MESSAGE}: {root}")
        return 0
    saved_root = Path(str(state.get("root") or ""))
    if saved_root.resolve() != root.resolve():
        print(
            f"Saved state belongs to another repository: {state_path}",
            file=sys.stderr,
        )
        return 2
    destination = quality / REPORT_NAME
    write_text_atomic(destination, render_html(state))
    print(
        f"Refreshed HTML at {destination} "
        f"(measurements unchanged: {state.get('generated_at', '')})"
    )
    return 0


@contextlib.contextmanager
def loop_run_lock(root: Path) -> Iterator[None]:
    """The quality loop's repository lock, so an update never overlaps a run."""
    import quality_loop

    with quality_loop.repository_run_lock(root):
        yield


def update_repository(
    target: Path, commit: str, *, run_lock: RunLock = loop_run_lock
) -> int:
    root = repository_root(target)
    if root is None:
        return 0
    with run_lock(root):
        update_toolchain(root, commit)
        return refresh_report(root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, type=Path)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-f0-9]{40}", args.ref):
            raise ValueError("Update requires a full commit")
        return update_repository(args.skill, args.ref)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Repository update failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
