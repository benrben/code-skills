#!/usr/bin/env python3
"""Portable, language-neutral repository quality gate.

Copy this file into a repository and run:

    python3 repo_quality_gate.py --root .

The runner auto-detects common toolchains and also accepts commands that emit
normalized JSON, which makes every gate extensible to arbitrary languages. It
installs missing coverage and complexity tools into isolated caches by default,
writes a self-contained HTML report, and exits non-zero unless every applicable
gate can be measured and passes.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import fnmatch
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from typing import Any, Iterable, Sequence
import xml.etree.ElementTree as ET


VERSION = "3.1.0"
CONFIG_NAME = ".quality-gate.json"
DEFAULT_REPORT = "quality-gate-report.html"

SOURCE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".cs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".swift",
    ".scala",
    ".lua",
    ".ex",
    ".exs",
    ".dart",
    ".groovy",
    ".sol",
    ".zig",
}

LIZARD_EXTENSIONS = {
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".mjs",
    ".cjs",
    ".php",
    ".rb",
    ".rs",
    ".scala",
    ".sol",
    ".swift",
    ".ts",
    ".tsx",
    ".zig",
}

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
    "out",
    "bin",
    "obj",
    "coverage",
    "htmlcov",
    ".coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    ".next",
    ".nuxt",
}

DEFAULT_TEST_PATTERNS = (
    "tests/**",
    "test/**",
    "spec/**",
    "features/**",
    "e2e/**",
    "qa/**",
    "**/tests/**",
    "**/test/**",
    "**/spec/**",
    "**/__tests__/**",
    "**/*_test.*",
    "**/test_*.*",
    "**/*.test.*",
    "**/*.spec.*",
)

OPERATOR_MUTATIONS = {
    "==": "!=",
    "!=": "==",
    "<=": ">",
    ">=": "<",
    "<": ">",
    ">": "<",
    "+": "-",
    "-": "+",
}

MANIFEST_LANGUAGES = {
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "package.json": "JavaScript/TypeScript",
    "go.mod": "Go",
    "Cargo.toml": "Rust",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "pom.xml": "Java",
    "build.gradle": "Java/Kotlin",
    "build.gradle.kts": "Java/Kotlin",
    "Package.swift": "Swift",
    "mix.exs": "Elixir",
    "pubspec.yaml": "Dart",
}

EXTENSION_LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".scala": "Scala",
    ".lua": "Lua",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".dart": "Dart",
    ".groovy": "Groovy",
    ".sol": "Solidity",
    ".zig": "Zig",
}


@dataclasses.dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    duration_seconds: float
    timed_out: bool = False


@dataclasses.dataclass
class CheckCommand:
    command: list[str]
    fail_on_output: bool = False


@dataclasses.dataclass
class FunctionMetric:
    path: str
    name: str
    start_line: int
    end_line: int
    complexity: int
    covered_lines: int
    total_lines: int
    coverage_percent: float
    craap_score: float
    parser: str
    coverage_limit: float = 100.0
    craap_limit: float = 6.0

    @property
    def passed(self) -> bool:
        return (
            self.coverage_percent >= self.coverage_limit
            and self.craap_score <= self.craap_limit
        )


@dataclasses.dataclass
class Mutation:
    mutant_id: str
    path: str
    line: int
    column: int
    original: str
    replacement: str
    survived: bool
    timed_out: bool
    duration_seconds: float
    output: str


@dataclasses.dataclass
class DependencyViolation:
    source: str
    source_module: str
    target: str
    target_module: str
    rule: str
    line: int = 0


@dataclasses.dataclass
class GateResult:
    key: str
    title: str
    passed: bool
    summary: str
    details: list[str] = dataclasses.field(default_factory=list)
    command_results: list[CommandResult] = dataclasses.field(default_factory=list)
    prompts: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    applicable: bool = True
    deferred: bool = False


@dataclasses.dataclass
class ToolContext:
    cache_dir: Path
    python: str
    python_path: Path
    lizard_available: bool = False
    cargo_llvm_cov: str | None = None
    setup_results: list[CommandResult] = dataclasses.field(default_factory=list)

    @property
    def python_env(self) -> dict[str, str]:
        existing = os.environ.get("PYTHONPATH", "")
        value = str(self.python_path)
        if existing:
            value += os.pathsep + existing
        return {"PYTHONPATH": value}


@dataclasses.dataclass
class AnalysisReport:
    root: str
    generated_at: str
    languages: list[str]
    gates: list[GateResult]
    functions: list[FunctionMetric]
    mutations: list[Mutation]
    dependency_violations: list[DependencyViolation]
    tool_setup: list[CommandResult]
    notes: list[str]
    rerun_command: str | None = None
    mode: str = "full"

    @property
    def passed(self) -> bool:
        return (
            self.mode == "full"
            and bool(self.gates)
            and all(gate.passed for gate in self.gates)
        )

    @property
    def ready_for_full(self) -> bool:
        executed = [gate for gate in self.gates if not gate.deferred]
        return (
            self.mode == "fast"
            and bool(executed)
            and all(gate.passed for gate in executed)
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def default_config() -> dict[str, Any]:
    return {
        "source": {
            "include": [],
            "exclude": list(DEFAULT_TEST_PATTERNS),
            "extensions": sorted(SOURCE_EXTENSIONS),
        },
        "test": {"command": None, "timeout_seconds": 600},
        "format_lint": {
            "enabled": "auto",
            "required": False,
            "commands": [],
            "timeout_seconds": 300,
        },
        "types": {
            "enabled": "auto",
            "required": False,
            "commands": [],
            "timeout_seconds": 600,
        },
        "contracts": {
            "enabled": "auto",
            "required": False,
            "commands": [],
            "patterns": [
                "**/openapi.json",
                "**/openapi.yaml",
                "**/openapi.yml",
                "**/*.schema.json",
                "**/schemas/*.json",
            ],
            "timeout_seconds": 300,
        },
        "metrics": {
            "command": None,
            "report": None,
            "coverage_commands": [],
            "coverage_report": None,
            "coverage_format": "auto",
            "craap_limit": 6,
            "coverage_limit": 100,
        },
        "mutation": {
            "enabled": True,
            "test_command": None,
            "timeout_seconds": 600,
            "max_mutants": 0,
            "operators": OPERATOR_MUTATIONS,
            "exclude": [],
        },
        "dead_code": {
            "enabled": "auto",
            "required": False,
            "commands": [],
            "timeout_seconds": 300,
        },
        "flaky_tests": {
            "enabled": True,
            "runs": 3,
            "timeout_seconds": 600,
        },
        "dependencies": {
            "command": None,
            "edges_report": None,
            "rules": ".quality-dependencies.json",
            "timeout_seconds": 300,
        },
        "tools": {"auto_install": True, "cache_dir": None},
    }


def normalize_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def load_config(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    config = default_config()
    if path and path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot read configuration {path}: {error}") from error
        if not isinstance(loaded, dict):
            raise ValueError(f"Configuration {path} must contain a JSON object")
        config = deep_merge(config, loaded)
        notes.append(f"Loaded configuration from {path}")
    else:
        notes.append("No configuration file found; using runtime auto-detection")
    return config, notes


def command_list(
    value: Any, substitutions: dict[str, str] | None = None
) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        command = shlex.split(value)
    elif isinstance(value, list) and all(
        isinstance(item, (str, int, float)) for item in value
    ):
        command = [str(item) for item in value]
    else:
        raise ValueError(
            "Commands must be a shell-style string or a JSON array of arguments"
        )
    substitutions = substitutions or {}
    return [substitute_text(part, substitutions) for part in command]


def command_lists(
    value: Any, substitutions: dict[str, str] | None = None
) -> list[list[str]]:
    if value is None or value == [] or value == "":
        return []
    if isinstance(value, str):
        command = command_list(value, substitutions)
        return [command] if command else []
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float)) for item in value
    ):
        command = command_list(value, substitutions)
        return [command] if command else []
    if isinstance(value, list):
        commands = []
        for raw_command in value:
            command = command_list(raw_command, substitutions)
            if command:
                commands.append(command)
        return commands
    raise ValueError("Commands must be a command or an array of commands")


def substitute_text(value: str, substitutions: dict[str, str]) -> str:
    for key, replacement in substitutions.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def run_command(
    command: Sequence[str],
    root: Path,
    timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.monotonic()
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=max(1, timeout_seconds),
            check=False,
        )
        return CommandResult(
            command=list(command),
            returncode=completed.returncode,
            stdout=completed.stdout[-12000:],
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return CommandResult(
            command=list(command),
            returncode=124,
            stdout=output[-12000:],
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as error:
        return CommandResult(
            command=list(command),
            returncode=127,
            stdout=str(error),
            duration_seconds=time.monotonic() - started,
        )


def executable(root: Path, name: str) -> str | None:
    local_candidates = [
        root / ".venv" / "bin" / name,
        root / "venv" / "bin" / name,
        root / "node_modules" / ".bin" / name,
        root / "vendor" / "bin" / name,
    ]
    for candidate in local_candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return name if shutil.which(name) else None


def detect_languages(root: Path) -> list[str]:
    languages = {
        language
        for manifest, language in MANIFEST_LANGUAGES.items()
        if (root / manifest).exists()
    }
    counts: dict[str, int] = {}
    for path in walk_files(root):
        language = EXTENSION_LANGUAGES.get(path.suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    languages.update(language for language, count in counts.items() if count > 0)
    return sorted(languages) or ["Unknown"]


def walk_files(root: Path) -> Iterable[Path]:
    if (root / ".git").exists() and shutil.which("git"):
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if listed.returncode == 0:
            for raw_path in listed.stdout.split(b"\0"):
                if not raw_path:
                    continue
                path = root / os.fsdecode(raw_path)
                if is_file_within(path, root) and not any(
                    part in DEFAULT_EXCLUDED_DIRS
                    for part in path.relative_to(root).parts
                ):
                    yield path
            return
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            directory for directory in dirs if directory not in DEFAULT_EXCLUDED_DIRS
        ]
        for filename in files:
            path = Path(current) / filename
            if is_file_within(path, root):
                yield path


def is_file_within(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def matches_any(relative: str, patterns: Sequence[str]) -> bool:
    path = Path(relative)
    lowered = relative.lower()
    return any(
        fnmatch.fnmatch(relative, pattern)
        or path.match(pattern)
        or (
            pattern.startswith("**/")
            and fnmatch.fnmatch(relative, pattern.removeprefix("**/"))
        )
        or fnmatch.fnmatch(lowered, pattern.lower())
        or Path(lowered).match(pattern.lower())
        or (
            pattern.lower().startswith("**/")
            and fnmatch.fnmatch(lowered, pattern.lower().removeprefix("**/"))
        )
        for pattern in patterns
    )


def discover_source_files(root: Path, source_config: dict[str, Any]) -> list[Path]:
    includes = [str(pattern) for pattern in source_config.get("include", [])]
    excludes = [str(pattern) for pattern in source_config.get("exclude", [])]
    extensions = {
        str(extension).lower()
        for extension in source_config.get("extensions", SOURCE_EXTENSIONS)
    }
    result: list[Path] = []
    for path in walk_files(root):
        relative = normalize_path(path, root)
        if path.name in {Path(__file__).name, DEFAULT_REPORT}:
            continue
        if includes:
            if not matches_any(relative, includes):
                continue
        elif path.suffix.lower() not in extensions:
            continue
        if matches_any(relative, excludes):
            continue
        result.append(path)
    return sorted(result)


def read_package_json(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def package_dependencies(root: Path) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for path in walk_files(root):
        if path.name != "package.json":
            continue
        try:
            package = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(package, dict):
            continue
        for key in ("dependencies", "devDependencies"):
            values = package.get(key, {})
            if isinstance(values, dict):
                dependencies.update(
                    {str(name): str(version) for name, version in values.items()}
                )
    return dependencies


def default_tool_cache() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured) if configured else Path.home() / ".cache"
    return base / "repo-quality-gate"


def node_package_version(root: Path, package_name: str) -> str | None:
    package_path = root / "node_modules" / package_name / "package.json"
    try:
        value = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = value.get("version") if isinstance(value, dict) else None
    return str(version) if version else None


def bootstrap_tools(
    root: Path,
    config: dict[str, Any],
    source_files: Sequence[Path],
) -> ToolContext:
    tools_config = config.get("tools", {})
    cache_value = tools_config.get("cache_dir")
    cache_dir = (
        resolve_config_path(str(cache_value), root)
        if cache_value
        else default_tool_cache()
    )
    python = executable(root, "python") or executable(root, "python3") or sys.executable
    python_key = hashlib.sha256(str(python).encode()).hexdigest()[:10]
    python_path = (
        cache_dir
        / f"python-{sys.version_info.major}.{sys.version_info.minor}-{python_key}"
    )
    python_path.mkdir(parents=True, exist_ok=True)
    context = ToolContext(cache_dir, python, python_path)
    auto_install = bool(tools_config.get("auto_install", True))

    metrics_config = config.get("metrics", {})
    has_metrics_adapter = bool(
        metrics_config.get("report") or metrics_config.get("command")
    )
    needs_lizard = not has_metrics_adapter and any(
        path.suffix.lower() in LIZARD_EXTENSIONS for path in source_files
    )
    test_command = command_list(
        config.get("test", {}).get("command")
    ) or infer_test_command(root)
    needs_coverage = bool(
        not has_metrics_adapter
        and any(path.suffix.lower() in {".py", ".pyi"} for path in source_files)
        and test_command
        and (
            Path(test_command[0]).name.startswith("python")
            or "pytest" in " ".join(test_command)
        )
    )
    python_env = context.python_env
    missing_packages: list[str] = []
    if needs_lizard and not _python_module_available(
        python, "lizard", root, python_env
    ):
        missing_packages.append("lizard")
    if needs_coverage and not _python_module_available(
        python, "coverage", root, python_env
    ):
        missing_packages.append("coverage")
    has_python = any(path.suffix.lower() in {".py", ".pyi"} for path in source_files)
    optional_python_tools = []
    if has_python and (
        any((root / name).exists() for name in ("ruff.toml", ".ruff.toml"))
        or project_config_contains(root, "[tool.ruff")
    ):
        optional_python_tools.append(("ruff", "ruff"))
    if has_python and (
        (root / "mypy.ini").exists()
        or project_config_contains(root, "[tool.mypy")
        or project_config_contains(root, "[mypy")
    ):
        optional_python_tools.append(("mypy", "mypy"))
    if has_python and project_config_contains(root, "[tool.vulture"):
        optional_python_tools.append(("vulture", "vulture"))
    contract_files = discover_contract_files(root, config.get("contracts", {}))
    if any(path.name.lower().endswith(".schema.json") for path in contract_files):
        optional_python_tools.append(("jsonschema", "jsonschema"))
    if any(
        path.name.lower() in {"openapi.json", "openapi.yaml", "openapi.yml"}
        for path in contract_files
    ):
        optional_python_tools.append(
            ("openapi_spec_validator", "openapi-spec-validator")
        )
    for module, package in optional_python_tools:
        if not _python_module_available(python, module, root, python_env):
            missing_packages.append(package)
    missing_packages = list(dict.fromkeys(missing_packages))
    if auto_install and missing_packages:
        install = run_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                "--target",
                str(python_path),
                *missing_packages,
            ],
            root,
            900,
        )
        context.setup_results.append(install)
    context.lizard_available = needs_lizard and _python_module_available(
        python, "lizard", root, python_env
    )

    dependencies = package_dependencies(root)
    vitest_version = node_package_version(root, "vitest")
    needs_vitest_coverage = (
        not has_metrics_adapter
        and "vitest" in dependencies
        and not node_package_version(root, "@vitest/coverage-v8")
    )
    if auto_install and needs_vitest_coverage and executable(root, "npm"):
        requested = vitest_version or dependencies.get("vitest", "latest")
        install = run_command(
            [
                executable(root, "npm") or "npm",
                "install",
                "--no-save",
                "--package-lock=false",
                "--ignore-scripts",
                f"@vitest/coverage-v8@{requested}",
            ],
            root,
            900,
        )
        context.setup_results.append(install)

    cargo_root = cache_dir / "cargo"
    cargo_binary = (
        cargo_root
        / "bin"
        / ("cargo-llvm-cov.exe" if os.name == "nt" else "cargo-llvm-cov")
    )
    existing_cargo_cov = executable(root, "cargo-llvm-cov")
    if existing_cargo_cov:
        context.cargo_llvm_cov = existing_cargo_cov
    elif cargo_binary.exists():
        context.cargo_llvm_cov = str(cargo_binary)
    elif (
        auto_install
        and not has_metrics_adapter
        and (root / "Cargo.toml").exists()
        and executable(root, "cargo")
    ):
        install = run_command(
            [
                executable(root, "cargo") or "cargo",
                "+stable",
                "install",
                "cargo-llvm-cov",
                "--locked",
                "--root",
                str(cargo_root),
            ],
            root,
            1800,
        )
        context.setup_results.append(install)
        if cargo_binary.exists():
            context.cargo_llvm_cov = str(cargo_binary)
    return context


def infer_test_command(root: Path) -> list[str] | None:
    package = read_package_json(root)
    scripts = (
        package.get("scripts", {}) if isinstance(package.get("scripts"), dict) else {}
    )
    if "test" in scripts and executable(root, "npm"):
        return [executable(root, "npm") or "npm", "test"]
    python = executable(root, "python") or executable(root, "python3")
    if python and any(
        (root / name).exists()
        for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tests")
    ):
        if executable(root, "pytest") or _python_module_available(
            python, "pytest", root
        ):
            return [python, "-m", "pytest", "-q"]
    if (root / "go.mod").exists() and executable(root, "go"):
        return [executable(root, "go") or "go", "test", "./..."]
    if (root / "Cargo.toml").exists() and executable(root, "cargo"):
        return [executable(root, "cargo") or "cargo", "test", "--all"]
    if (root / "Gemfile").exists() and executable(root, "bundle"):
        if (root / "spec").exists():
            return [executable(root, "bundle") or "bundle", "exec", "rspec"]
        return [executable(root, "bundle") or "bundle", "exec", "rake", "test"]
    if (root / "composer.json").exists() and (root / "vendor/bin/phpunit").exists():
        return [str(root / "vendor/bin/phpunit")]
    if (root / "pom.xml").exists() and executable(root, "mvn"):
        return [executable(root, "mvn") or "mvn", "test"]
    if (root / "gradlew").exists():
        return [str(root / "gradlew"), "test"]
    if list(root.glob("*.sln")) and executable(root, "dotnet"):
        return [executable(root, "dotnet") or "dotnet", "test"]
    return None


def package_scripts(root: Path) -> dict[str, str]:
    package = read_package_json(root)
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(command) for name, command in scripts.items()}


def package_script_command(root: Path, name: str) -> list[str] | None:
    if name not in package_scripts(root):
        return None
    if (root / "pnpm-lock.yaml").exists() and executable(root, "pnpm"):
        return [executable(root, "pnpm") or "pnpm", "run", name]
    if (root / "yarn.lock").exists() and executable(root, "yarn"):
        return [executable(root, "yarn") or "yarn", "run", name]
    if executable(root, "npm"):
        return [executable(root, "npm") or "npm", "run", name]
    return None


def first_package_script(root: Path, names: Sequence[str]) -> CheckCommand | None:
    for name in names:
        command = package_script_command(root, name)
        if command:
            return CheckCommand(command)
    return None


def configured_check_commands(
    section: dict[str, Any], root: Path
) -> list[CheckCommand]:
    substitutions = {"root": str(root)}
    value = section.get("commands")
    if not value and section.get("command"):
        value = section["command"]
    return [CheckCommand(command) for command in command_lists(value, substitutions)]


def unique_check_commands(commands: Sequence[CheckCommand]) -> list[CheckCommand]:
    seen: set[tuple[str, ...]] = set()
    result = []
    for command in commands:
        key = tuple(command.command)
        if key not in seen:
            seen.add(key)
            result.append(command)
    return result


def project_config_contains(root: Path, text: str) -> bool:
    for name in ("pyproject.toml", "setup.cfg", "tox.ini"):
        path = root / name
        try:
            if text in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def infer_format_lint_commands(
    root: Path, source_files: Sequence[Path], tools: ToolContext
) -> list[CheckCommand]:
    commands: list[CheckCommand] = []
    lint = first_package_script(root, ("lint:check", "check:lint", "lint"))
    formatting = first_package_script(
        root, ("format:check", "check:format", "format-check", "fmt:check")
    )
    if lint:
        commands.append(lint)
    if formatting:
        commands.append(formatting)

    eslint_configs = (
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        ".eslintrc.cjs",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
    )
    eslint = executable(root, "eslint")
    if not lint and eslint and any((root / name).exists() for name in eslint_configs):
        commands.append(CheckCommand([eslint, "."]))
    prettier = executable(root, "prettier")
    prettier_configs = (
        ".prettierrc",
        ".prettierrc.json",
        ".prettierrc.js",
        "prettier.config.js",
        "prettier.config.cjs",
    )
    if (
        not formatting
        and prettier
        and any((root / name).exists() for name in prettier_configs)
    ):
        commands.append(CheckCommand([prettier, "--check", "."]))

    has_python = any(path.suffix.lower() in {".py", ".pyi"} for path in source_files)
    has_ruff_config = any(
        (root / name).exists() for name in ("ruff.toml", ".ruff.toml")
    ) or project_config_contains(root, "[tool.ruff")
    if (
        has_python
        and has_ruff_config
        and _python_module_available(tools.python, "ruff", root, tools.python_env)
    ):
        commands.extend(
            [
                CheckCommand([tools.python, "-m", "ruff", "check", "."]),
                CheckCommand([tools.python, "-m", "ruff", "format", "--check", "."]),
            ]
        )
    go_files = [path for path in source_files if path.suffix.lower() == ".go"]
    gofmt = executable(root, "gofmt")
    if go_files and gofmt:
        commands.append(
            CheckCommand(
                [gofmt, "-l", *[normalize_path(path, root) for path in go_files]],
                fail_on_output=True,
            )
        )
    cargo = executable(root, "cargo")
    if (root / "Cargo.toml").exists() and cargo:
        commands.append(CheckCommand([cargo, "fmt", "--all", "--", "--check"]))
    return unique_check_commands(commands)


def infer_type_commands(
    root: Path, source_files: Sequence[Path], tools: ToolContext
) -> list[CheckCommand]:
    commands: list[CheckCommand] = []
    script = first_package_script(
        root, ("typecheck", "type-check", "check:types", "types:check")
    )
    if script:
        commands.append(script)
    elif list(root.glob("tsconfig*.json")) and executable(root, "tsc"):
        commands.append(CheckCommand([executable(root, "tsc") or "tsc", "--noEmit"]))
    has_python = any(path.suffix.lower() in {".py", ".pyi"} for path in source_files)
    has_mypy_config = (
        (root / "mypy.ini").exists()
        or project_config_contains(root, "[tool.mypy")
        or project_config_contains(root, "[mypy")
    )
    if (
        has_python
        and has_mypy_config
        and _python_module_available(tools.python, "mypy", root, tools.python_env)
    ):
        commands.append(CheckCommand([tools.python, "-m", "mypy", "."]))
    if (root / "go.mod").exists() and executable(root, "go"):
        commands.append(CheckCommand([executable(root, "go") or "go", "vet", "./..."]))
    if (root / "Cargo.toml").exists() and executable(root, "cargo"):
        commands.append(
            CheckCommand(
                [executable(root, "cargo") or "cargo", "check", "--all-targets"]
            )
        )
    if list(root.glob("*.sln")) and executable(root, "dotnet"):
        commands.append(
            CheckCommand(
                [executable(root, "dotnet") or "dotnet", "build", "--no-restore"]
            )
        )
    return unique_check_commands(commands)


def discover_contract_files(root: Path, section: dict[str, Any]) -> list[Path]:
    patterns = [str(pattern) for pattern in section.get("patterns", [])]
    return sorted(
        path
        for path in walk_files(root)
        if matches_any(normalize_path(path, root), patterns)
    )


def infer_contract_commands(root: Path) -> list[CheckCommand]:
    commands = []
    for name in package_scripts(root):
        lowered = name.lower()
        if "contract" in lowered or lowered in {
            "schema:check",
            "check:schema",
            "openapi:check",
            "check:openapi",
        }:
            command = package_script_command(root, name)
            if command:
                commands.append(CheckCommand(command))
    return unique_check_commands(commands)


def infer_dead_code_commands(
    root: Path, source_files: Sequence[Path], tools: ToolContext
) -> list[CheckCommand]:
    commands = []
    for name in package_scripts(root):
        lowered = name.lower()
        if any(
            token in lowered for token in ("dead-code", "deadcode", "unused")
        ) or lowered in {
            "knip",
            "ts-prune",
        }:
            command = package_script_command(root, name)
            if command:
                commands.append(CheckCommand(command))
    dependencies = package_dependencies(root)
    npx = executable(root, "npx")
    if npx and "knip" in dependencies and not commands:
        commands.append(CheckCommand([npx, "--no-install", "knip"]))
    elif npx and "ts-prune" in dependencies and not commands:
        commands.append(CheckCommand([npx, "--no-install", "ts-prune"]))
    has_python = any(path.suffix.lower() in {".py", ".pyi"} for path in source_files)
    if (
        has_python
        and project_config_contains(root, "[tool.vulture")
        and _python_module_available(tools.python, "vulture", root, tools.python_env)
    ):
        commands.append(CheckCommand([tools.python, "-m", "vulture", "."]))
    return unique_check_commands(commands)


def unavailable_check(
    key: str, title: str, section: dict[str, Any], guidance: str
) -> GateResult:
    if bool(section.get("required", False)):
        return GateResult(
            key,
            title,
            False,
            f"No configured or supported {title.lower()} command was available.",
            [guidance],
            prompts=[
                (
                    f"Configure {title.lower()}",
                    f"Configure a deterministic {title.lower()} command that exits non-zero on violations. {guidance} Do not disable the requested gate.",
                )
            ],
        )
    return GateResult(
        key,
        title,
        True,
        f"Not applicable: no configured or supported {title.lower()} command was detected.",
        [guidance],
        applicable=False,
    )


def deferred_check(key: str, title: str, reason: str) -> GateResult:
    return GateResult(
        key,
        title,
        False,
        f"Deferred in fast mode: {reason}",
        ["Run the full certification command without --fast before shipping."],
        deferred=True,
    )


def run_command_check_gate(
    root: Path,
    key: str,
    title: str,
    section: dict[str, Any],
    inferred: Sequence[CheckCommand],
    guidance: str,
    extra_env: dict[str, str] | None = None,
) -> GateResult:
    if section.get("enabled", "auto") is False:
        return GateResult(
            key,
            title,
            False,
            f"{title} is disabled; a requested gate cannot be skipped.",
            prompts=[
                (
                    f"Enable {title.lower()}",
                    f"Enable and configure the {title.lower()} gate, then repair every reported violation.",
                )
            ],
        )
    configured = configured_check_commands(section, root)
    commands = configured or list(inferred)
    if not commands:
        return unavailable_check(key, title, section, guidance)
    timeout = int(section.get("timeout_seconds", 300))
    results = []
    failures = []
    for check in commands:
        result = run_command(check.command, root, timeout, extra_env)
        results.append(result)
        if result.returncode != 0 or (check.fail_on_output and result.stdout.strip()):
            failures.append(result)
    if failures:
        details = [format_command(result) for result in failures]
        first = failures[0]
        return GateResult(
            key,
            title,
            False,
            f"{len(failures)} of {len(results)} {title.lower()} commands failed.",
            details,
            results,
            [
                (
                    f"Repair {title.lower()}",
                    generic_adapter_prompt(title.lower(), first),
                )
            ],
        )
    return GateResult(
        key,
        title,
        True,
        f"All {len(results)} {title.lower()} commands passed with zero violations.",
        command_results=results,
    )


JSON_SCHEMA_CHECK = """\
import json
import sys
from jsonschema.validators import validator_for

