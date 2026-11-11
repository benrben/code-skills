"""Project policy for the quality loop: thresholds, configuration, gate choices.

Two repository-owned files hold the policy: ``.quality/quality-gate.json``
(what to run) and ``.quality/quality-thresholds.json`` (the limits). The
skill ships default thresholds beside its scripts. Older files are read
through migrations here, so the rest of the loop sees one current shape.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gate_tests_command import infer_test_command
from quality_model import GATE_ORDER, write_json_atomic
from source_files import DEFAULT_TEST_PATTERNS

VERSION = "7.0.0"
QUALITY_DIRECTORY = ".quality"
CONFIG_NAME = f"{QUALITY_DIRECTORY}/quality-gate.json"
THRESHOLDS_NAME = f"{QUALITY_DIRECTORY}/quality-thresholds.json"
STATE_NAME = "quality-gate-state.json"
REPORT_NAME = "quality-gate-report.html"
THRESHOLDS_SCHEMA_VERSION = 3

# Rule kinds for threshold values: "zero" must stay 0, "percent" is a number
# from 0 to 100, and an int rule is a whole number at least that large.
ZERO = "zero"
PERCENT = "percent"
THRESHOLD_RULES: dict[str, dict[str, str | int]] = {
    "shortcuts": {"max_findings": ZERO},
    "secrets": {"max_findings": ZERO},
    "file_loc": {"max_lines": 1},
    "project_checks": {"max_failures": ZERO},
    "tests": {"min_executed": 1, "max_skipped": ZERO, "max_failures": ZERO},
    "coverage": {"line_coverage_limit": PERCENT, "branch_coverage_limit": PERCENT},
    "complexity": {
        "cyclomatic_limit": 1,
        "cognitive_limit": 1,
        "nesting_limit": 1,
        "crap_limit": 1,
    },
    "duplication": {"min_tokens": 10, "min_lines": 2, "max_new_clones": ZERO},
    "dead_code": {"max_findings": ZERO},
    "vulnerabilities": {"max_findings": ZERO},
}

# (old section, old key) -> (new section, new key) for schema 2 files.
V2_THRESHOLD_MOVES: tuple[tuple[tuple[str, str], tuple[str, str]], ...] = (
    (("metrics", "coverage_limit"), ("coverage", "line_coverage_limit")),
    (("metrics", "branch_coverage_limit"), ("coverage", "branch_coverage_limit")),
    (("metrics", "complexity_limit"), ("complexity", "cyclomatic_limit")),
    (("metrics", "craap_limit"), ("complexity", "crap_limit")),
    (("metrics", "crap_limit"), ("complexity", "crap_limit")),
    (
        ("portable_analysis", "max_cognitive_complexity"),
        ("complexity", "cognitive_limit"),
    ),
    (("portable_analysis", "max_nesting_depth"), ("complexity", "nesting_limit")),
    (("file_loc", "max_lines"), ("file_loc", "max_lines")),
    (("test_execution", "min_executed"), ("tests", "min_executed")),
    (("test_execution", "max_skipped"), ("tests", "max_skipped")),
)

# Old gate keys and the new gates they now choose.
LEGACY_GATES: dict[str, tuple[str, ...]] = {
    "quality": ("tests", "coverage", "complexity"),
    "test_execution": ("tests",),
    "format_lint": ("project_checks",),
    "types": ("project_checks",),
    "scope": ("shortcuts",),
    "error_handling": ("shortcuts", "coverage"),
    "cognitive": ("complexity",),
}
REMOVED_GATES = frozenset(
    {
        "gherkin",
        "smoke",
        "mutation",
        "flaky",
        "slow_tests",
        "contracts",
        "dependencies",
        "dependency_health",
        "hotspots",
        "test_integrity",
        "extensibility",
    }
)

DEFAULT_TEST_TIMEOUT = 900
DEFAULT_CHECK_TIMEOUT = 600


# --- Thresholds ---------------------------------------------------------------


def bundled_thresholds_path() -> Path:
    """The default thresholds shipped with the skill (or beside a standalone
    copy of the scripts)."""
    directory = Path(__file__).resolve().parent
    candidates = (
        directory.parent / "quality-thresholds.json",
        directory / "quality-thresholds.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(
        "Bundled quality thresholds were not found. Copy quality-thresholds.json "
        "beside the quality scripts or pass --thresholds PATH."
    )


def default_thresholds() -> dict[str, Any]:
    thresholds = _read_json_object(bundled_thresholds_path(), "thresholds")
    validate_thresholds(thresholds)
    return thresholds


def load_thresholds(path: Path | None) -> dict[str, Any]:
    """The thresholds in ``path`` (the bundled defaults when None).

    A schema 2 file is migrated in memory; a schema 3 file that predates a
    goal gets the bundled default for it. Raises ValueError when the result
    is not a valid schema 3 document."""
    if path is None:
        return default_thresholds()
    loaded = _read_json_object(path, "thresholds")
    if loaded.get("schema_version") == 2:
        loaded = migrate_thresholds(loaded)
    else:
        loaded = _complete_thresholds(loaded)
    try:
        validate_thresholds(loaded)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    return loaded


def migrate_thresholds(old: Mapping[str, Any]) -> dict[str, Any]:
    """A schema 3 document from a schema 2 one: kept goals move to their new
    sections, every other goal comes from the bundled defaults."""
    migrated = default_thresholds()
    for (old_section, old_key), (new_section, new_key) in V2_THRESHOLD_MOVES:
        section = old.get(old_section)
        if isinstance(section, Mapping) and old_key in section:
            migrated[new_section][new_key] = section[old_key]
    return migrated


def sync_thresholds_file(path: Path) -> bool:
    """Rewrite an older thresholds file as schema 3. True when it changed."""
    loaded = _read_json_object(path, "thresholds")
    if loaded.get("schema_version") == THRESHOLDS_SCHEMA_VERSION:
        return False
    write_json_atomic(path, load_thresholds(path))
    return True


def validate_thresholds(thresholds: Mapping[str, Any]) -> None:
    """Raise ValueError unless ``thresholds`` is a complete schema 3 document."""
    _check_keys("thresholds", thresholds, {"schema_version", *THRESHOLD_RULES})
    if thresholds["schema_version"] != THRESHOLDS_SCHEMA_VERSION:
        raise ValueError(
            f"threshold schema_version must be {THRESHOLDS_SCHEMA_VERSION}"
        )
    for section, rules in THRESHOLD_RULES.items():
        values = thresholds[section]
        if not isinstance(values, Mapping):
            raise ValueError(f"threshold section {section} must be a JSON object")
        _check_keys(f"threshold section {section}", values, set(rules))
        for key, rule in rules.items():
            _check_value(f"{section}.{key}", values[key], rule)


def _check_keys(label: str, value: Mapping[str, Any], expected: set[str]) -> None:
    problems = []
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        problems.append(f"missing keys: {', '.join(missing)}")
    if unknown:
        problems.append(f"unknown keys: {', '.join(unknown)}")
    if problems:
        raise ValueError(f"invalid {label} ({'; '.join(problems)})")


def _check_value(name: str, value: Any, rule: str | int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"threshold {name} must be a number")
    if rule == PERCENT:
        if not 0 <= value <= 100:
            raise ValueError(f"threshold {name} must be between 0 and 100")
        return
    if not isinstance(value, int):
        raise ValueError(f"threshold {name} must be a whole number")
    if rule == ZERO and value != 0:
        raise ValueError(f"threshold {name} must remain 0 for certification")
    if isinstance(rule, int) and value < rule:
        raise ValueError(f"threshold {name} must be at least {rule}")


def _complete_thresholds(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """Add goals a schema 3 file predates; owned values always win."""
    completed = dict(loaded)
    for section, goals in default_thresholds().items():
        current = loaded.get(section)
        if section not in loaded:
            completed[section] = goals
        elif isinstance(current, Mapping) and isinstance(goals, Mapping):
            completed[section] = {**goals, **current}
    return completed


# --- Configuration ------------------------------------------------------------


def default_config() -> dict[str, Any]:
    return {
        "gates": {},
        "source": {},
        "test": {"command": None, "timeout_seconds": DEFAULT_TEST_TIMEOUT},
        "project_checks": {"timeout_seconds": DEFAULT_CHECK_TIMEOUT},
        "coverage": {},
        "tools": {"auto_install": True, "cache_dir": None},
    }


def load_config(path: Path | None) -> dict[str, Any]:
    """The configuration in ``path`` over the defaults (defaults when None).

    Legacy coverage settings become a ``coverage`` adapter only when they
    describe one command and its report; otherwise the recipe is used."""
    if path is None:
        return default_config()
    loaded = _read_json_object(path, "configuration")
    if not isinstance(loaded.get("gates", {}), Mapping):
        raise ValueError("gates must be an object mapping gate keys to true or false")
    config = deep_merge(default_config(), loaded)
    if not loaded.get("coverage"):
        config["coverage"] = legacy_coverage_adapter(loaded.get("metrics"))
    return config


def legacy_coverage_adapter(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        return {}
    commands = metrics.get("coverage_commands")
    report = metrics.get("coverage_report")
    if isinstance(commands, list) and len(commands) == 1:
        commands = commands[0]
    if not commands or not report or not _is_one_command(commands):
        return {}
    return {
        "command": commands,
        "report": report,
        "format": str(metrics.get("coverage_format") or "auto"),
    }


def _is_one_command(value: Any) -> bool:
    if isinstance(value, str):
        return True
    return isinstance(value, list) and all(isinstance(part, str) for part in value)


def gate_choices(config: Mapping[str, Any]) -> tuple[dict[str, bool], list[str]]:
    """Which of the ten gates are on, plus notes about ignored legacy keys.

    Legacy keys choose the gates they became; when several legacy keys choose
    one gate, it stays on if any of them is on. A new key set explicitly wins
    over every legacy key."""
    choices = config.get("gates") or {}
    if not isinstance(choices, Mapping):
        raise ValueError("gates must be an object mapping gate keys to true or false")
    notes: list[str] = []
    legacy = _legacy_choices(choices, notes)
    enabled = {key: bool(choices.get(key, legacy.get(key, True))) for key in GATE_ORDER}
    return enabled, notes


def _legacy_choices(choices: Mapping[str, Any], notes: list[str]) -> dict[str, bool]:
    """New gate keys chosen by legacy keys; validates every key and value."""
    legacy: dict[str, bool] = {}
    for key, value in choices.items():
        if not isinstance(value, bool):
            raise ValueError(f"gates.{key} must be true or false")
        if key in REMOVED_GATES:
            notes.append(f"ignored removed gate {key}")
        elif key in LEGACY_GATES:
            for target in LEGACY_GATES[key]:
                legacy[target] = legacy.get(target, False) or value
        elif key not in GATE_ORDER:
            raise ValueError(f"unknown gate {key!r} in gates")
    return legacy


def config_template(root: Path) -> dict[str, Any]:
    """The configuration ``--init`` writes: every gate on, nothing hidden."""
    return {
        "gates": {key: True for key in GATE_ORDER},
        "source": {"include": [], "exclude": list(DEFAULT_TEST_PATTERNS)},
        "test": {
            "command": infer_test_command(root),
            "timeout_seconds": DEFAULT_TEST_TIMEOUT,
        },
        "project_checks": {"commands": [], "timeout_seconds": DEFAULT_CHECK_TIMEOUT},
        "coverage": {},
        "tools": {"auto_install": True},
    }


def write_initial_config(root: Path) -> Path | None:
    """Write ``.quality/quality-gate.json``; None when it already exists."""
    path = root / CONFIG_NAME
    if path.exists():
        return None
    write_json_atomic(path, config_template(root))
    return path


def write_initial_thresholds(root: Path) -> Path | None:
    """Write ``.quality/quality-thresholds.json``; None when it already exists."""
    path = root / THRESHOLDS_NAME
    if path.exists():
        return None
    write_json_atomic(path, default_thresholds())
    return path


def ensure_git_repository(root: Path) -> str | None:
    """Create a git repository at ``root`` when none exists; the line to print."""
    if (root / ".git").exists():
        return None
    try:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return "git is not available here: commit each green step once it is."
    return "Initialized an empty git repository: commit each green step."


# --- Helpers ------------------------------------------------------------------


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} {path} must contain a JSON object")
    return loaded
