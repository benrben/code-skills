"""Progressive terminal setup for the project-wide quality profile."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Sequence, cast

from project_profile import (
    ProjectProfile,
    detect_profile,
    load_profile,
    merge_answers,
    profile_questions,
    write_profile,
)


@dataclass(frozen=True)
class RunFiles:
    config: Path
    thresholds: Path
    artifact_dir: Path
    html: Path
    state: Path
    gate_script: Path
    profile: Path
    explicit_config: bool
    explicit_thresholds: bool
    explicit_artifacts: bool
    explicit_html: bool
    explicit_gate_script: bool


def add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--init",
        action="store_true",
        help="create missing gate files and a detected project profile, then run the baseline",
    )
    parser.add_argument(
        "--setup",
        nargs="?",
        const="all",
        choices=("all", "product", "runtime", "risk"),
        help="resume progressive setup after the baseline (default group: all)",
    )
    parser.add_argument(
        "--show-profile",
        action="store_true",
        help="print a read-only detected or saved project profile and exit",
    )
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="save a setup answer; VALUE accepts JSON or plain text",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never read stdin; print pending setup questions for an agent or CI",
    )


def parse_setup_answers(values: Sequence[str]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"setup answer must use KEY=VALUE: {value}")
        key, raw = value.split("=", 1)
        if not key.strip():
            raise ValueError("setup answer key cannot be empty")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        answers[key.strip()] = parsed
    return answers


def saved_profile(path: Path) -> ProjectProfile | None:
    return load_profile(path) if path.exists() else None


def detected_profile(root: Path, path: Path) -> ProjectProfile:
    existing = saved_profile(path)
    return detect_profile(root, existing.as_dict() if existing else None)


def pending_questions(profile: ProjectProfile, group: str) -> list[dict[str, Any]]:
    questions = profile_questions(profile)
    if group == "all":
        return questions
    return [item for item in questions if item["category"] == group]


def print_pending_questions(profile: ProjectProfile, group: str) -> None:
    questions = pending_questions(profile, group)
    if not questions:
        print("Project setup has no pending questions in this group.")
        return
    print("Decisions needed for broader project-quality evidence:")
    for item in questions:
        print(f"- {item['key']}: {item['prompt']} ({item['reason']})")


def can_prompt(args: argparse.Namespace) -> bool:
    return bool(
        not args.non_interactive
        and not os.environ.get("CI")
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def prompt_answers(profile: ProjectProfile, group: str) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for item in pending_questions(profile, group):
        print(f"\n{item['prompt']}")
        print(f"Why: {item['reason']}")
        for number, option in enumerate(item["options"], 1):
            suffix = " (recommended)" if option == item["recommended"] else ""
            print(f"  {number}. {option}{suffix}")
        raw = input("Answer with a number or a value: ").strip()
        answers[item["key"]] = selected_answer(raw, item["options"])
    return answers


def selected_answer(raw: str, options: Sequence[Any]) -> Any:
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return raw or "Decide later"


def initialize_gate_files(
    gate: ModuleType,
    root: Path,
    config_path: Path,
    thresholds_path: Path,
) -> list[str]:
    written: list[str] = []
    if not thresholds_path.exists():
        gate.write_initial_thresholds(thresholds_path)
        written.append(str(thresholds_path))
    if not config_path.exists():
        gate.write_initial_config(root, config_path, gate.default_thresholds())
        written.append(str(config_path))
    rules_path = root / gate.DEPENDENCIES_NAME
    if gate.write_initial_dependencies(root, rules_path):
        written.append(str(rules_path))
    gate.ensure_git_repository(root)
    return written


def show_profile(root: Path, path: Path) -> int:
    print(detected_profile(root, path).to_json(), end="")
    return 0


def setup_requested(args: argparse.Namespace) -> bool:
    return bool(args.init or args.setup or args.answer)


def setup_before_baseline(
    args: argparse.Namespace, gate: ModuleType, root: Path, files: RunFiles
) -> ProjectProfile | None:
    if not setup_requested(args):
        return None
    profile = detected_profile(root, files.profile)
    answers = parse_setup_answers(args.answer)
    if answers:
        profile = merge_answers(profile, answers)
    if args.init or args.setup:
        written = initialize_gate_files(gate, root, files.config, files.thresholds)
        for path in written:
            print(f"Wrote {path}")
    write_profile(files.profile, profile)
    print(f"Profile: {files.profile}")
    return profile


def safe_setup_before_baseline(
    args: argparse.Namespace, gate: ModuleType, root: Path, files: RunFiles
) -> tuple[ProjectProfile | None, bool]:
    try:
        return setup_before_baseline(args, gate, root, files), True
    except (OSError, ValueError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return None, False


def active_thresholds_path(gate: ModuleType, files: RunFiles) -> Path:
    if not files.explicit_thresholds and not files.thresholds.exists():
        return cast(Path, gate.bundled_thresholds_path())
    return files.thresholds


def resolve_run_files(
    args: argparse.Namespace,
    root: Path,
    quality_directory_name: str,
    profile_name: str,
    default_gate_script: Path,
    resolve_path: Callable[[str | None, Path, Path], Path],
    resolve_artifacts: Callable[[argparse.Namespace, Path], tuple[Path, Path, Path]],
) -> RunFiles:
    quality_directory = root / quality_directory_name
    config = resolve_path(args.config, root, quality_directory / "quality-gate.json")
    thresholds = resolve_path(
        args.thresholds, root, quality_directory / "quality-thresholds.json"
    )
    artifact_dir, html, state = resolve_artifacts(args, root)
    gate_script = resolve_path(args.gate_script, root, default_gate_script)
    return RunFiles(
        config,
        thresholds,
        artifact_dir,
        html,
        state,
        gate_script,
        root / profile_name,
        args.config is not None,
        args.thresholds is not None,
        args.artifact_dir is not None,
        args.html is not None,
        args.gate_script is not None,
    )


def rerun_for(
    args: argparse.Namespace,
    root: Path,
    files: RunFiles,
    thresholds_path: Path,
    build_command: Callable[..., str],
    scope_arguments: Callable[[argparse.Namespace], list[str]],
    selection_names: Callable[[argparse.Namespace], tuple[str, ...]],
) -> str:
    return build_command(
        root,
        files.config,
        files.explicit_config,
        thresholds_path,
        files.explicit_thresholds,
        files.artifact_dir,
        files.explicit_artifacts,
        files.html,
        files.explicit_html,
        files.gate_script,
        files.explicit_gate_script,
        scope_arguments(args),
        args.no_install,
        args.fast,
        args.mutation_workers,
        selection_names(args),
    )


def refresh_project_profile_artifacts(
    gate: ModuleType,
    analysis: Any,
    root: Path,
    files: RunFiles,
    thresholds_path: Path,
    exit_code: int,
    run_error: str | None,
    write_artifacts: Callable[..., dict[str, Any]],
) -> None:
    thresholds, _notes = gate.load_thresholds(thresholds_path)
    config, _config_notes = gate.load_config(
        files.config if files.config.exists() else None, thresholds
    )
    gate.attach_project_quality(analysis, root, config)
    write_artifacts(gate, analysis, files.html, files.state, exit_code, run_error)


def setup_after_baseline(
    args: argparse.Namespace,
    gate: ModuleType,
    analysis: Any,
    root: Path,
    files: RunFiles,
    thresholds_path: Path,
    profile: ProjectProfile,
    exit_code: int,
    run_error: str | None,
    write_artifacts: Callable[..., dict[str, Any]],
) -> None:
    group = args.setup or "all"
    if not can_prompt(args):
        print_pending_questions(profile, group)
        return
    answers = prompt_answers(profile, group)
    if not answers:
        return
    write_profile(files.profile, merge_answers(profile, answers))
    refresh_project_profile_artifacts(
        gate,
        analysis,
        root,
        files,
        thresholds_path,
        exit_code,
        run_error,
        write_artifacts,
    )
    print(f"Updated project-quality coverage in {files.html}")


def print_analysis(
    args: argparse.Namespace,
    gate: ModuleType,
    analysis: Any,
    state: dict[str, Any],
    files: RunFiles,
    previous: Any,
    previous_items: Any,
    print_report: Callable[..., None],
) -> None:
    print_report(
        gate,
        analysis,
        state,
        files.state,
        files.html,
        args.print_prompt,
        previous,
        previous_items,
    )