with open(sys.argv[1], encoding="utf-8") as handle:
    schema = json.load(handle)
validator_for(schema).check_schema(schema)
print(f"valid JSON Schema: {sys.argv[1]}")
"""


def contract_file_commands(
    files: Sequence[Path], tools: ToolContext
) -> list[CheckCommand]:
    commands = []
    for path in files:
        lowered = path.name.lower()
        if lowered in {"openapi.json", "openapi.yaml", "openapi.yml"}:
            commands.append(
                CheckCommand([tools.python, "-m", "openapi_spec_validator", str(path)])
            )
        else:
            commands.append(
                CheckCommand([tools.python, "-c", JSON_SCHEMA_CHECK, str(path)])
            )
    return commands


def run_contract_gate(
    root: Path, config: dict[str, Any], tools: ToolContext
) -> GateResult:
    section = config["contracts"]
    files = discover_contract_files(root, section)
    commands = configured_check_commands(section, root)
    commands.extend(infer_contract_commands(root))
    if files:
        commands.extend(contract_file_commands(files, tools))
    effective_section = dict(section)
    effective_section["command"] = None
    effective_section["commands"] = []
    result = run_command_check_gate(
        root,
        "contracts",
        "Contract/schema validation",
        effective_section,
        unique_check_commands(commands),
        "Add OpenAPI or *.schema.json documents, or configure contracts.commands for repository-specific compatibility checks.",
        tools.python_env,
    )
    if result.applicable and result.passed and files:
        result.summary = (
            f"All {len(files)} detected contract/schema files and "
            f"{len(result.command_results) - len(files)} configured checks passed."
        )
    return result


def run_test_baseline(
    root: Path, config: dict[str, Any]
) -> tuple[list[str] | None, CommandResult | None]:
    command = command_list(config["test"].get("command")) or infer_test_command(root)
    if not command:
        return None, None
    return command, run_command(
        command, root, int(config["test"].get("timeout_seconds", 600))
    )


def combine_test_and_metrics_gate(
    metrics_gate: GateResult,
    test_command: list[str] | None,
    baseline: CommandResult | None,
) -> GateResult:
    title = "Tests, coverage & CRAAP"
    if not test_command or baseline is None:
        return GateResult(
            "quality",
            title,
            False,
            "No complete test command could be configured or inferred.",
            prompts=[
                (
                    "Configure the complete test suite",
                    "Configure test.command as an argument array that runs every required test and exits non-zero on failure. Then rerun coverage and CRAAP analysis.",
                )
            ],
        )
    if baseline.returncode != 0:
        return GateResult(
            "quality",
            title,
            False,
            "The complete test suite failed before coverage and CRAAP could be certified.",
            [baseline.stdout],
            [baseline],
            [("Repair tests", generic_adapter_prompt("test", baseline))],
        )
    metrics_gate.key = "quality"
    metrics_gate.title = title
    metrics_gate.command_results.insert(0, baseline)
    if metrics_gate.passed:
        metrics_gate.summary = f"Tests pass. {metrics_gate.summary}"
    return metrics_gate


def run_flaky_test_gate(
    root: Path,
    config: dict[str, Any],
    test_command: list[str] | None,
    baseline: CommandResult | None,
) -> GateResult:
    section = config["flaky_tests"]
    if section.get("enabled", True) is False:
        return GateResult(
            "flaky",
            "Flaky-test detection",
            False,
            "Flaky-test detection is disabled; a requested gate cannot be skipped.",
        )
    if not test_command or baseline is None:
        return GateResult(
            "flaky",
            "Flaky-test detection",
            True,
            "Not applicable: no complete test command was available.",
            applicable=False,
        )
    if baseline.returncode != 0:
        return GateResult(
            "flaky",
            "Flaky-test detection",
            True,
            "Not evaluated because the baseline test suite failed consistently before repeat runs.",
            [baseline.stdout],
            [baseline],
            applicable=False,
        )
    runs = max(2, int(section.get("runs", 3)))
    timeout = int(
        section.get("timeout_seconds", config["test"].get("timeout_seconds", 600))
    )
    results = [baseline]
    for _ in range(runs - 1):
        results.append(run_command(test_command, root, timeout))
    exit_codes = {result.returncode for result in results}
    if exit_codes == {0}:
        return GateResult(
            "flaky",
            "Flaky-test detection",
            True,
            f"The complete test suite passed consistently across {runs} runs; zero flakes were observed.",
            command_results=results,
        )
    return GateResult(
        "flaky",
        "Flaky-test detection",
        False,
        f"The test suite was inconsistent across {runs} runs; exit codes were {sorted(exit_codes)}.",
        [format_command(result) for result in results if result.returncode != 0],
        results,
        [
            (
                "Eliminate flaky tests",
                "Reproduce the inconsistent complete-suite result. Remove dependencies on timing, order, shared mutable state, randomness, network state, and leaked resources. Do not add retries or quarantine the test merely to hide the flake. Rerun the full suite repeatedly until every configured run passes.",
            )
        ],
    )


def _python_module_available(
    python: str,
    module: str,
    root: Path,
    extra_env: dict[str, str] | None = None,
) -> bool:
    result = run_command([python, "-c", f"import {module}"], root, 10, extra_env)
    return result.returncode == 0


def infer_coverage_commands(
    root: Path,
    report_path: Path,
    tools: ToolContext | None = None,
) -> tuple[list[list[str]], str] | None:
    python = executable(root, "python") or executable(root, "python3")
    if (
        python
        and infer_test_command(root)
        and _python_module_available(
            python, "coverage", root, tools.python_env if tools else None
        )
    ):
        test = infer_test_command(root) or []
        if len(test) >= 3 and test[1:3] == ["-m", "pytest"]:
            return (
                [
                    [python, "-m", "coverage", "erase"],
                    [python, "-m", "coverage", "run", "--branch", "-m", "pytest", "-q"],
                    [python, "-m", "coverage", "json", "-o", str(report_path)],
                ],
                "coverage-json",
            )
    if (root / "go.mod").exists() and executable(root, "go"):
        return (
            [
                [
                    executable(root, "go") or "go",
                    "test",
                    "./...",
                    f"-coverprofile={report_path}",
                ]
            ],
            "go-cover",
        )
    dependencies = package_dependencies(root)
    if "jest" in dependencies and executable(root, "npx"):
        return (
            [
                [
                    executable(root, "npx") or "npx",
                    "jest",
                    "--coverage",
                    "--coverageReporters=lcov",
                    "--runInBand",
                ]
            ],
            "lcov",
        )
    if "vitest" in dependencies and executable(root, "npx"):
        return (
            [
                [
                    executable(root, "npx") or "npx",
                    "vitest",
                    "run",
                    "--coverage",
                    "--coverage.reporter=lcov",
                ]
            ],
            "lcov",
        )
    cargo_llvm_cov = (
        tools.cargo_llvm_cov if tools else executable(root, "cargo-llvm-cov")
    )
    if (root / "Cargo.toml").exists() and cargo_llvm_cov:
        return (
            [
                [
                    cargo_llvm_cov,
                    "--all",
                    "--lcov",
                    "--output-path",
                    str(report_path),
                ]
            ],
            "lcov",
        )
    return None


def discover_coverage_report(root: Path) -> tuple[Path, str] | None:
    candidates = [
        (root / "coverage.json", "coverage-json"),
        (root / "coverage" / "coverage-final.json", "istanbul-json"),
        (root / "coverage" / "lcov.info", "lcov"),
        (root / "lcov.info", "lcov"),
        (root / "coverage.xml", "cobertura"),
        (root / "coverage" / "cobertura-coverage.xml", "cobertura"),
        (root / "cover.out", "go-cover"),
        (root / "coverage.out", "go-cover"),
    ]
    for path, format_name in candidates:
        if path.exists():
            return path, format_name
    for path in root.glob("**/jacoco.xml"):
        if not any(part in DEFAULT_EXCLUDED_DIRS for part in path.parts):
            return path, "jacoco"
    return None


def load_normalized_metrics(
    path: Path,
    root: Path,
    coverage_limit: float = 100.0,
    craap_limit: float = 6.0,
) -> list[FunctionMetric]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("functions", []) if isinstance(value, dict) else []
    if not isinstance(rows, list):
        raise ValueError("Normalized metrics report must contain a 'functions' array")
    functions: list[FunctionMetric] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Function metric #{index + 1} must be an object")
        complexity = int(row["complexity"])
        covered = int(row.get("covered_lines", 0))
        total = int(row.get("total_lines", 0))
        coverage = float(
            row.get("coverage_percent", (100.0 * covered / total) if total else 0.0)
        )
        score = float(row.get("craap_score", craap_score(complexity, coverage)))
        functions.append(
            FunctionMetric(
                path=normalize_report_path(str(row["path"]), root),
                name=str(row["name"]),
                start_line=int(row.get("start_line", 1)),
                end_line=int(row.get("end_line", row.get("start_line", 1))),
                complexity=complexity,
                covered_lines=covered,
                total_lines=total,
                coverage_percent=coverage,
                craap_score=score,
                parser=str(row.get("parser", "adapter")),
                coverage_limit=coverage_limit,
                craap_limit=craap_limit,
            )
        )
    return functions


def normalize_report_path(value: str, root: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return normalize_path(path, root)
    return Path(value).as_posix().lstrip("./")


def parse_coverage(
    path: Path, format_name: str, root: Path
) -> dict[str, dict[int, int]]:
    if format_name == "auto":
        name = path.name.lower()
        if name.endswith(".info"):
            format_name = "lcov"
        elif name.endswith(".xml"):
            format_name = "cobertura"
        elif name.endswith(".out"):
            format_name = "go-cover"
        else:
            format_name = "coverage-json"
    if format_name == "coverage-json":
        return parse_coverage_json(path, root)
    if format_name == "istanbul-json":
        return parse_istanbul_json(path, root)
    if format_name == "lcov":
        return parse_lcov(path, root)
    if format_name in {"cobertura", "jacoco"}:
        return parse_xml_coverage(path, root)
    if format_name == "go-cover":
        return parse_go_cover(path, root)
    raise ValueError(f"Unknown coverage format: {format_name}")


def parse_coverage_json(path: Path, root: Path) -> dict[str, dict[int, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    files = value.get("files", {}) if isinstance(value, dict) else {}
    result: dict[str, dict[int, int]] = {}
    for filename, details in files.items():
        executed = details.get("executed_lines", [])
        missing = details.get("missing_lines", [])
        lines = {int(line): 1 for line in executed}
        lines.update({int(line): 0 for line in missing})
        result[normalize_report_path(str(filename), root)] = lines
    return result


def parse_istanbul_json(path: Path, root: Path) -> dict[str, dict[int, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[int, int]] = {}
    for filename, details in value.items():
        if not isinstance(details, dict):
            continue
        statement_map = details.get("statementMap", {})
        statement_hits = details.get("s", {})
        lines: dict[int, int] = {}
        for statement_id, location in statement_map.items():
            try:
                line = int(location["start"]["line"])
                hit = int(statement_hits.get(statement_id, 0))
            except (KeyError, TypeError, ValueError):
                continue
            lines[line] = max(lines.get(line, 0), hit)
        result[normalize_report_path(str(filename), root)] = lines
    return result


def parse_lcov(path: Path, root: Path) -> dict[str, dict[int, int]]:
    result: dict[str, dict[int, int]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw_line.startswith("SF:"):
            current = normalize_report_path(raw_line[3:], root)
            result.setdefault(current, {})
        elif raw_line.startswith("DA:") and current:
            parts = raw_line[3:].split(",")
            if len(parts) >= 2:
                result[current][int(parts[0])] = int(parts[1])
    return result


def parse_xml_coverage(path: Path, root: Path) -> dict[str, dict[int, int]]:
    tree = ET.parse(path)
    result: dict[str, dict[int, int]] = {}
    for class_node in tree.findall(".//class"):
        filename = class_node.attrib.get("filename")
        if not filename:
            continue
        relative = normalize_report_path(filename, root)
        lines = result.setdefault(relative, {})
        for line_node in class_node.findall(".//line"):
            if "number" in line_node.attrib:
                lines[int(line_node.attrib["number"])] = int(
                    float(line_node.attrib.get("hits", "0"))
                )
    if result:
        return result
    # JaCoCo stores package/sourcefile rather than a filename on class nodes.
    for package in tree.findall(".//package"):
        package_name = package.attrib.get("name", "")
        for source in package.findall("sourcefile"):
            filename = "/".join(
                part for part in (package_name, source.attrib.get("name", "")) if part
            )
            lines = result.setdefault(normalize_report_path(filename, root), {})
            for line_node in source.findall("line"):
                line = int(line_node.attrib["nr"])
                lines[line] = int(line_node.attrib.get("ci", "0"))
    return result


def parse_go_cover(path: Path, root: Path) -> dict[str, dict[int, int]]:
    result: dict[str, dict[int, int]] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw_line.startswith("mode:") or not raw_line.strip():
            continue
        match = re.match(r"(.+):(\d+)\.\d+,(\d+)\.\d+\s+\d+\s+(\d+)$", raw_line)
        if not match:
            continue
        filename, start, end, count = match.groups()
        relative = _match_report_suffix(filename, root)
        lines = result.setdefault(relative, {})
        for line in range(int(start), int(end) + 1):
            lines[line] = max(lines.get(line, 0), int(count))
    return result


def _match_report_suffix(filename: str, root: Path) -> str:
    normalized = filename.replace("\\", "/")
    candidates = [normalize_path(path, root) for path in walk_files(root)]
    matching = [candidate for candidate in candidates if normalized.endswith(candidate)]
    return min(matching, key=len) if matching else normalized


def craap_score(complexity: int, coverage_percent: float) -> float:
    """The standard CRAP formula, named CRAAP here to match the requested gate."""
    uncovered = max(0.0, min(1.0, 1.0 - coverage_percent / 100.0))
    return complexity**2 * uncovered**3 + complexity


def parse_functions(path: Path, root: Path) -> list[tuple[str, int, int, int, str]]:
    if path.suffix.lower() in {".py", ".pyi"}:
        return parse_python_functions(path)
    return parse_brace_functions(path)


class PythonFunctionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[tuple[str, int, int, int, str]] = []
        self.scope: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = ".".join([*self.scope, node.name])
        complexity = python_complexity(node)
        self.functions.append(
            (
                name,
                node.lineno,
                getattr(node, "end_lineno", node.lineno),
                complexity,
                "python-ast",
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


class PythonComplexityVisitor(ast.NodeVisitor):
    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.complexity = 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if node is self.root:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        return None

    def generic_visit(self, node: ast.AST) -> Any:
        if isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.IfExp,
                ast.comprehension,
            ),
        ):
            self.complexity += 1
        elif isinstance(node, ast.BoolOp):
            self.complexity += max(0, len(node.values) - 1)
        elif isinstance(node, ast.Match):
            self.complexity += len(node.cases)
        return super().generic_visit(node)


def python_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    visitor = PythonComplexityVisitor(node)
    visitor.visit(node)
    return visitor.complexity


def parse_python_functions(path: Path) -> list[tuple[str, int, int, int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    visitor = PythonFunctionVisitor()
    visitor.visit(tree)
    return visitor.functions


CONTROL_PREFIXES = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "with",
    "when",
    "match",
    "else",
    "do",
}
FUNCTION_HEADER = re.compile(
    r"(?P<name>[A-Za-z_$~][\w$.:<>~]*)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{\s*$",
    re.MULTILINE,
)


def parse_brace_functions(path: Path) -> list[tuple[str, int, int, int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    masked = mask_strings_and_comments(text)
    functions: list[tuple[str, int, int, int, str]] = []
    for match in FUNCTION_HEADER.finditer(masked):
        name = match.group("name").split("::")[-1].split(".")[-1]
        if name in CONTROL_PREFIXES:
            continue
        open_brace = masked.rfind("{", match.start(), match.end())
        close_brace = matching_brace(masked, open_brace)
        if close_brace < 0:
            continue
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, close_brace) + 1
        body = masked[open_brace + 1 : close_brace]
        branches = re.findall(
            r"\b(?:if|for|while|case|catch|when)\b|&&|\|\||(?<!\?)\?(?!\?)", body
        )
        functions.append(
            (name, start_line, end_line, 1 + len(branches), "generic-brace")
        )
    return remove_nested_duplicates(functions)


def remove_nested_duplicates(
    functions: list[tuple[str, int, int, int, str]],
) -> list[tuple[str, int, int, int, str]]:
    seen: set[tuple[int, int]] = set()
    result = []
    for function in functions:
        span = (function[1], function[2])
        if span not in seen:
            result.append(function)
            seen.add(span)
    return result


def mask_strings_and_comments(text: str) -> str:
    chars = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current in {"'", '"', "`"}:
                state, quote = "string", current
                chars[index] = " "
            elif current == "/" and following == "/":
                state = "line-comment"
                chars[index] = chars[index + 1] = " "
                index += 1
            elif current == "/" and following == "*":
                state = "block-comment"
                chars[index] = chars[index + 1] = " "
                index += 1
            elif current == "#":
                state = "line-comment"
                chars[index] = " "
        elif state == "string":
            if current == "\\":
                chars[index] = " "
                if index + 1 < len(chars) and chars[index + 1] != "\n":
                    chars[index + 1] = " "
                    index += 1
            elif current == quote:
                chars[index] = " "
                state = "code"
            elif current != "\n":
                chars[index] = " "
        elif state == "line-comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block-comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "code"
            elif current != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def mask_comments(text: str) -> str:
    """Blank comments while preserving strings and character positions."""
    chars = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current in {"'", '"', "`"}:
                state, quote = "string", current
            elif current == "/" and following == "/":
                state = "line-comment"
                chars[index] = chars[index + 1] = " "
                index += 1
            elif current == "/" and following == "*":
                state = "block-comment"
                chars[index] = chars[index + 1] = " "
                index += 1
            elif current == "#" and not re.match(r"#\s*include\b", text[index:]):
                state = "line-comment"
                chars[index] = " "
        elif state == "string":
            if current == "\\":
                index += 1
            elif current == quote:
                state = "code"
        elif state == "line-comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block-comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "code"
            elif current != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def matching_brace(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def build_function_metrics(
    source_files: Sequence[Path],
    root: Path,
    coverage: dict[str, dict[int, int]],
    coverage_limit: float = 100.0,
    craap_limit: float = 6.0,
    external_functions: dict[str, list[tuple[str, int, int, int, str]]] | None = None,
) -> list[FunctionMetric]:
    external_functions = external_functions or {}
    functions: list[FunctionMetric] = []
    for path in source_files:
        relative = normalize_path(path, root)
        line_hits = find_coverage_lines(relative, coverage)
        parsed = external_functions.get(relative)
        if parsed is None:
            parsed = parse_functions(path, root)
        for name, start, end, complexity, parser in parsed:
            relevant = {
                line: hits for line, hits in line_hits.items() if start <= line <= end
            }
            covered = sum(1 for hits in relevant.values() if hits > 0)
            total = len(relevant)
            percent = (100.0 * covered / total) if total else 0.0
            functions.append(
                FunctionMetric(
                    path=relative,
                    name=name,
                    start_line=start,
                    end_line=end,
                    complexity=complexity,
                    covered_lines=covered,
                    total_lines=total,
                    coverage_percent=percent,
                    craap_score=craap_score(complexity, percent),
                    parser=parser,
                    coverage_limit=coverage_limit,
                    craap_limit=craap_limit,
                )
            )
    return sorted(functions, key=lambda item: (item.path, item.start_line, item.name))


LIZARD_HELPER = r"""
import json
from pathlib import Path
import sys
import lizard

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = {"files": {}}
for filename in request["files"]:
    analysis = lizard.analyze_file(filename)
    result["files"][filename] = [
        {
            "name": function.name,
            "start_line": function.start_line,
            "end_line": function.end_line,
            "complexity": function.cyclomatic_complexity,
        }
        for function in analysis.function_list
    ]
Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
"""


def analyze_with_lizard(
    source_files: Sequence[Path],
    root: Path,
    workspace: Path,
    tools: ToolContext,
) -> tuple[dict[str, list[tuple[str, int, int, int, str]]], CommandResult | None]:
    candidates = [
        path for path in source_files if path.suffix.lower() in LIZARD_EXTENSIONS
    ]
    if not candidates or not tools.lizard_available:
        return {}, None
    request_path = workspace / "lizard-request.json"
    output_path = workspace / "lizard-result.json"
    request_path.write_text(
        json.dumps({"files": [str(path) for path in candidates]}), encoding="utf-8"
    )
    result = run_command(
        [
            tools.python,
            "-c",
            LIZARD_HELPER,
            str(request_path),
            str(output_path),
        ],
        root,
        600,
        tools.python_env,
    )
    if result.returncode != 0 or not output_path.exists():
        return {}, result
    try:
        value = json.loads(output_path.read_text(encoding="utf-8"))
        files = value.get("files", {})
        parsed: dict[str, list[tuple[str, int, int, int, str]]] = {}
        for filename, rows in files.items():
            relative = normalize_report_path(str(filename), root)
            parsed[relative] = [
                (
                    str(row["name"]),
                    int(row["start_line"]),
                    int(row["end_line"]),
                    int(row["complexity"]),
                    "lizard",
                )
                for row in rows
            ]
        return parsed, result
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}, result


def find_coverage_lines(
    relative: str, coverage: dict[str, dict[int, int]]
) -> dict[int, int]:
    if relative in coverage:
        return coverage[relative]
    matches = [
        lines
        for name, lines in coverage.items()
        if name.endswith(relative) or relative.endswith(name)
    ]
    return matches[0] if len(matches) == 1 else {}


def cleaner_prompt(function: FunctionMetric) -> str:
    return f"""You are the cleaner agent. Fix the CRAAP gate failure in {function.path}:{function.start_line} for `{function.name}`.

