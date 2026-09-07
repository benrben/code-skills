#!/usr/bin/env python3
"""What to fix next, one file at a time, read from the quality-loop state file.

The agent never opens the state file: ``--next`` prints the first file with open
items and every item in it, ``--file`` does the same for a chosen file,
``--summary`` lists files and counts, and ``--briefs N`` prints one sub-agent
brief per file for the N files with the most items.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quality_report import (  # noqa: E402
    REPOSITORY,
    file_line,
    files_in_order,
    item_records,
    metric_breakdown,
)

VERSION = "1.0.0"
STATE_NAME = ".quality/quality-gate-state.json"
NOTHING_TO_FIX = "Nothing to fix: the last run listed no items."
BRIEF_RULES = (
    "Rules: edit only the files named above. Do not run --init, do not edit .quality/, "
    "do not commit. Verify with the test file that belongs to this file. When an item "
    "cannot be fixed without touching another file, stop and report it instead of "
    "touching that file. Reply with what you changed and what is still open, and why."
)
PARENT_LINE = (
    "Parent: wait for every sub-agent, then rerun the fast run once — "
    "the report is the evidence."
)

Groups = list[tuple[str, list[dict[str, Any]]]]


def load_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return state


def groups_from_state(state: dict[str, Any]) -> Groups:
    return files_in_order(item_records(state))


def summary_lines(groups: Groups) -> list[str]:
    if not groups:
        return [NOTHING_TO_FIX]
    total = sum(len(items) for _, items in groups)
    lines = [f"{total} items in {len(groups)} files — one file per cycle, top first"]
    lines.extend(
        file_line(number, path, items) for number, (path, items) in enumerate(groups, 1)
    )
    return lines


def item_lines(record: dict[str, Any]) -> list[str]:
    location = f"L{record['line']}" if record["line"] > 0 else "—"
    lines = [f"  {location:<6} {record['text']}", f"         → {record['hint']}"]
    lines.extend(f"         {detail}" for detail in record.get("details", []))
    return lines


def file_lines(path: str, records: Sequence[dict[str, Any]], others: int) -> list[str]:
    noun = "item" if len(records) == 1 else "items"
    lines = [f"{path} — {len(records)} {noun} ({metric_breakdown(records)})"]
    for record in records:
        lines.extend(item_lines(record))
    lines.append(
        f"When this file is green, rerun the fast run. {others} more files after this one."
    )
    return lines


def next_lines(groups: Groups) -> list[str]:
    if not groups:
        return [NOTHING_TO_FIX]
    path, records = groups[0]
    return file_lines(path, records, len(groups) - 1)


def find_group(groups: Groups, wanted: str) -> tuple[str, list[dict[str, Any]]] | None:
    for path, records in groups:
        if path == wanted or path.endswith("/" + wanted):
            return path, records
    return None


def chosen_file_lines(groups: Groups, wanted: str) -> list[str]:
    found = find_group(groups, wanted)
    if found is None:
        return [f"No open items in {wanted}."] + summary_lines(groups)
    path, records = found
    return file_lines(path, records, len(groups) - 1)


def brief_lines(path: str, records: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        f"You own {path} and its test file. Bring these {len(records)} items to green:"
    ]
    lines.extend(f"  - {record['text']} → {record['hint']}" for record in records)
    lines.append(BRIEF_RULES)
    return lines


def briefs_lines(groups: Groups, count: int) -> list[str]:
    chosen = [group for group in groups if group[0] != REPOSITORY][:count]
    if not chosen:
        return ["No file-level items to delegate."]
    lines: list[str] = []
    for number, (path, records) in enumerate(chosen, 1):
        lines.append(f"=== brief {number} of {len(chosen)}: {path} ===")
        lines.extend(brief_lines(path, records))
    lines.append(PARENT_LINE)
    return lines


def render(args: argparse.Namespace, groups: Groups) -> list[str]:
    if args.summary:
        return summary_lines(groups)
    if args.file:
        return chosen_file_lines(groups, args.file)
    if args.briefs is not None:
        return briefs_lines(groups, args.briefs)
    return next_lines(groups)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--state", help=f"state file (default: <root>/{STATE_NAME})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--next",
        action="store_true",
        help="the first file to fix and its items (default)",
    )
    mode.add_argument("--file", help="items for one file (path or file name)")
    mode.add_argument("--summary", action="store_true", help="files and counts only")
    mode.add_argument(
        "--briefs",
        type=int,
        metavar="N",
        help="one sub-agent brief per file, top N files",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def state_path_for(args: argparse.Namespace) -> Path:
    return Path(args.state) if args.state else Path(args.root) / STATE_NAME


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = state_path_for(args)
    try:
        state = load_state(state_path)
    except (OSError, ValueError) as error:
        print(f"error: cannot read {state_path}: {error}", file=sys.stderr)
        print("Run the quality loop first; it writes the state file.", file=sys.stderr)
        return 2
    print("\n".join(render(args, groups_from_state(state))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
