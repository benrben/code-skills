#!/usr/bin/env python3
"""What to fix next, one file at a time, read from the quality-loop state file.

The agent never opens the state file: ``--next`` prints the first file with open
issues and every issue in it, ``--file`` does the same for a chosen file,
``--summary`` lists files and counts, and ``--briefs N`` prints one sub-agent
brief per file for the N files with the most issues. Only issues are printed,
never hints.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gate_types import plural
from quality_model import read_state
from quality_report import (
    REPOSITORY,
    Groups,
    Issue,
    default_state_path,
    file_line,
    files_in_order,
    is_current_state,
    issues_from_state,
    metric_breakdown,
    summary_header,
)

VERSION = "2.0.0"
NOTHING_TO_FIX = "Nothing to fix: the last run listed no issues."
BRIEF_RULES = (
    "Rules: edit only the file named above and its test file. Do not run --init, "
    "do not edit .quality/, do not commit. Verify with the test file that belongs "
    "to this file. When an issue cannot be fixed without touching another file, "
    "stop and report it instead of touching that file. Reply with what you changed "
    "and what is still open, and why."
)
PARENT_LINE = (
    "Parent: wait for every sub-agent, then rerun the fast run once — "
    "the report is the evidence."
)


def summary_lines(groups: Groups) -> list[str]:
    if not groups:
        return [NOTHING_TO_FIX]
    issues = [issue for _, grouped in groups for issue in grouped]
    lines = [summary_header(issues, groups)]
    lines.extend(
        file_line(number, path, grouped)
        for number, (path, grouped) in enumerate(groups, 1)
    )
    return lines


def issue_lines(issue: Issue) -> list[str]:
    location = f"L{issue.line}" if issue.line else "—"
    lines = [f"  {location:<6} {issue.describe()}"]
    lines.extend(f"         {detail}" for detail in issue.details)
    return lines


def file_lines(path: str, issues: Sequence[Issue], others: int) -> list[str]:
    lines = [f"{path} — {plural(len(issues), 'issue')} ({metric_breakdown(issues)})"]
    for issue in issues:
        lines.extend(issue_lines(issue))
    lines.append(f"{plural(others, 'more file')} after this one.")
    return lines


def next_lines(groups: Groups) -> list[str]:
    if not groups:
        return [NOTHING_TO_FIX]
    path, issues = groups[0]
    return file_lines(path, issues, len(groups) - 1)


def find_group(groups: Groups, wanted: str) -> tuple[str, list[Issue]] | None:
    for path, issues in groups:
        if path == wanted or path.endswith("/" + wanted):
            return path, issues
    return None


def chosen_file_lines(groups: Groups, wanted: str) -> list[str]:
    found = find_group(groups, wanted)
    if found is None:
        return [f"No open issues in {wanted}.", *summary_lines(groups)]
    path, issues = found
    return file_lines(path, issues, len(groups) - 1)


def brief_lines(path: str, issues: Sequence[Issue]) -> list[str]:
    lines = [
        f"You own {path} and its test file. Fix these {plural(len(issues), 'issue')}:"
    ]
    lines.extend(f"  - {issue.text()}" for issue in issues)
    lines.append(BRIEF_RULES)
    return lines


def briefs_lines(groups: Groups, count: int) -> list[str]:
    chosen = [group for group in groups if group[0] != REPOSITORY][: max(count, 0)]
    if not chosen:
        return ["No file-level issues to delegate."]
    lines: list[str] = []
    for number, (path, issues) in enumerate(chosen, 1):
        lines.append(f"=== brief {number} of {len(chosen)}: {path} ===")
        lines.extend(brief_lines(path, issues))
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
    parser.add_argument(
        "--state", help="state file (default: <root>/.quality/quality-gate-state.json)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--next",
        action="store_true",
        help="the first file to fix and its issues (default)",
    )
    mode.add_argument("--file", help="issues for one file (path or file name)")
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
    return Path(args.state) if args.state else default_state_path(Path(args.root))


def load_groups(path: Path) -> tuple[Groups | None, str]:
    """The issues grouped by file, or None with the reason there are none."""
    state: dict[str, Any] | None = read_state(path)
    if state is None:
        return None, f"no readable state file at {path}; run the quality loop first"
    if not is_current_state(state):
        return None, f"{path} comes from an older quality loop; run the loop once"
    return files_in_order(issues_from_state(state)), ""


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    groups, problem = load_groups(state_path_for(args))
    if groups is None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    print("\n".join(render(args, groups)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