Current evidence:
- line coverage: {function.coverage_percent:.2f}% ({function.covered_lines}/{function.total_lines})
- cyclomatic complexity: {function.complexity}
- CRAAP score: {function.craap_score:.2f}
- required: {function.coverage_limit:g}% coverage and CRAAP <= {function.craap_limit:g}

Read the function, its callers, and neighboring tests. Add behavior-focused tests for every uncovered path, then simplify control flow without changing behavior until complexity and CRAAP satisfy the threshold. Preserve error paths and public contracts. Run the repository's real coverage command and report the exact before/after metrics; do not exclude lines, weaken assertions, or mock the function under test."""


def run_metrics_gate(
    root: Path,
    config: dict[str, Any],
    source_files: Sequence[Path],
    workspace: Path,
    tools: ToolContext,
) -> tuple[GateResult, list[FunctionMetric]]:
    metrics = config["metrics"]
    coverage_limit = float(metrics.get("coverage_limit", 100))
    craap_limit = float(metrics.get("craap_limit", metrics.get("complexity_limit", 6)))
    command_results: list[CommandResult] = []
    substitutions = {"root": str(root), "report": str(workspace / "metrics.json")}
    adapter_command = command_list(metrics.get("command"), substitutions)
    report_value = metrics.get("report")
    if adapter_command:
        command_results.append(
            run_command(
                adapter_command,
                root,
                int(metrics.get("timeout_seconds", 600)),
                tools.python_env,
            )
        )
        if command_results[-1].returncode != 0:
            return GateResult(
                "craap",
                "CRAAP: coverage + complexity",
                False,
                "The metrics adapter failed.",
                [command_results[-1].stdout],
                command_results,
                [
                    (
                        "Repair the metrics adapter",
                        generic_adapter_prompt("metrics", command_results[-1]),
                    )
                ],
            ), []
    if report_value:
        metrics_path = resolve_config_path(
            substitute_text(str(report_value), substitutions), root
        )
        if metrics_path.exists():
            try:
                functions = load_normalized_metrics(
                    metrics_path, root, coverage_limit, craap_limit
                )
                return finish_metrics_gate(functions, command_results), functions
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ) as error:
                return GateResult(
                    "craap",
                    "CRAAP: coverage + complexity",
                    False,
                    f"The normalized metrics report is invalid: {error}",
                    [],
                    command_results,
                    [("Fix metrics report", normalized_metrics_prompt(str(error)))],
                ), []
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            f"Metrics report not found: {metrics_path}",
            [],
            command_results,
            [
                (
                    "Configure metrics",
                    normalized_metrics_prompt("report file was not produced"),
                )
            ],
        ), []

    coverage_report: tuple[Path, str] | None = None
    configured_commands = metrics.get("coverage_commands", [])
    configured_report = metrics.get("coverage_report")
    if configured_commands:
        for raw_command in configured_commands:
            command = command_list(
                raw_command,
                {"root": str(root), "report": str(workspace / "coverage.data")},
            )
            if not command:
                continue
            result = run_command(
                command,
                root,
                int(metrics.get("timeout_seconds", 600)),
                tools.python_env,
            )
            command_results.append(result)
            if result.returncode != 0:
                return GateResult(
                    "craap",
                    "CRAAP: coverage + complexity",
                    False,
                    "A configured coverage command failed.",
                    [result.stdout],
                    command_results,
                    [("Repair coverage", generic_adapter_prompt("coverage", result))],
                ), []
        if configured_report:
            coverage_report = (
                resolve_config_path(
                    substitute_text(
                        str(configured_report),
                        {
                            "root": str(root),
                            "report": str(workspace / "coverage.data"),
                        },
                    ),
                    root,
                ),
                str(metrics.get("coverage_format", "auto")),
            )
    elif configured_report:
        coverage_report = (
            resolve_config_path(str(configured_report), root),
            str(metrics.get("coverage_format", "auto")),
        )
    else:
        inferred_path = workspace / "coverage.data"
        inferred = infer_coverage_commands(root, inferred_path, tools)
        if inferred:
            commands, format_name = inferred
            for command in commands:
                result = run_command(
                    command,
                    root,
                    int(metrics.get("timeout_seconds", 600)),
                    tools.python_env,
                )
                command_results.append(result)
                if result.returncode != 0:
                    return GateResult(
                        "craap",
                        "CRAAP: coverage + complexity",
                        False,
                        "The auto-detected coverage command failed.",
                        [result.stdout],
                        command_results,
                        [
                            (
                                "Repair coverage",
                                generic_adapter_prompt("coverage", result),
                            )
                        ],
                    ), []
            if format_name == "lcov" and not inferred_path.exists():
                discovered = discover_coverage_report(root)
                coverage_report = discovered
            else:
                coverage_report = (inferred_path, format_name)
        else:
            coverage_report = discover_coverage_report(root)

    if not coverage_report or not coverage_report[0].exists():
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            "No coverage adapter or supported coverage report was available.",
            [
                "Configure metrics.command + metrics.report for any language, or coverage_commands + coverage_report for a supported coverage format."
            ],
            command_results,
            [
                (
                    "Configure language metrics",
                    normalized_metrics_prompt("coverage could not be measured"),
                )
            ],
        ), []
    try:
        coverage = parse_coverage(coverage_report[0], coverage_report[1], root)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        ET.ParseError,
    ) as error:
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            f"Coverage report could not be parsed: {error}",
            [],
            command_results,
            [("Fix coverage report", normalized_metrics_prompt(str(error)))],
        ), []
    lizard_functions, lizard_result = analyze_with_lizard(
        source_files, root, workspace, tools
    )
    if lizard_result:
        command_results.append(lizard_result)
    functions = build_function_metrics(
        source_files,
        root,
        coverage,
        coverage_limit,
        craap_limit,
        lizard_functions,
    )
    heuristic_files = sorted(
        normalize_path(path, root)
        for path in source_files
        if path.suffix.lower() not in {".py", ".pyi"}
        and normalize_path(path, root) not in lizard_functions
    )
    return (
        finish_metrics_gate(functions, command_results, heuristic_files),
        functions,
    )


