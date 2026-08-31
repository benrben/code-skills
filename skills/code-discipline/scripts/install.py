#!/usr/bin/env python3
"""Install or update the self-contained code-discipline skill from GitHub."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid


VERSION = "1.0.0"
GITHUB_REPOSITORY = "benrben/code-skills"
DEFAULT_REF = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}"
SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "quality-thresholds.json",
    "references/repository-setup.md",
    "scripts/install.py",
    "scripts/quality_loop.py",
    "scripts/repo_quality_gate.py",
)
PYTHON_FILES = (
    "scripts/install.py",
    "scripts/quality_loop.py",
    "scripts/repo_quality_gate.py",
)


def validate_ref(reference: str) -> str:
    if (
        not reference
        or reference.startswith("/")
        or ".." in reference.split("/")
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", reference)
    ):
        raise argparse.ArgumentTypeError(
            "ref may contain only letters, digits, '.', '_', '-', and '/'"
        )
    return reference


def raw_url(reference: str, relative_path: str) -> str:
    return (
        f"{RAW_BASE}/{quote(reference, safe='/')}/skills/code-discipline/"
        f"{quote(relative_path, safe='/')}"
    )


def download_file(url: str, maximum_bytes: int = 2_000_000) -> bytes:
    request = Request(url, headers={"User-Agent": f"code-discipline-installer/{VERSION}"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(maximum_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        curl = shutil.which("curl")
        if curl is None:
            raise RuntimeError(f"could not download {url}: {error}") from error
        try:
            completed = subprocess.run(
                [
                    curl,
                    "-fsSL",
                    "--max-time",
                    "30",
                    "--max-filesize",
                    str(maximum_bytes),
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=35,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as curl_error:
            raise RuntimeError(
                f"could not download {url} with Python HTTPS or curl: {curl_error}"
            ) from curl_error
        if completed.returncode != 0:
            details = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"could not download {url} with Python HTTPS or curl: "
                f"{details or f'curl exited {completed.returncode}'}"
            ) from error
        payload = completed.stdout
    if len(payload) > maximum_bytes:
        raise RuntimeError(f"download exceeds {maximum_bytes} bytes: {url}")
    return payload


def download_skill(reference: str) -> dict[str, bytes]:
    return {
        relative: download_file(raw_url(reference, relative))
        for relative in SKILL_FILES
    }


def write_staged_skill(directory: Path, payloads: Mapping[str, bytes]) -> None:
    missing = sorted(set(SKILL_FILES) - set(payloads))
    unknown = sorted(set(payloads) - set(SKILL_FILES))
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise RuntimeError("invalid skill payload (" + "; ".join(details) + ")")
    for relative, payload in payloads.items():
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if relative in PYTHON_FILES:
            destination.chmod(0o755)


def validate_staged_skill(directory: Path) -> tuple[str, str]:
    skill_text = (directory / "SKILL.md").read_text(encoding="utf-8")
    if not skill_text.startswith("---\n") or "\nname: code-discipline\n" not in skill_text:
        raise RuntimeError("downloaded SKILL.md is not code-discipline")
    thresholds = json.loads(
        (directory / "quality-thresholds.json").read_text(encoding="utf-8")
    )
    if not isinstance(thresholds, dict) or thresholds.get("schema_version") != 1:
        raise RuntimeError("downloaded quality-thresholds.json is invalid")
    for relative in PYTHON_FILES:
        source = (directory / relative).read_text(encoding="utf-8")
        ast.parse(source, filename=relative)
    versions = []
    for relative in (
        "scripts/repo_quality_gate.py",
        "scripts/quality_loop.py",
    ):
        completed = subprocess.run(
            [sys.executable, str(directory / relative), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).strip()
            raise RuntimeError(f"downloaded {relative} failed validation: {output}")
        versions.append(completed.stdout.strip())
    return versions[0], versions[1]


def path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def managed_skill(path: Path) -> bool:
    try:
        text = (path.resolve() / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return text.startswith("---\n") and "\nname: code-discipline\n" in text


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def install_skill(
    target: Path,
    payloads: Mapping[str, bytes],
    update: bool,
) -> tuple[str, str]:
    # Keep the lexical destination. Resolving an existing symlink here could
    # replace the shared source it points at instead of the requested install.
    target = target.absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = path_exists(target)
    if exists and not update:
        raise RuntimeError(f"skill already exists; use --update-current: {target}")
    if exists and not managed_skill(target):
        raise RuntimeError(f"refusing to replace an unmanaged path: {target}")
    staging = Path(tempfile.mkdtemp(prefix=".code-discipline-install-", dir=target.parent))
    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    moved_existing = False
    try:
        write_staged_skill(staging, payloads)
        versions = validate_staged_skill(staging)
        if exists:
            os.replace(target, backup)
            moved_existing = True
        os.replace(staging, target)
    except Exception:
        if moved_existing:
            if path_exists(target):
                remove_path(target)
            os.replace(backup, target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if path_exists(backup):
        remove_path(backup)
    return versions


def claude_link_for(target: Path, scope_root: Path) -> Path:
    return scope_root / ".claude" / "skills" / "code-discipline"


def validate_claude_link(link: Path, target: Path) -> None:
    if not path_exists(link):
        return
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    raise RuntimeError(f"refusing to replace existing Claude skill path: {link}")


def ensure_claude_link(link: Path, target: Path) -> None:
    if path_exists(link):
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(target, link.parent)
    link.symlink_to(relative, target_is_directory=True)


def repo_target(root: Path) -> tuple[Path, Path]:
    resolved = root.resolve()
    return resolved / ".agents" / "skills" / "code-discipline", resolved


def global_target() -> tuple[Path, Path]:
    home = Path.home().resolve()
    return home / ".agents" / "skills" / "code-discipline", home


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--repo", action="store_true", help="install in ROOT/.agents/skills"
    )
    destination.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="install in ~/.agents/skills",
    )
    destination.add_argument(
        "--update-current",
        action="store_true",
        help="update the skill containing this script",
    )
    parser.add_argument("--root", default=".", help="repository root for --repo")
    parser.add_argument("--ref", default=DEFAULT_REF, type=validate_ref, help="Git tag, branch, or commit")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.update_current:
            target = Path(__file__).resolve().parent.parent
            update = True
            link = None
        elif args.repo:
            target, scope_root = repo_target(Path(args.root))
            update = False
            link = claude_link_for(target, scope_root)
        else:
            target, scope_root = global_target()
            update = False
            link = claude_link_for(target, scope_root)
        if link:
            validate_claude_link(link, target)
        payloads = download_skill(args.ref)
        core_version, loop_version = install_skill(target, payloads, update)
        if link:
            ensure_claude_link(link, target)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, SyntaxError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    action = "Updated" if update else "Installed"
    print(f"{action} code-discipline at {target}")
    print(f"Quality engine {core_version}; quality loop {loop_version}")
    if link:
        print(f"Claude link: {link}")
    print("Repository quality configuration was not overwritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
