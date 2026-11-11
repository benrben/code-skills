"""Configuration-only project initialization for the quality loop."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import quality_config
from source_files import discover_source_files, walk_files


def add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--init",
        action="store_true",
        help=(
            "write missing .quality/ configuration and thresholds, then exit "
            "without running checks"
        ),
    )


def written_line(written: Path | None, default: Path, source: str = "") -> str:
    if written is None:
        return f"Kept existing {default}"
    return f"Wrote {written}{source}"


def custom_path_notes(args: argparse.Namespace) -> list[str]:
    notes = []
    for flag, value in (
        ("--config", getattr(args, "config", None)),
        ("--thresholds", getattr(args, "thresholds", None)),
    ):
        if value:
            notes.append(
                f"Note: --init writes the default .quality/ files; {flag} {value} "
                "applies only to quality runs."
            )
    return notes


def commit_count(root: Path) -> int:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(completed.stdout.strip()) if completed.returncode == 0 else 0


def init_scale_threshold(root: Path) -> int:
    """A multi-package skeleton legitimately has more files before a feature."""
    manifests = sum(1 for path in walk_files(root) if path.name == "package.json")
    return 10 + 5 * max(0, manifests - 1)


def init_scale_message(root: Path, source_config: dict[str, Any]) -> str | None:
    """Warn when the gate arrives after the code did (SKILL.md rule 1)."""
    sources = len(discover_source_files(root, source_config))
    if sources <= init_scale_threshold(root):
        return None
    if commit_count(root) <= 1:
        return (
            f"WARNING: {sources} source files exist and the gate is only now being "
            "initialized. Rule 1 puts the gate before the first feature; expect a "
            "large first report and fan it out per rule 9."
        )
    return (
        f"Existing repository: {sources} source files enter the gate now. Expect a "
        "large first report; work it with quality_items.py and rule 9's fan-out."
    )


def configured_source(config_path: Path) -> dict[str, Any]:
    config = quality_config.load_config(config_path if config_path.exists() else None)
    source = config.get("source", {})
    return source if isinstance(source, dict) else {}


def init_lines(root: Path) -> Iterator[str]:
    config_path = root / quality_config.CONFIG_NAME
    yield written_line(quality_config.write_initial_config(root), config_path)
    defaults = f" (defaults from {quality_config.bundled_thresholds_path()})"
    yield written_line(
        quality_config.write_initial_thresholds(root),
        root / quality_config.THRESHOLDS_NAME,
        defaults,
    )
    for message in (
        quality_config.ensure_git_repository(root),
        init_scale_message(root, configured_source(config_path)),
    ):
        if message:
            yield message


def run_init(root: Path, args: argparse.Namespace) -> int:
    """Write missing configuration files; never run checks or ask questions."""
    try:
        for line in init_lines(root):
            print(line)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for note in custom_path_notes(args):
        print(note)
    print(
        "No checks ran. Save the project's gate choices in "
        f"{quality_config.CONFIG_NAME}, then run the quality loop."
    )
    return 0