def finish_metrics_gate(
    functions: list[FunctionMetric],
    commands: list[CommandResult],
    heuristic_files: Sequence[str] = (),
) -> GateResult:
    if not functions:
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            "No function-level metrics were produced.",
            [
                "Use a normalized metrics adapter when the built-in syntax scanner does not understand the repository language."
            ],
            commands,
            [
                (
                    "Add a metrics adapter",
                    normalized_metrics_prompt("no functions were discovered"),
                )
            ],
        )
    coverage_limit = functions[0].coverage_limit
    craap_limit = functions[0].craap_limit
    failures = sorted(
        (function for function in functions if not function.passed),
        key=lambda function: (
            -function.craap_score,
            function.coverage_percent,
            function.path,
            function.start_line,
        ),
    )
    details = [
        f"{function.path}:{function.start_line} {function.name}: coverage {function.coverage_percent:.2f}%, complexity {function.complexity}, CRAAP {function.craap_score:.2f}"
        for function in failures[:100]
    ]
    prompts = [
        (
            f"Fix {function.path}:{function.start_line} {function.name}",
            cleaner_prompt(function),
        )
        for function in failures
    ]
    if heuristic_files:
        details.insert(
            0,
            f"A semantic metrics adapter is required for {len(heuristic_files)} non-Python source files; the built-in brace scan is diagnostic only.",
        )
        prompts.insert(
            0,
            (
                "Add a semantic language adapter",
                normalized_metrics_prompt(
                    f"{len(heuristic_files)} files use syntax that the portable fallback cannot certify"
                ),
            ),
        )
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            f"Semantic adapter required for {len(heuristic_files)} files; {len(failures)} measured functions also fail thresholds.",
            details,
            commands,
            prompts,
        )
    if failures:
        return GateResult(
            "craap",
            "CRAAP: coverage + complexity",
            False,
            f"{len(failures)} of {len(functions)} functions fail {coverage_limit:g}% coverage and CRAAP <= {craap_limit:g}.",
            details,
            commands,
            prompts,
        )
    return GateResult(
        "craap",
        "CRAAP: coverage + complexity",
        True,
        f"All {len(functions)} functions have {coverage_limit:g}% coverage and CRAAP <= {craap_limit:g}.",
        [],
        commands,
    )


def resolve_config_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def generic_adapter_prompt(kind: str, result: CommandResult) -> str:
    return f"""Repair the repository's {kind} quality-gate adapter.

Command: {shlex.join(result.command)}
Exit code: {result.returncode}
Timed out: {result.timed_out}
Output:
{result.stdout[-4000:]}

Run the command directly, diagnose the first real failure, and make the smallest correct change. Do not disable the gate or replace a failing command with a no-op. Re-run the full quality gate and preserve the command's deterministic non-zero-on-failure contract."""


def normalized_metrics_prompt(reason: str) -> str:
    return f"""Create or repair a language adapter for the repository quality gate. Reason: {reason}.

Configure `metrics.command` to run the language's real coverage and cyclomatic-complexity tools and `metrics.report` to point to normalized JSON with this shape:
{{"functions":[{{"path":"src/file.ext","name":"functionName","start_line":1,"end_line":10,"complexity":2,"covered_lines":5,"total_lines":5,"coverage_percent":100}}]}}

Include every production function. Use executable-line coverage, not file averages. The gate requires exactly 100% coverage and computes CRAAP as complexity^2 * (1 - coverage/100)^3 + complexity, with a maximum of 6. Do not invent measurements or omit failing functions."""


def operator_offsets(
    path: Path, operators: dict[str, str]
) -> list[tuple[int, int, int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if path.suffix.lower() in {".py", ".pyi"}:
        return python_operator_offsets(text, operators)
    masked = mask_strings_and_comments(text)
    pattern = re.compile(
        "|".join(
            re.escape(operator) for operator in sorted(operators, key=len, reverse=True)
        )
    )
    offsets: list[tuple[int, int, int, str, str]] = []
    for match in pattern.finditer(masked):
        original = match.group(0)
        if original in {"+", "-"} and not is_binary_sign(
            masked, match.start(), match.end()
        ):
            continue
        if original in {"<", ">"} and _part_of_compound_operator(
            masked, match.start(), match.end()
        ):
            continue
        line = text.count("\n", 0, match.start()) + 1
        line_start = text.rfind("\n", 0, match.start()) + 1
        offsets.append(
            (
                match.start(),
                line,
                match.start() - line_start + 1,
                original,
                str(operators[original]),
            )
        )
    return offsets


def python_operator_offsets(
    text: str, operators: dict[str, str]
) -> list[tuple[int, int, int, str, str]]:
    line_offsets = [0]
    for match in re.finditer("\n", text):
        line_offsets.append(match.end())
    offsets: list[tuple[int, int, int, str, str]] = []
    try:
        tokens = tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__)
        for token in tokens:
            if token.type != tokenize.OP or token.string not in operators:
                continue
            line, column_zero = token.start
            absolute = line_offsets[line - 1] + column_zero
            if token.string in {"+", "-"} and not is_binary_sign(
                text, absolute, absolute + 1
            ):
                continue
            offsets.append(
                (
                    absolute,
                    line,
                    column_zero + 1,
                    token.string,
                    str(operators[token.string]),
                )
            )
    except (tokenize.TokenError, IndentationError):
        return []
    return offsets


def is_binary_sign(text: str, start: int, end: int) -> bool:
    left = start - 1
    while left >= 0 and text[left].isspace():
        left -= 1
    right = end
    while right < len(text) and text[right].isspace():
        right += 1
    if left < 0 or right >= len(text):
        return False
    left_char, right_char = text[left], text[right]
    if left_char in "([{,:;=<>!&|?+-*/%^~" or right_char in ")]},:;=<>!&|?+-*/%^~":
        return False
    return True


def _part_of_compound_operator(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return before in "-=<>" or after in "=-<>"


def mutation_prompt(mutation: Mutation) -> str:
    return f"""You are the hardener agent. Kill surviving mutant `{mutation.mutant_id}` in {mutation.path}:{mutation.line}:{mutation.column}.

Mutation: `{mutation.original}` -> `{mutation.replacement}`
The complete configured test suite still exited successfully.

First decide which externally visible behavior differs under this mutation. Add the smallest behavior-focused test through the production API that fails with the mutant and passes on the original code. If the mutant is genuinely equivalent, simplify the production expression so the equivalent mutation site disappears; do not mark it ignored. Re-run the single mutant, then the full mutation gate. Do not assert implementation details or weaken the zero-survivor threshold."""


def run_mutation_gate(
    root: Path,
    config: dict[str, Any],
    source_files: Sequence[Path],
    cli_max_mutants: int | None,
    test_baseline: CommandResult | None = None,
) -> tuple[GateResult, list[Mutation]]:
    mutation_config = config["mutation"]
    if not mutation_config.get("enabled", True):
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            "Mutation testing is disabled; a required gate cannot be skipped.",
            prompts=[
                (
                    "Enable mutation testing",
                    "Enable the mutation gate and run every generated mutant. The required threshold is zero surviving mutants.",
                )
            ],
        ), []
    test_command = (
        command_list(mutation_config.get("test_command"))
        or command_list(config["test"].get("command"))
        or infer_test_command(root)
    )
    if not test_command:
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            "No full-suite test command could be configured or inferred.",
            prompts=[
                (
                    "Configure tests",
                    "Configure `test.command` or `mutation.test_command` as an argument array that runs the repository's complete test suite and exits non-zero on any failure.",
                )
            ],
        ), []
    timeout = int(
        mutation_config.get(
            "timeout_seconds", config["test"].get("timeout_seconds", 600)
        )
    )
    baseline = (
        test_baseline
        if test_baseline and test_baseline.command == test_command
        else run_command(test_command, root, timeout)
    )
    if baseline.returncode != 0:
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            "The unmodified baseline test suite failed, so mutation results would be invalid.",
            [baseline.stdout],
            [baseline],
            [("Repair baseline tests", generic_adapter_prompt("test", baseline))],
        ), []
    operators = {
        str(key): str(value)
        for key, value in mutation_config.get("operators", OPERATOR_MUTATIONS).items()
    }
    excludes = [str(pattern) for pattern in mutation_config.get("exclude", [])]
    candidates: list[tuple[Path, int, int, int, str, str]] = []
    for path in source_files:
        relative = normalize_path(path, root)
        if matches_any(relative, excludes):
            continue
        for offset, line, column, original, replacement in operator_offsets(
            path, operators
        ):
            candidates.append((path, offset, line, column, original, replacement))
    configured_max = int(mutation_config.get("max_mutants", 0))
    max_mutants = cli_max_mutants if cli_max_mutants is not None else configured_max
    total_candidates = len(candidates)
    if max_mutants and len(candidates) > max_mutants:
        candidates = candidates[:max_mutants]
    if not candidates:
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            "No mutation candidates were generated; zero survivors cannot be claimed.",
            prompts=[
                (
                    "Repair mutation discovery",
                    "Inspect production source selection and mutation operators. Ensure all production files are included and configure language-specific operators if the repository uses syntax outside the built-in operator set.",
                )
            ],
        ), []
    results: list[Mutation] = []
    for index, (path, offset, line, column, original, replacement) in enumerate(
        candidates, start=1
    ):
        relative = normalize_path(path, root)
        if sys.stderr.isatty():
            print(
                f"[mutation {index}/{len(candidates)}] {relative}:{line}:{column} {original}->{replacement}",
                file=sys.stderr,
                flush=True,
            )
        mutant_id = hashlib.sha256(
            f"{relative}:{offset}:{original}:{replacement}".encode()
        ).hexdigest()[:12]
        try:
            original_bytes = path.read_bytes()
            text = original_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            results.append(
                Mutation(
                    mutant_id,
                    relative,
                    line,
                    column,
                    original,
                    replacement,
                    True,
                    False,
                    0.0,
                    str(error),
                )
            )
            continue
        if text[offset : offset + len(original)] != original:
            results.append(
                Mutation(
                    mutant_id,
                    relative,
                    line,
                    column,
                    original,
                    replacement,
                    True,
                    False,
                    0.0,
                    "Source changed during mutation run",
                )
            )
            continue
        mutated = text[:offset] + replacement + text[offset + len(original) :]
        original_stat = path.stat()
        result: CommandResult | None = None
        mutation_error = ""
        try:
            path.write_text(mutated, encoding="utf-8")
            os.chmod(path, original_stat.st_mode)
            with tempfile.TemporaryDirectory(prefix="quality-gate-pycache-") as pycache:
                result = run_command(
                    test_command,
                    root,
                    timeout,
                    {"PYTHONPYCACHEPREFIX": pycache},
                )
        except OSError as error:
            mutation_error = str(error)
        finally:
            path.write_bytes(original_bytes)
            os.chmod(path, original_stat.st_mode)
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        if result is None:
            results.append(
                Mutation(
                    mutant_id,
                    relative,
                    line,
                    column,
                    original,
                    replacement,
                    True,
                    False,
                    0.0,
                    mutation_error,
                )
            )
            continue
        survived = result.returncode == 0
        results.append(
            Mutation(
                mutant_id,
                relative,
                line,
                column,
                original,
                replacement,
                survived,
                result.timed_out,
                result.duration_seconds,
                result.stdout,
            )
        )
    survivors = [mutation for mutation in results if mutation.survived]
    prompts = [
        (f"Kill mutant {mutation.mutant_id}", mutation_prompt(mutation))
        for mutation in survivors
    ]
    if max_mutants and max_mutants < total_candidates:
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            f"Diagnostic limit tested {len(results)} mutants; a limited run cannot pass the full gate. {len(survivors)} survived.",
            [
                f"{mutation.path}:{mutation.line}:{mutation.column} {mutation.original}->{mutation.replacement}"
                for mutation in survivors
            ],
            [baseline],
            prompts,
        ), results
    if survivors:
        return GateResult(
            "mutation",
            "Mutation testing",
            False,
            f"{len(survivors)} of {len(results)} mutants survived; required: zero.",
            [
                f"{mutation.path}:{mutation.line}:{mutation.column} {mutation.original}->{mutation.replacement}"
                for mutation in survivors
            ],
            [baseline],
            prompts,
        ), results
    return GateResult(
        "mutation",
        "Mutation testing",
        True,
        f"All {len(results)} mutants were killed by the full test suite.",
        [],
        [baseline],
    ), results


IMPORT_PATTERNS = [
    re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE),
    re.compile(r"(?:import|export)\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
    re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"^\s*use\s+(?:crate::)?([\w:]+)", re.MULTILINE),
    re.compile(r"^\s*(?:import|using)\s+([\w.]+)", re.MULTILINE),
    re.compile(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE),
    re.compile(r"^\s*require_relative\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    re.compile(r"^\s*require\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
]


def module_for_path(relative: str, modules: Sequence[dict[str, Any]]) -> str | None:
    matches = modules_for_path(relative, modules)
    return matches[0] if len(matches) == 1 else None


def modules_for_path(relative: str, modules: Sequence[dict[str, Any]]) -> list[str]:
    return [
        str(module["name"])
        for module in modules
        if matches_any(relative, [str(pattern) for pattern in module.get("paths", [])])
    ]


def import_specs(path: Path) -> list[tuple[str, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    result: list[tuple[str, int]] = []
    masked = mask_comments(text)
    for pattern in IMPORT_PATTERNS:
        for match in pattern.finditer(masked):
            specifier = next((group for group in match.groups() if group), "")
            if specifier:
                result.append((specifier, text.count("\n", 0, match.start()) + 1))
    return result


def resolve_import(
    source: Path, specifier: str, root: Path, source_files: Sequence[Path]
) -> str | None:
    relative_files = {normalize_path(path, root): path for path in source_files}
    clean = (
        specifier.replace("::", "/").replace(".", "/")
        if not specifier.startswith(".")
        else specifier
    )
    candidate_strings: list[str] = []
    if specifier.startswith("."):
        if specifier.startswith(("./", "../")):
            base = (source.parent / specifier).resolve()
        else:
            leading_dots = len(specifier) - len(specifier.lstrip("."))
            base = source.parent
            for _ in range(max(0, leading_dots - 1)):
                base = base.parent
            remainder = specifier[leading_dots:].replace(".", "/")
            base = (base / remainder).resolve()
        for extension in SOURCE_EXTENSIONS:
            candidate_strings.extend(
                [
                    normalize_path(Path(str(base) + extension), root),
                    normalize_path(base / ("index" + extension), root),
                ]
            )
    else:
        clean = clean.removeprefix("crate/").lstrip("/")
        for extension in SOURCE_EXTENSIONS:
            candidate_strings.extend(
                [
                    clean + extension,
                    clean + "/index" + extension,
                    clean + "/__init__" + extension,
                ]
            )
        candidate_strings.append(clean)
    for candidate in candidate_strings:
        if candidate in relative_files:
            return candidate
    suffixes = [
        candidate
        for candidate in relative_files
        if candidate.endswith("/" + clean)
        or candidate.endswith("/" + clean + Path(candidate).suffix)
    ]
    return min(suffixes, key=len) if len(suffixes) == 1 else None


def load_dependency_edges(path: Path, root: Path) -> list[tuple[str, str, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    edges = value.get("edges", []) if isinstance(value, dict) else []
    result = []
    for edge in edges:
        result.append(
            (
                normalize_report_path(str(edge["from"]), root),
                normalize_report_path(str(edge["to"]), root),
                int(edge.get("line", 0)),
            )
        )
    return result


def dependency_prompt(violation: DependencyViolation) -> str:
    return f"""Repair this module-dependency violation:

- source: {violation.source}:{violation.line} (module `{violation.source_module}`)
- target: {violation.target} (module `{violation.target_module}`)
- violated rule: {violation.rule}

Read the dependency specification and neighboring architecture before editing. Restore the permitted direction with the smallest coherent change: invert the dependency behind an owned interface, move the responsibility to the correct module, or split the mixed module. Preserve behavior and add or update tests around the boundary. Do not broaden the allow-list merely to make the checker green. Re-run dependency analysis and the complete test suite."""


def run_dependency_gate(
    root: Path,
    config: dict[str, Any],
    source_files: Sequence[Path],
    workspace: Path | None = None,
) -> tuple[GateResult, list[DependencyViolation]]:
    dependency = config["dependencies"]
    rules_path = resolve_config_path(
        str(dependency.get("rules", ".quality-dependencies.json")), root
    )
    command_results: list[CommandResult] = []
    adapter_report = (workspace or root) / "dependency-edges.json"
    substitutions = {"root": str(root), "report": str(adapter_report)}
    command = command_list(
        dependency.get("command"),
        substitutions,
    )
    if command:
        result = run_command(command, root, int(dependency.get("timeout_seconds", 300)))
        command_results.append(result)
        if result.returncode != 0:
            return GateResult(
                "dependencies",
                "Module dependencies",
                False,
                "The dependency adapter failed.",
                [result.stdout],
                command_results,
                [
                    (
                        "Repair dependency adapter",
                        generic_adapter_prompt("dependency", result),
                    )
                ],
            ), []
    if not rules_path.exists():
        return GateResult(
            "dependencies",
            "Module dependencies",
            False,
            f"Dependency specification not found: {rules_path}",
            prompts=[("Define architecture rules", dependency_spec_prompt())],
        ), []
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        if not isinstance(rules, dict):
            raise ValueError("the root must be a JSON object")
        modules = rules.get("modules", [])
        allowed = rules.get("allow", {})
        denied = rules.get("deny", [])
        if not isinstance(modules, list) or not modules:
            raise ValueError("'modules' must be a non-empty array")
        if not isinstance(allowed, dict):
            raise ValueError("'allow' must be an object keyed by module name")
        names: list[str] = []
        for module in modules:
            if (
                not isinstance(module, dict)
                or not isinstance(module.get("name"), str)
                or not isinstance(module.get("paths"), list)
                or not module["paths"]
            ):
                raise ValueError(
                    "every module needs a string name and non-empty paths array"
                )
            names.append(module["name"])
        if len(names) != len(set(names)):
            raise ValueError("module names must be unique")
        missing_allow = sorted(set(names) - set(allowed))
        if missing_allow:
            raise ValueError(
                f"allow rules missing for modules: {', '.join(missing_allow)}"
            )
        unknown_allowed = sorted(
            {
                str(target)
                for targets in allowed.values()
                if isinstance(targets, list)
                for target in targets
                if str(target) not in names
            }
        )
        if unknown_allowed:
            raise ValueError(
                f"allow rules name unknown modules: {', '.join(unknown_allowed)}"
            )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
        return GateResult(
            "dependencies",
            "Module dependencies",
            False,
            f"Dependency specification is invalid: {error}",
            prompts=[("Fix architecture rules", dependency_spec_prompt(str(error)))],
        ), []
    edges_path_value = dependency.get("edges_report")
    try:
        if edges_path_value:
            edges = load_dependency_edges(
                resolve_config_path(
                    substitute_text(str(edges_path_value), substitutions), root
                ),
                root,
            )
        else:
            edges = []
            for source in source_files:
                for specifier, line in import_specs(source):
                    target = resolve_import(source, specifier, root, source_files)
                    if target:
                        edges.append((normalize_path(source, root), target, line))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return GateResult(
            "dependencies",
            "Module dependencies",
            False,
            f"Dependency edges report is invalid: {error}",
            prompts=[("Fix dependency edges", dependency_spec_prompt(str(error)))],
        ), []
    violations: list[DependencyViolation] = []
    for source in source_files:
        relative = normalize_path(source, root)
        owners = modules_for_path(relative, modules)
        if not owners:
            violations.append(
                DependencyViolation(
                    relative,
                    "(unassigned)",
                    "(module specification)",
                    "(none)",
                    "every production file must belong to exactly one module",
                )
            )
        elif len(owners) > 1:
            violations.append(
                DependencyViolation(
                    relative,
                    ", ".join(owners),
                    "(module specification)",
                    "(ambiguous)",
                    "module path patterns overlap",
                )
            )
    deny_pairs = {
        (str(item.get("from")), str(item.get("to")))
        for item in denied
        if isinstance(item, dict)
    }
    for source, target, line in edges:
        source_module = module_for_path(source, modules)
        target_module = module_for_path(target, modules)
        if not source_module or not target_module or source_module == target_module:
            continue
        allowed_targets = allowed.get(source_module)
        violation_rule = ""
        if (source_module, target_module) in deny_pairs:
            violation_rule = f"deny {source_module} -> {target_module}"
        elif isinstance(allowed_targets, list) and target_module not in [
            str(value) for value in allowed_targets
        ]:
            violation_rule = f"allow[{source_module}] excludes {target_module}"
        if violation_rule:
            violations.append(
                DependencyViolation(
                    source, source_module, target, target_module, violation_rule, line
                )
            )
    prompts = [
        (f"Fix {item.source_module} -> {item.target_module}", dependency_prompt(item))
        for item in violations
    ]
    if violations:
        return GateResult(
            "dependencies",
            "Module dependencies",
            False,
            f"{len(violations)} dependency-rule violations found; required: zero.",
            [
                f"{item.source}:{item.line} {item.source_module} -> {item.target_module}"
                for item in violations
            ],
            command_results,
            prompts,
        ), violations
    return GateResult(
        "dependencies",
        "Module dependencies",
        True,
        f"Zero dependency-rule violations across {len(edges)} resolved edges and {len(modules)} declared modules.",
        command_results=command_results,
    ), []


def dependency_spec_prompt(reason: str = "no specification exists") -> str:
    return f"""Define the repository's enforceable module dependency contract ({reason}). Create `.quality-dependencies.json`:

{{
  "modules": [
    {{"name": "domain", "paths": ["src/domain/**"]}},
    {{"name": "application", "paths": ["src/application/**"]}},
    {{"name": "infrastructure", "paths": ["src/infrastructure/**"]}}
  ],
  "allow": {{
    "domain": [],
    "application": ["domain"],
    "infrastructure": ["application", "domain"]
  }},
  "deny": [{{"from": "domain", "to": "infrastructure"}}]
}}

Replace the example with actual repository boundaries. Every production file must match exactly one intended module. Declare dependency direction from architecture, not from the current accidental imports. For an unsupported import syntax, configure `dependencies.command` and `dependencies.edges_report` to emit `{{"edges":[{{"from":"path","to":"path","line":1}}]}}`."""


def format_command(result: CommandResult) -> str:
    status = "timeout" if result.timed_out else f"exit {result.returncode}"
    return f"$ {shlex.join(result.command)}\n[{status}; {result.duration_seconds:.2f}s]\n{result.stdout}".strip()


def without_fast_flag(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return command.replace(" --fast", "")
    return shlex.join([part for part in parts if part != "--fast"])


def master_fix_prompt(report: AnalysisReport) -> str:
    failing_functions = sorted(
        (function for function in report.functions if not function.passed),
        key=lambda function: (
            -function.craap_score,
            function.coverage_percent,
            function.path,
            function.start_line,
        ),
    )
    survivors = [mutation for mutation in report.mutations if mutation.survived]
    failed_installs = [result for result in report.tool_setup if result.returncode != 0]
    gate_summary = "\n".join(
        f"- {gate.title}: {gate_outcome(gate)} — {gate.summary}"
        for gate in report.gates
    )
    failure_evidence = (
        "\n\n".join(
            f"{gate.title}:\n"
            + "\n".join(f"- {detail}" for detail in gate.details[:20])
            for gate in report.gates
            if gate.applicable
            and not gate.deferred
            and not gate.passed
            and gate.details
        )
        or "No additional command output was recorded. Read each gate summary above."
    )
    function_summary = (
        "\n".join(
            f"- {function.path}:{function.start_line} `{function.name}` — coverage {function.coverage_percent:.2f}%, complexity {function.complexity}, CRAAP {function.craap_score:.2f}"
            for function in failing_functions[:50]
        )
        or "- None in the current report."
    )
    mutation_summary = (
        "\n".join(
            f"- {mutation.path}:{mutation.line}:{mutation.column} — `{mutation.original}` -> `{mutation.replacement}` (mutant {mutation.mutant_id})"
            for mutation in survivors[:50]
        )
        or "- None in the current report."
    )
    dependency_summary = (
        "\n".join(
            f"- {item.source}:{item.line} ({item.source_module}) -> {item.target} ({item.target_module}): {item.rule}"
            for item in report.dependency_violations[:50]
        )
        or "- None in the current report."
    )
    install_summary = (
        "\n\n".join(format_command(result) for result in failed_installs)
        or "No automatic tool installation failed."
    )
    rerun_command = report.rerun_command or (
        f"python3 {shlex.quote(str(Path(__file__).resolve()))} --root ."
    )
    full_command = without_fast_flag(rerun_command)
    if report.mode == "fast" and report.ready_for_full:
        objective = """The fast diagnostic checks are green. Do not claim the repository is ready to ship yet. Run the full certification command now; it executes the deferred flaky-test repetitions and exhaustive mutation gate."""
        run_instructions = f"""Run from the repository root:
{full_command}"""
        loop_rule = "Run the full certification command now. If it finds failures, repair them and rerun the full gate until it exits 0."
    elif report.mode == "fast":
        objective = """Fix every issue measured by this fast diagnostic run. Work autonomously and rerun fast mode after each coherent batch. Fast mode can never certify the repository: once its executed checks are green, run the full certification command and continue until that full gate exits 0."""
        run_instructions = f"""Fast diagnostic command:
{rerun_command}

Required full certification command:
{full_command}"""
        loop_rule = "Rerun fast mode after each coherent repair batch. Once it reports READY_FOR_FULL, run the full certification command and continue until the full gate exits 0."
    else:
        objective = """Fix every issue reported by the repository quality gate. Work autonomously and keep looping until the gate exits with code 0. Do not stop after fixing only the examples listed here; each rerun may reveal the next failures."""
        run_instructions = f"""Run from the repository root:
{rerun_command}"""
        loop_rule = "After each coherent batch, run the focused tests, then rerun the full gate. Continue until the full gate exits 0 and report final evidence for every applicable check."
    return f"""You are the lead quality-repair agent for this repository:
{report.root}

{objective}

{run_instructions}

Non-negotiable finish conditions:
1. Every applicable formatter and linter command passes with zero violations.
2. Every applicable static type checker passes with zero errors.
3. Every detected or configured contract/schema check passes.
4. The complete test suite passes; every production function has 100% executable-line coverage and CRAAP <= 6.
5. Every applicable dead-code detector reports zero findings.
6. The complete test suite passes consistently across every configured flaky-test run.
7. The complete test suite kills every generated operator mutant; zero survive.
8. The dependency checker reports zero ownership or direction-rule violations.

Current gate status:
{gate_summary}

Highest-priority CRAAP failures ({len(failing_functions)} total; first 50 shown):
{function_summary}

Surviving mutants ({len(survivors)} total; first 50 shown):
{mutation_summary}

Dependency violations ({len(report.dependency_violations)} total; first 50 shown):
{dependency_summary}

Other failing-gate evidence:
{failure_evidence}

Automatic installation failures:
{install_summary}

Repair rules:
- Read neighboring production code and tests before editing.
- For uncovered behavior or a surviving mutant, add a behavior-focused test through the production API and prove it fails against the unfixed or mutated code.
- Simplify control flow without changing behavior until each function meets the CRAAP limit. Preserve public contracts and error paths.
- Fix formatter, lint, type, contract, and dead-code findings in production code; do not hide them with ignore comments, generated baselines, exclusions, or weakened configuration.
- Eliminate test nondeterminism at its source. Do not use retries or quarantine to conceal flaky behavior.
- Fix architecture violations by moving responsibility, splitting a mixed module, or inverting the dependency behind an owned interface. Do not broaden rules merely to make the checker green.
- Do not disable a gate, lower thresholds, cap mutants, add coverage exclusions, skip tests, weaken assertions, add suppressions, or replace commands with no-ops.
- {loop_rule}"""


def gate_outcome(gate: GateResult) -> str:
    if gate.deferred:
        return "DEFERRED"
    if not gate.applicable:
        return "NOT APPLICABLE"
    return "PASS" if gate.passed else "FAIL"


def gate_card_html(gate: GateResult, presentation: tuple[str, str, str, str]) -> str:
    step, question, kicker, explanation = presentation
    state = (
        "deferred"
        if gate.deferred
        else ("na" if not gate.applicable else ("pass" if gate.passed else "fail"))
    )
    badge = (
        "FULL RUN ONLY"
        if gate.deferred
        else (
            "NOT NEEDED"
            if not gate.applicable
            else ("LOOKS GOOD" if gate.passed else "FIX THIS")
        )
    )
    return f"""<article class="gate {state}">
      <div class="gate-top"><span class="step">{html.escape(step)}</span><span class="badge">{badge}</span></div>
      <div class="kicker">{html.escape(kicker)}</div>
      <h3>{html.escape(question)}</h3>
      <p class="explain">{html.escape(explanation)}</p>
      <p class="result">{html.escape(gate.summary)}</p>
      {details_html(gate.details)}
      {commands_html(gate.command_results)}
    </article>"""


def html_report(report: AnalysisReport) -> str:
    if report.mode == "fast":
        status = (
            "READY FOR FULL RUN" if report.ready_for_full else "FAST CHECKS NEED WORK"
        )
        page_heading = "Fast quality check"
        verdict_class = "diagnostic"
    else:
        status = "READY TO SHIP" if report.passed else "NOT READY YET"
        page_heading = "Can I ship this?"
        verdict_class = "pass" if report.passed else "fail"
    coverage_limit = report.functions[0].coverage_limit if report.functions else 100
    craap_limit = report.functions[0].craap_limit if report.functions else 6
    applicable_gates = sum(
        gate.applicable and not gate.deferred for gate in report.gates
    )
    passed_gates = sum(
        gate.passed and gate.applicable and not gate.deferred for gate in report.gates
    )
    not_applicable_gates = sum(not gate.applicable for gate in report.gates)
    deferred_gates = sum(gate.deferred for gate in report.gates)
    gate_language = {
        "format_lint": (
            "1",
            "Is the code formatted and idiomatic?",
            "Formatter + lint",
            "Every detected project formatter and linter must pass without changing files.",
        ),
        "types": (
            "2",
            "Do the types agree?",
            "Static types",
            "Configured type checkers must report zero errors before tests run.",
        ),
        "contracts": (
            "3",
            "Are service contracts valid?",
            "Contracts + schemas",
            "Detected OpenAPI and JSON Schema files, plus configured compatibility checks, must pass.",
        ),
        "quality": (
            "4",
            "Are all code paths tested?",
            "Tests + coverage + complexity",
            f"The complete suite must pass, with {coverage_limit:g}% function coverage and CRAAP {craap_limit:g} or lower.",
        ),
        "dead_code": (
            "5",
            "Is unused code gone?",
            "Dead code",
            "Configured high-confidence unused-code detectors must report zero findings.",
        ),
        "flaky": (
            "6",
            "Are the tests repeatable?",
            "Flaky-test detection",
            "Repeated complete-suite runs must produce consistent passing results.",
        ),
        "mutation": (
            "7",
            "Would tests catch wrong code?",
            "Mutation testing",
            "The gate makes tiny wrong changes. Your tests must catch every one.",
        ),
        "dependencies": (
            "8",
            "Does the architecture stay clean?",
            "Module boundaries",
            "Imports must follow the dependency rules declared by the project.",
        ),
    }
    gate_cards = "".join(
        gate_card_html(
            gate,
            gate_language.get(gate.key, ("•", gate.title, gate.title, gate.summary)),
        )
        for gate in report.gates
    )
    ordered_functions = sorted(
        report.functions,
        key=lambda function: (
            function.passed,
            -function.craap_score,
            function.path,
            function.start_line,
        ),
    )
    shown_functions = ordered_functions[:200]
    function_rows = (
        "".join(
            f"""<tr class="{"ok" if function.passed else "bad"}">
          <td><code>{html.escape(function.path)}:{function.start_line}</code></td>
          <td>{html.escape(function.name)}</td><td>{function.coverage_percent:.2f}%</td>
          <td>{function.complexity}</td><td>{function.craap_score:.2f}</td>
          <td>{html.escape(function.parser)}</td><td>{"PASS" if function.passed else "FAIL"}</td>
        </tr>"""
            for function in shown_functions
        )
        or '<tr><td colspan="7">No function metrics produced.</td></tr>'
    )
    if len(ordered_functions) > len(shown_functions):
        function_rows += f'<tr><td colspan="7">Showing the 200 highest-priority functions out of {len(ordered_functions)}. Fix and rerun to refresh this list.</td></tr>'
    ordered_mutations = sorted(
        report.mutations,
        key=lambda mutation: (
            not mutation.survived,
            mutation.path,
            mutation.line,
            mutation.column,
        ),
    )
    shown_mutations = ordered_mutations[:200]
    mutation_rows = (
        "".join(
            f"""<tr class="{"bad" if mutation.survived else "ok"}"><td><code>{mutation.mutant_id}</code></td>
        <td><code>{html.escape(mutation.path)}:{mutation.line}:{mutation.column}</code></td>
        <td><code>{html.escape(mutation.original)} → {html.escape(mutation.replacement)}</code></td>
        <td>{"SURVIVED" if mutation.survived else "KILLED"}</td><td>{mutation.duration_seconds:.2f}s</td></tr>"""
            for mutation in shown_mutations
        )
        or '<tr><td colspan="5">No mutants executed.</td></tr>'
    )
    if len(ordered_mutations) > len(shown_mutations):
        mutation_rows += f'<tr><td colspan="5">Showing 200 priority mutants out of {len(ordered_mutations)}.</td></tr>'
    shown_dependencies = report.dependency_violations[:200]
    dependency_rows = (
        "".join(
            f"""<tr class="bad"><td><code>{html.escape(item.source)}:{item.line}</code></td>
        <td>{html.escape(item.source_module)}</td><td><code>{html.escape(item.target)}</code></td>
        <td>{html.escape(item.target_module)}</td><td>{html.escape(item.rule)}</td></tr>"""
            for item in shown_dependencies
        )
        or '<tr><td colspan="5">No dependency violations.</td></tr>'
    )
    if len(report.dependency_violations) > len(shown_dependencies):
        dependency_rows += f'<tr><td colspan="5">Showing 200 violations out of {len(report.dependency_violations)}.</td></tr>'
    prompts = []
    for gate in report.gates:
        shown_prompts = gate.prompts[:3]
        for title, prompt in shown_prompts:
            prompt_id = f"prompt-issue-{len(prompts)}"
            prompts.append(f"""<article class="prompt"><div class="prompt-head"><h3>{html.escape(title)}</h3>
            <button data-copy="{prompt_id}">Copy prompt</button></div>
            <pre id="{prompt_id}">{html.escape(prompt)}</pre></article>""")
        if len(gate.prompts) > len(shown_prompts):
            prompts.append(
                f'<article class="prompt more"><h3>{len(gate.prompts) - len(shown_prompts)} more {html.escape(gate.title)} fixes</h3><p>Start with the prompts shown, then rerun the gate. The next highest-priority fixes will move into this queue.</p></article>'
            )
    if report.passed:
        prompt_html = '<div class="all-clear">Nothing to fix. Every applicable check is green.</div>'
    else:
        master_prompt = html.escape(master_fix_prompt(report))
        individual_prompt_html = "".join(prompts)
        if individual_prompt_html:
            individual_prompt_html = f"""<div class="prompt-group-heading"><h3>Prefer smaller tasks?</h3>
            <p>Use these focused prompts one issue at a time, then rerun the gate.</p></div>{individual_prompt_html}"""
        if report.mode == "fast" and report.ready_for_full:
            prompt_kicker = "Fast checks are green · certification remains"
            prompt_heading = "Run full certification"
            prompt_copy = "Copy full-run prompt"
            prompt_explanation = "The expensive flaky-test and mutation gates were deferred. Run them now before shipping."
        elif report.mode == "fast":
            prompt_kicker = "Fast diagnostic · one agent task"
            prompt_heading = "Fix measured issues"
            prompt_copy = "Copy repair prompt"
            prompt_explanation = "Repair the executed failures quickly, rerun fast mode, then complete a full certification run."
        else:
            prompt_kicker = "All failing gates · one agent task"
            prompt_heading = "Fix every issue"
            prompt_copy = "Copy complete prompt"
            prompt_explanation = "Paste this once. It tells the agent to repair every applicable check and keep rerunning the gate until it passes."
        prompt_html = f"""<article class="prompt master-prompt">
          <div class="prompt-head"><div><div class="kicker">{prompt_kicker}</div><h3>{prompt_heading}</h3></div>
          <button data-copy="prompt-fix-everything">{prompt_copy}</button></div>
          <p>{prompt_explanation}</p>
          <pre id="prompt-fix-everything">{master_prompt}</pre>
        </article>{individual_prompt_html}"""
    notes_html = "".join(f"<li>{html.escape(note)}</li>" for note in report.notes)
    language_text = ", ".join(report.languages)
    setup_failed = sum(result.returncode != 0 for result in report.tool_setup)
    if report.tool_setup:
        setup_summary = (
            f"Ran {len(report.tool_setup)} automatic install command(s); "
            f"{setup_failed} failed."
        )
        setup_evidence = commands_html(report.tool_setup)
    else:
        setup_summary = "No install was needed. Built-in tools and existing project tools were enough."
        setup_evidence = ""
    if report.mode == "fast":
        quick_guide = """<div><strong>Purple means diagnostic</strong>A full run is still required.</div><div><strong>Green means measured clean</strong>This executed check passed.</div><div><strong>Red means repair</strong>Fix it during the fast loop.</div><div><strong>Deferred means later</strong>It runs during certification.</div>"""
    else:
        quick_guide = """<div><strong>Green means go</strong>No action needed.</div><div><strong>Red means pause</strong>Fix it before shipping.</div><div><strong>Gray means not applicable</strong>No supported project check was detected.</div><div><strong>Need help?</strong>Copy an AI repair prompt below.</div>"""
    functions_passing = sum(function.passed for function in report.functions)
    mutants_killed = sum(not mutation.survived for mutation in report.mutations)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_heading} — {status}</title>
<style>
:root{{--bg:#f6f4ff;--ink:#17152b;--muted:#67637b;--card:#fff;--line:#ded9f0;--purple:#6d4aff;--purple-soft:#eee9ff;--good:#087a55;--good-soft:#e5f8f0;--bad:#c72c41;--bad-soft:#fff0f2;--na:#6b7280;--na-soft:#f1f2f4;--diagnostic:#7047c9;--diagnostic-soft:#f1eaff;--code:#19172b}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 8% 0,#ebe5ff 0,transparent 32%),var(--bg);color:var(--ink);font:16px/1.58 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}}
main{{max-width:1220px;margin:auto;padding:42px 28px 72px}} h1,h2,h3{{line-height:1.12;letter-spacing:-.025em}} h1{{font-size:clamp(42px,7vw,76px);margin:.12em 0}} h2{{font-size:28px;margin:52px 0 8px}} h3{{font-size:24px;margin:8px 0 12px}}
.eyebrow,.kicker{{color:var(--purple);font-size:13px;font-weight:900;letter-spacing:.1em;text-transform:uppercase}} .muted,.section-note{{color:var(--muted)}}
.hero{{display:grid;grid-template-columns:1fr auto;gap:28px;align-items:end;padding:30px;border:1px solid var(--line);border-radius:26px;background:rgba(255,255,255,.78);box-shadow:0 18px 60px rgba(67,47,140,.09)}}
.verdict{{min-width:220px;text-align:center;font-weight:950;font-size:24px;padding:18px 22px;border-radius:18px}} .verdict.pass{{color:var(--good);background:var(--good-soft)}} .verdict.fail{{color:var(--bad);background:var(--bad-soft)}} .verdict.diagnostic{{color:var(--diagnostic);background:var(--diagnostic-soft)}}
.progress{{margin-top:16px;color:var(--muted);font-weight:700}} .progress strong{{color:var(--ink)}} .quick-guide{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px}} .quick-guide div{{padding:14px 16px;background:rgba(255,255,255,.7);border:1px solid var(--line);border-radius:14px}} .quick-guide strong{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} .gate,.prompt,.setup{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 10px 35px rgba(67,47,140,.06)}}
.gate.pass{{border-top:6px solid var(--good)}} .gate.fail{{border-top:6px solid var(--bad)}} .gate.na{{border-top:6px solid var(--na)}} .gate.deferred{{border-top:6px solid var(--diagnostic)}} .gate-top,.prompt-head{{display:flex;align-items:center;justify-content:space-between;gap:12px}} .step{{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;background:var(--purple-soft);color:var(--purple);font-weight:950}} .badge{{font-size:12px;font-weight:950;padding:6px 9px;border-radius:99px}} .pass .badge{{background:var(--good-soft);color:var(--good)}} .fail .badge{{background:var(--bad-soft);color:var(--bad)}} .na .badge{{background:var(--na-soft);color:var(--na)}} .deferred .badge{{background:var(--diagnostic-soft);color:var(--diagnostic)}}
.explain{{color:var(--muted);min-height:76px}} .result{{padding:12px 14px;border-radius:12px;font-weight:750}} .pass .result{{background:var(--good-soft);color:var(--good)}} .fail .result{{background:var(--bad-soft);color:var(--bad)}} .na .result{{background:var(--na-soft);color:var(--na)}} .deferred .result{{background:var(--diagnostic-soft);color:var(--diagnostic)}} .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}} .stat{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}} .stat strong{{display:block;font-size:26px}} .stat span{{color:var(--muted)}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px;background:var(--card)}} table{{width:100%;border-collapse:collapse}} th,td{{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}} th{{background:#f0edfa;font-size:13px;text-transform:uppercase;letter-spacing:.05em}} tr.bad{{background:#fff8f9}} tr.bad td:last-child{{color:var(--bad);font-weight:900}} tr.ok td:last-child{{color:var(--good);font-weight:800}}
code,pre{{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}} pre{{white-space:pre-wrap;word-break:break-word;background:var(--code);color:#f4f1ff;padding:15px;border-radius:12px;max-height:420px;overflow:auto}} details{{margin-top:12px}} summary{{cursor:pointer;font-weight:800;color:var(--purple)}} button{{background:var(--purple);color:#fff;border:0;border-radius:10px;padding:9px 13px;font-weight:900;cursor:pointer}} button.copied{{background:var(--good)}}
.prompt{{border-left:5px solid var(--purple)}} .prompt h3{{font-size:18px;letter-spacing:0}} .master-prompt{{grid-column:1/-1;border:2px solid var(--purple);background:linear-gradient(135deg,#fff 0,#f5f1ff 100%)}} .master-prompt h3{{font-size:28px;margin-top:5px}} .master-prompt p{{color:var(--muted)}} .prompt-group-heading{{grid-column:1/-1;margin-top:12px}} .prompt-group-heading h3{{margin-bottom:4px}} .prompt-group-heading p{{margin:0;color:var(--muted)}} .all-clear{{grid-column:1/-1;padding:24px;background:var(--good-soft);color:var(--good);font-weight:850;border-radius:16px}} .setup{{padding:18px 22px}} ul{{padding-left:20px}}
@media(max-width:900px){{.grid,.quick-guide{{grid-template-columns:1fr}}.stats{{grid-template-columns:1fr 1fr}}.explain{{min-height:0}}}} @media(max-width:650px){{main{{padding:18px}}.hero{{grid-template-columns:1fr;padding:22px}}.verdict{{min-width:0}}.stats{{grid-template-columns:1fr}}}}
</style></head><body><main>
<section class="hero"><div><div class="eyebrow">CODE CONFIDENCE CHECK · v{VERSION} · {report.mode.upper()}</div><h1>{page_heading}</h1>
<div class="muted">{html.escape(report.root)}<br>{html.escape(report.generated_at)} · {html.escape(language_text)}</div>
<div class="progress"><strong>{passed_gates} of {applicable_gates}</strong> executed checks passed · {deferred_gates} deferred · {not_applicable_gates} not applicable</div></div>
<div class="verdict {verdict_class}">{status}</div></section>
<section class="quick-guide">{quick_guide}</section>
<h2>Your quality checks</h2><p class="section-note">Start here. Technical proof is lower down when you need it.</p><section class="grid">{gate_cards}</section>
<section class="stats"><div class="stat"><strong>{functions_passing}/{len(report.functions)}</strong><span>functions meet coverage + CRAAP</span></div><div class="stat"><strong>{mutants_killed}/{len(report.mutations)}</strong><span>wrong-code mutations caught</span></div><div class="stat"><strong>{len(report.dependency_violations)}</strong><span>architecture violations</span></div></section>
<h2>Fix with your coding agent</h2><p class="section-note">Choose the complete all-in-one prompt or a focused prompt for one issue.</p><section class="grid">{prompt_html}</section>
<h2>Automatic tool setup</h2><section class="setup"><strong>{html.escape(setup_summary)}</strong>{setup_evidence}</section>
<h2>Function health</h2><p class="section-note">Lower CRAAP is better. Required: {coverage_limit:g}% executable-line coverage and CRAAP ≤ {craap_limit:g}.</p>
<div class="table-wrap"><table><thead><tr><th>Location</th><th>Function</th><th>Coverage</th><th>Complexity</th><th>CRAAP</th><th>Parser</th><th>Status</th></tr></thead><tbody>{function_rows}</tbody></table></div>
<h2>Test strength</h2><p class="section-note">A “survived” mutant is a tiny wrong-code change your tests failed to catch. Required: zero survivors.</p>
<div class="table-wrap"><table><thead><tr><th>ID</th><th>Location</th><th>Change</th><th>Result</th><th>Time</th></tr></thead><tbody>{mutation_rows}</tbody></table></div>
<h2>Architecture boundaries</h2><p class="section-note">These imports cross a boundary your project says should stay closed.</p><div class="table-wrap"><table><thead><tr><th>Source</th><th>From module</th><th>Target</th><th>To module</th><th>Broken rule</th></tr></thead><tbody>{dependency_rows}</tbody></table></div>
<h2>Run details</h2><section class="setup"><ul>{notes_html}</ul></section>
</main><script>
document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{{
 const text=document.getElementById(button.dataset.copy).textContent;
 const originalLabel=button.textContent;
 try{{await navigator.clipboard.writeText(text)}}catch(error){{
   const area=document.createElement('textarea'); area.value=text; document.body.appendChild(area);
   area.select(); document.execCommand('copy'); area.remove();
 }}
 button.textContent='Copied'; button.classList.add('copied'); setTimeout(()=>{{button.textContent=originalLabel;button.classList.remove('copied')}},1600);
}}));
</script></body></html>"""


def details_html(details: Sequence[str]) -> str:
    if not details:
        return ""
    content = "\n".join(details)
    return f"<details><summary>Details ({len(details)})</summary><pre>{html.escape(content)}</pre></details>"


def commands_html(results: Sequence[CommandResult]) -> str:
    if not results:
        return ""
    content = "\n\n".join(format_command(result) for result in results)
    return f"<details><summary>Command evidence ({len(results)})</summary><pre>{html.escape(content)}</pre></details>"


def config_template(root: Path) -> dict[str, Any]:
    test = infer_test_command(root)
    package = read_package_json(root)
    return deep_merge(
        default_config(),
        {
            "test": {"command": test},
            "mutation": {"test_command": test},
            "_adapter_contract": {
                "metrics": {
                    "functions": [
                        {
                            "path": "src/file.ext",
                            "name": "functionName",
                            "start_line": 1,
                            "end_line": 10,
                            "complexity": 2,
                            "covered_lines": 5,
                            "total_lines": 5,
                            "coverage_percent": 100,
                        }
                    ]
                },
                "dependencies": {
                    "edges": [{"from": "src/a.ext", "to": "src/b.ext", "line": 1}]
                },
            },
            "_note": "Commands are argument arrays. Use ['bash','-lc','...'] only when shell syntax is required. Remove underscore-prefixed documentation keys if desired.",
            "_detected_package": package.get("name") if package else None,
        },
    )


def write_initial_config(root: Path, path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing configuration: {path}")
    path.write_text(
        json.dumps(config_template(root), indent=2) + "\n", encoding="utf-8"
    )


def run(
    root: Path,
    config: dict[str, Any],
    report_path: Path,
    cli_max_mutants: int | None,
    notes: list[str],
    fast: bool = False,
) -> AnalysisReport:
    languages = detect_languages(root)
    source_files = discover_source_files(root, config["source"])
    notes.append(f"Detected languages: {', '.join(languages)}")
    notes.append(f"Selected {len(source_files)} production source files")
    if fast:
        notes.append(
            "Fast diagnostic mode: flaky-test repetitions and mutation testing are deferred; this run cannot certify the repository."
        )
    tools = bootstrap_tools(root, config, source_files)
    notes.append(
        "Mutation testing and dependency checking are built in; no package install is required for those gates."
    )
    if tools.setup_results:
        succeeded = sum(result.returncode == 0 for result in tools.setup_results)
        notes.append(
            f"Automatic tool setup completed {succeeded}/{len(tools.setup_results)} install commands successfully."
        )
    else:
        notes.append(
            "All detected analysis tools were already available or unnecessary."
        )
    with tempfile.TemporaryDirectory(prefix="repo-quality-gate-") as temporary:
        workspace = Path(temporary)
        format_lint_gate = run_command_check_gate(
            root,
            "format_lint",
            "Formatter & lint",
            config["format_lint"],
            infer_format_lint_commands(root, source_files, tools),
            "Configure format_lint.commands with non-mutating check-mode formatter and linter commands.",
            tools.python_env,
        )
        types_gate = run_command_check_gate(
            root,
            "types",
            "Static type checking",
            config["types"],
            infer_type_commands(root, source_files, tools),
            "Configure types.commands with the repository's complete static type checker.",
            tools.python_env,
        )
        contracts_gate = run_contract_gate(root, config, tools)
        test_command, test_baseline = run_test_baseline(root, config)
        if test_baseline and test_baseline.returncode == 0:
            raw_metrics_gate, functions = run_metrics_gate(
                root, config, source_files, workspace, tools
            )
        else:
            raw_metrics_gate = GateResult(
                "craap",
                "CRAAP: coverage + complexity",
                False,
                "Coverage and CRAAP were not run because baseline tests did not pass.",
            )
            functions = []
        quality_gate = combine_test_and_metrics_gate(
            raw_metrics_gate, test_command, test_baseline
        )
        dead_code_gate = run_command_check_gate(
            root,
            "dead_code",
            "Dead code",
            config["dead_code"],
            infer_dead_code_commands(root, source_files, tools),
            "Configure dead_code.commands with a high-confidence unused-code detector such as Vulture, Knip, or ts-prune.",
            tools.python_env,
        )
        if fast:
            flaky_gate = deferred_check(
                "flaky",
                "Flaky-test detection",
                "repeated complete-suite runs are reserved for full certification.",
            )
            mutation_gate = deferred_check(
                "mutation",
                "Mutation testing",
                "the exhaustive mutant run is reserved for full certification.",
            )
            mutations = []
        else:
            flaky_gate = run_flaky_test_gate(root, config, test_command, test_baseline)
            mutation_gate, mutations = run_mutation_gate(
                root, config, source_files, cli_max_mutants, test_baseline
            )
        dependency_gate, violations = run_dependency_gate(
            root, config, source_files, workspace
        )
    analysis = AnalysisReport(
        root=str(root),
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
        languages=languages,
        gates=[
            format_lint_gate,
            types_gate,
            contracts_gate,
            quality_gate,
            dead_code_gate,
            flaky_gate,
            mutation_gate,
            dependency_gate,
        ],
        functions=functions,
        mutations=mutations,
        dependency_violations=violations,
        tool_setup=tools.setup_results,
        notes=notes,
        mode="fast" if fast else "full",
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html_report(analysis), encoding="utf-8")
    return analysis


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", default=".", help="repository root (default: current directory)"
    )
    parser.add_argument(
        "--config",
        help=f"JSON configuration (default: ROOT/{CONFIG_NAME} when present)",
    )
    parser.add_argument(
        "--html",
        default=DEFAULT_REPORT,
        help=f"HTML report path (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--init", action="store_true", help=f"write a detected {CONFIG_NAME} and exit"
    )
    parser.add_argument(
        "--max-mutants", type=int, help="diagnostic cap; a capped run can never pass"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run one diagnostic pass and defer flaky-test repetitions and mutation testing; never certifies",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="do not automatically install missing analysis tools",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: repository root does not exist: {root}", file=sys.stderr)
        return 2
    config_path = Path(args.config).resolve() if args.config else root / CONFIG_NAME
    if args.init:
        try:
            write_initial_config(root, config_path)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"Wrote {config_path}")
        print(
            "Review source, format/lint, types, contracts, tests, metrics, dead-code, flaky-test, mutation, and dependency settings before the first enforcing run."
        )
        return 0
    report_path = Path(args.html)
    if not report_path.is_absolute():
        report_path = root / report_path
    try:
        config, notes = load_config(config_path if config_path.exists() else None)
        if args.no_install:
            config["tools"]["auto_install"] = False
        analysis = run(
            root, config, report_path, args.max_mutants, notes, fast=args.fast
        )
        rerun = [sys.executable, str(Path(__file__).resolve()), "--root", "."]
        if args.config:
            rerun.extend(["--config", str(config_path)])
        rerun.extend(["--html", str(report_path)])
        if args.no_install:
            rerun.append("--no-install")
        if args.fast:
            rerun.append("--fast")
        analysis.rerun_command = shlex.join(rerun)
        report_path.write_text(html_report(analysis), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as error:
        failure = AnalysisReport(
            root=str(root),
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S %z"),
            languages=detect_languages(root),
            gates=[
                GateResult(
                    "runner",
                    "Gate runner",
                    False,
                    f"The quality-gate runner stopped: {error}",
                    prompts=[
                        (
                            "Repair gate configuration",
                            f"Run the repository quality gate and repair this configuration or adapter error without disabling a required gate:\n\n{error}",
                        )
                    ],
                )
            ],
            functions=[],
            mutations=[],
            dependency_violations=[],
            tool_setup=[],
            notes=["The run stopped before all gates could be evaluated."],
            mode="fast" if args.fast else "full",
        )
        with contextlib.suppress(OSError):
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(html_report(failure), encoding="utf-8")
        print(f"error: {error}", file=sys.stderr)
        print(f"HTML report: {report_path}", file=sys.stderr)
        return 2
    for gate in analysis.gates:
        print(f"[{gate_outcome(gate)}] {gate.title}: {gate.summary}")
    print(f"HTML report: {report_path}")
    return 0 if analysis.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
