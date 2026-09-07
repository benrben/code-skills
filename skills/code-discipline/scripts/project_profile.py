"""Read-only project discovery and progressive quality setup questions.

Detection never runs project commands; only ``write_profile`` persists data."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union, cast

# fmt: off
JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]
PROFILE_NAME = ".quality/project-profile.json"
SOURCES = {"detected", "confirmed", "default", "missing"}
STATUSES = {"ready", "needs_context", "not_applicable"}
IGNORED = {
    ".git", ".hg", ".svn", ".quality", ".venv", "venv", "node_modules",
    "vendor", "target", "dist", "build", "out", "bin", "obj", "coverage",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".scala": "Scala", ".c": "C", ".h": "C", ".cc": "C++",
    ".cpp": "C++", ".hpp": "C++",
}
MANIFEST_LANGUAGES = {
    "pyproject.toml": "Python", "requirements.txt": "Python",
    "package.json": "JavaScript", "tsconfig.json": "TypeScript", "go.mod": "Go", "cargo.toml": "Rust",
    "pom.xml": "Java", "build.gradle": "Java", "build.gradle.kts": "Kotlin",
    "gemfile": "Ruby", "composer.json": "PHP",
}
FRAMEWORKS = {
    "FastAPI": ("fastapi",), "Django": ("django",), "Flask": ("flask",),
    "React": ('"react"',), "Next.js": ('"next"', "nextjs"),
    "Vue": ('"vue"',), "Svelte": ("svelte",), "Express": ("express",),
    "NestJS": ("@nestjs/", "nestjs"),
    "Spring": ("spring-boot", "org.springframework"),
    "ASP.NET": ("microsoft.aspnetcore",), "Rails": ("rails",),
    "Laravel": ("laravel/framework",), "Celery": ("celery",),
}
DATABASES = {
    "PostgreSQL": ("postgresql", "postgres", "psycopg"),
    "MySQL": ("mysql", "mariadb"), "SQLite": ("sqlite",),
    "MongoDB": ("mongodb", "mongoose", "pymongo"), "Redis": ("redis",),
}
OBSERVABILITY = {
    "OpenTelemetry": ("opentelemetry", "@opentelemetry/"),
    "Prometheus": ("prometheus", "prom-client"),
    "Sentry": ("sentry", "@sentry/"), "Datadog": ("datadog", "ddtrace"),
    "Structured logging": ("structlog", "pino", "winston", "serilog"),
}
# fmt: on


@dataclass(frozen=True, slots=True)
class ProfileFact:
    value: JsonValue
    source: str
    confidence: float
    status: str

    def __post_init__(self) -> None:
        if self.source not in SOURCES or self.status not in STATUSES:
            raise ValueError("invalid profile source or status")
        if not 0 <= self.confidence <= 1:
            raise ValueError("profile confidence must be between 0 and 1")

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class SetupQuestion:
    key: str
    category: str
    prompt: str
    reason: str
    options: tuple[str, ...]
    recommended: str

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "key": self.key,
            "category": self.category,
            "prompt": self.prompt,
            "reason": self.reason,
            "options": list(self.options),
            "recommended": self.recommended,
            "required": True,
        }


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    schema_version: int
    facts: dict[str, ProfileFact]
    dimensions: dict[str, dict[str, JsonValue]]
    questions: tuple[SetupQuestion, ...]

    def fact(self, name: str) -> ProfileFact:
        return self.facts[name]

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "facts": {
                name: fact.as_dict() for name, fact in sorted(self.facts.items())
            },
            "dimensions": cast(dict[str, JsonValue], self.dimensions),
            "questions": [question.as_dict() for question in self.questions],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class Snapshot:
    root: Path
    paths: tuple[str, ...]

    @property
    def names(self) -> set[str]:
        return {Path(path).name.lower() for path in self.paths}

    def read(self, path: str, limit: int = 1_000_000) -> str:
        target = self.root / path
        try:
            if not target.is_file() or target.stat().st_size > limit:
                return ""
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _snapshot(root: Path) -> Snapshot:
    resolved = root.resolve()
    paths: list[str] = []
    for directory, names, filenames in os.walk(resolved, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED)
        base = Path(directory)
        for filename in sorted(filenames):
            try:
                paths.append((base / filename).relative_to(resolved).as_posix())
            except ValueError:
                continue
    return Snapshot(resolved, tuple(paths))


def _fact(value: JsonValue, confidence: float = 0.95) -> ProfileFact:
    return ProfileFact(value, "detected", confidence, "ready")


def _missing() -> ProfileFact:
    return ProfileFact(None, "missing", 0, "needs_context")


def _json_file(snapshot: Snapshot, name: str) -> dict[str, Any]:
    path = next((path for path in snapshot.paths if path.lower() == name), None)
    if path is None:
        return {}
    try:
        value = json.loads(snapshot.read(path))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# fmt: off
def _manifest(snapshot: Snapshot) -> str:
    names = set(MANIFEST_LANGUAGES) | {"requirements-dev.txt"}
    return "\n".join(snapshot.read(path).lower() for path in snapshot.paths
                     if Path(path).name.lower() in names)
def _source_sample(snapshot: Snapshot) -> str:
    paths = [path for path in snapshot.paths
             if Path(path).suffix.lower() in LANGUAGES and not set(Path(path).parts) & {"test", "tests", "spec", "specs"}][:100]
    return "\n".join(snapshot.read(path, 100_000).lower() for path in paths)

def _markers(text: str,
             definitions: Mapping[str, tuple[str, ...]]) -> list[JsonValue]:
    return cast(list[JsonValue], sorted(
        label for label, markers in definitions.items()
        if any(marker in text for marker in markers)))

def _languages(snapshot: Snapshot) -> list[JsonValue]:
    found = {LANGUAGES[Path(path).suffix.lower()] for path in snapshot.paths
             if Path(path).suffix.lower() in LANGUAGES}
    found.update(language for name, language in MANIFEST_LANGUAGES.items()
                 if name in snapshot.names)
    return cast(list[JsonValue], sorted(found))

def _javascript_test(snapshot: Snapshot) -> Optional[str]:
    scripts = _json_file(snapshot, "package.json").get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("test"), str):
        return None
    manager = ("pnpm" if "pnpm-lock.yaml" in snapshot.names else
               "yarn" if "yarn.lock" in snapshot.names else "npm")
    return f"{manager} test"
def _python_test(snapshot: Snapshot, languages: set[str], manifest: str) -> Optional[str]:
    if "Python" not in languages or not any("test" in path.lower() for path in snapshot.paths):
        return None
    return ("python3 -m pytest" if "pytest" in manifest or
            "pytest.ini" in snapshot.names else "python3 -m unittest discover")
def _tests(snapshot: Snapshot, languages: set[str], manifest: str) -> list[JsonValue]:
    commands = {command for command in (_javascript_test(snapshot),
                _python_test(snapshot, languages, manifest)) if command}
    rules = (("Go" in languages, "go test -json ./..."),
             ("Rust" in languages, "cargo test --all --frozen"),
             ("pom.xml" in snapshot.names, "mvn -o test"),
             ("gradlew" in snapshot.names, "./gradlew --offline test"),
             (any(path.lower().endswith((".sln", ".csproj"))
                  for path in snapshot.paths), "dotnet test --no-restore"))
    commands.update(command for enabled, command in rules if enabled)
    return cast(list[JsonValue], sorted(commands))
def _package_entrypoints(snapshot: Snapshot) -> set[str]:
    found: set[str] = set()
    package = _json_file(snapshot, "package.json")
    for key in ("main", "module", "bin"):
        value = package.get(key)
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            found.update(item for item in value.values() if isinstance(item, str))
    return found
def _python_entrypoints(snapshot: Snapshot) -> set[str]:
    pyproject = next((snapshot.read(path) for path in snapshot.paths
                      if path == "pyproject.toml"), "")
    scripts = re.search(r"(?ms)^\[project\.scripts\]\s*(.*?)(?=^\[|\Z)",
                        pyproject)
    if not scripts:
        return set()
    return {f"{name}: {target}" for name, target in re.findall(
        r'^\s*([\w.-]+)\s*=\s*["\']([^"\']+)', scripts.group(1), re.M)}
def _entrypoints(snapshot: Snapshot) -> list[JsonValue]:
    candidates = {"main.py", "app.py", "manage.py", "main.go", "src/main.rs",
                  "index.js", "index.ts", "server.js", "server.ts"}
    found = _package_entrypoints(snapshot) | _python_entrypoints(snapshot)
    found.update(path for path in snapshot.paths if path.lower() in candidates
                 or path.lower().endswith("/main.go"))
    return cast(list[JsonValue], sorted(found))

def _api_specs(snapshot: Snapshot) -> list[JsonValue]:
    markers = ("openapi", "swagger", ".graphql", ".gql", ".proto")
    return cast(list[JsonValue], sorted(
        path for path in snapshot.paths
        if any(marker in path.lower() for marker in markers)))

def _has_name_prefix(paths: set[str], prefix: str) -> bool:
    return any(Path(path).name.startswith(prefix) for path in paths)
def _has_suffix(paths: set[str], suffix: str) -> bool:
    return any(path.endswith(suffix) for path in paths)
def _has_prefix(paths: set[str], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefixes) for path in paths)
def _has_compose_file(paths: set[str]) -> bool:
    return any("compose" in path and path.endswith((".yml", ".yaml"))
               for path in paths)
def _enabled_labels(rules: Mapping[str, bool]) -> list[JsonValue]:
    return cast(list[JsonValue], sorted(name for name, present in rules.items()
                                        if present))

def _infrastructure(snapshot: Snapshot) -> list[JsonValue]:
    paths = {path.lower() for path in snapshot.paths}
    rules = {
        "Docker": _has_name_prefix(paths, "dockerfile"),
        "Docker Compose": _has_compose_file(paths),
        "Terraform": _has_suffix(paths, ".tf"),
        "Kubernetes": _has_prefix(paths, ("k8s/", "kubernetes/", "helm/",
                                          "charts/")),
        "Serverless": bool(paths & {"serverless.yml", "serverless.yaml",
                                     "samconfig.toml"}),
    }
    return _enabled_labels(rules)

def _ci(snapshot: Snapshot) -> list[JsonValue]:
    paths = {path.lower() for path in snapshot.paths}
    rules = {".gitlab-ci.yml": "GitLab CI", "jenkinsfile": "Jenkins",
             "azure-pipelines.yml": "Azure Pipelines",
             ".circleci/config.yml": "CircleCI"}
    found = {label for marker, label in rules.items() if marker in paths}
    if any(path.startswith(".github/workflows/") for path in paths):
        found.add("GitHub Actions")
    return cast(list[JsonValue], sorted(found))

def _has_api(frameworks: set[str], api_specs: list[JsonValue]) -> bool:
    servers = {"FastAPI", "Django", "Flask", "Express", "NestJS", "Spring",
               "ASP.NET", "Rails", "Laravel"}
    return bool(api_specs) or bool(frameworks & servers)
def _has_worker(frameworks: set[str], manifest: str) -> bool:
    return "Celery" in frameworks or any(name in manifest
                                           for name in ("sidekiq", "bullmq"))
def _has_cli(snapshot: Snapshot, manifest: str) -> bool:
    return bool(_json_file(snapshot, "package.json").get("bin")) or \
        "[project.scripts]" in manifest
def _with_library_fallback(kinds: set[str], snapshot: Snapshot) -> set[str]:
    if not kinds and snapshot.names & set(MANIFEST_LANGUAGES):
        kinds.add("library")
    return kinds
def _project_types(frameworks: set[str], snapshot: Snapshot,
                   api_specs: list[JsonValue], manifest: str) -> list[JsonValue]:
    rules = {
        "web": bool(frameworks & {"React", "Next.js", "Vue", "Svelte"}),
        "api": _has_api(frameworks, api_specs),
        "worker": _has_worker(frameworks, manifest),
        "cli": _has_cli(snapshot, manifest),
    }
    kinds = set(cast(list[str], _enabled_labels(rules)))
    return cast(list[JsonValue], sorted(_with_library_fallback(kinds, snapshot)))

def _deployment(infrastructure: set[str], kinds: set[str],
                snapshot: Snapshot) -> list[JsonValue]:
    targets = set(infrastructure)
    if "Serverless" in infrastructure:
        targets.add("Serverless platform")
    if "library" in kinds or _json_file(snapshot, "package.json").get(
            "publishConfig"):
        targets.add("Package registry")
    if snapshot.names & {"vercel.json", "netlify.toml", "fly.toml", "render.yaml", "procfile"}:
        targets.add("Application platform")
    return cast(list[JsonValue], sorted(targets))

def _detected_databases(metadata: str, snapshot: Snapshot) -> list[JsonValue]:
    databases = _markers(metadata, DATABASES)
    if databases:
        return databases
    if any("migration" in path.lower() for path in snapshot.paths):
        return ["Database engine not detected"]
    return []
def _detected_observability(metadata: str, source: str) -> list[JsonValue]:
    found = _markers(metadata, OBSERVABILITY)
    if re.search(r'(?:@\w+\.(?:get|route)|\b(?:app|router)\.(?:get|use))\(\s*["\']/health(?:z)?["\']', source):
        found.append("Health endpoint")
    return cast(list[JsonValue], sorted(set(cast(list[str], found))))
def _data_safety_fact(databases: list[JsonValue]) -> ProfileFact:
    if databases:
        return _missing()
    return ProfileFact(None, "default", 1, "not_applicable")
def _detected_facts(snapshot: Snapshot) -> dict[str, ProfileFact]:
    manifest, source = _manifest(snapshot), _source_sample(snapshot)
    metadata = manifest + "\n".join(line for line in source.splitlines() if re.match(r"\s*(?:from|import|require|use|using)\b", line))
    languages = _languages(snapshot)
    frameworks = _markers(metadata, FRAMEWORKS)
    entries, api_specs = _entrypoints(snapshot), _api_specs(snapshot)
    infrastructure = _infrastructure(snapshot)
    kinds = _project_types(set(cast(list[str], frameworks)), snapshot,
                           api_specs, manifest)
    databases = _detected_databases(metadata, snapshot)
    observability = _detected_observability(metadata, source)
    facts = {
        "project_type": _fact(kinds, 0.8) if kinds else _missing(),
        "languages": _fact(languages), "frameworks": _fact(frameworks),
        "test_commands": _fact(_tests(snapshot, set(cast(list[str], languages)),
                                      manifest)),
        "entrypoints": _fact(entries, 0.85), "api_specs": _fact(api_specs),
        "databases": _fact(databases, 0.85),
        "infrastructure": _fact(infrastructure), "ci": _fact(_ci(snapshot)),
        "observability": _fact(observability, 0.85),
        "deployment_targets": _fact(_deployment(
            set(cast(list[str], infrastructure)), set(cast(list[str], kinds)),
            snapshot), 0.8),
        "critical_user_stories": _missing(),
        "observability_requirements": _missing(),
        "security_level": _missing(),
        "data_safety": _data_safety_fact(databases),
        "performance": ProfileFact({"mode": "measure_only"}, "default", 1,
                                   "ready"),
    }
    return facts
# fmt: on


def _needs(facts: Mapping[str, ProfileFact], name: str) -> bool:
    return name not in facts or facts[name].status == "needs_context"


def _dimension(
    applicable: bool, reason: str, missing: tuple[str, ...] = ()
) -> dict[str, JsonValue]:
    status = "needs_context" if missing else "ready" if applicable else "not_applicable"
    return {
        "applicable": applicable,
        "status": status,
        "reason": reason,
        "missing_context": list(missing),
    }


# fmt: off
def _missing_context(facts: Mapping[str, ProfileFact],
                     names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if _needs(facts, name))
def _context_if(facts: Mapping[str, ProfileFact], applies: bool,
                name: str) -> tuple[str, ...]:
    return (name,) if applies and _needs(facts, name) else ()
def _entry_context(facts: Mapping[str, ProfileFact],
                   runnable: bool) -> tuple[str, ...]:
    return ("entrypoints",) if runnable and not facts["entrypoints"].value else ()
def _dimensions(facts: Mapping[str, ProfileFact]
                ) -> dict[str, dict[str, JsonValue]]:
    kinds = set(cast(list[str], facts["project_type"].value or []))
    runnable = bool(kinds & {"web", "api", "cli", "worker"})
    database = bool(facts["databases"].value)
    story = _missing_context(facts, ("project_type", "critical_user_stories"))
    signals = _context_if(facts, runnable, "observability_requirements")
    security = _context_if(facts, True, "security_level")
    data = _context_if(facts, database, "data_safety")
    entry = _entry_context(facts, runnable)
    integration = any((database, "api" in kinds, bool(facts["api_specs"].value)))
    infrastructure = bool(facts["infrastructure"].value)
    return {
        "product": _dimension(True, "Critical behavior and acceptance.", story),
        "integration": _dimension(integration, "API and service contracts."),
        "performance": _dimension(runnable, "Measure-only until budgets exist."),
        "reliability": _dimension(runnable, "Startup and recovery.", entry),
        "ui_accessibility": _dimension("web" in kinds, "Browser accessibility."),
        "security": _dimension(True, "Risk-based security.", security),
        "supply_chain": _dimension(True, "Dependencies and provenance."),
        "infrastructure": _dimension(infrastructure, "Deployment policy."),
        "data_safety": _dimension(database, "Data recovery.", data),
        "observability": _dimension(runnable, "Runtime signals.", signals),
        "operations": _dimension(runnable, "Operational recovery.", signals),
        "governance": _dimension(True, "Ownership and release policy."),
        "language_semantics": _dimension(bool(facts["languages"].value),
                                         "Language-aware analysis."),
    }

def _question(key: str, category: str, prompt: str, reason: str,
              options: tuple[str, ...], recommended: str) -> SetupQuestion:
    return SetupQuestion(key, category, prompt, reason,
                         (*options, "Decide later"), recommended)

def _product_question(facts: Mapping[str, ProfileFact],
                      kinds: set[str]) -> Optional[SetupQuestion]:
    if _needs(facts, "project_type"):
        return _question("project_type", "product",
            "What is this project mainly used for?",
            "This selects the product checks that apply.",
            ("Web application", "API", "CLI", "Library", "Worker", "Mixed"),
            "Mixed")
    if not _needs(facts, "critical_user_stories"):
        return None
    subject = "public behavior" if kinds == {"library"} else "user flow"
    return _question("critical_user_stories", "product",
            f"What is the most important {subject} to protect?",
            "This creates a product smoke and acceptance target.",
            ("Enter one critical behavior",), "Enter one critical behavior")
def _runtime_question(facts: Mapping[str, ProfileFact],
                      runnable: bool) -> Optional[SetupQuestion]:
    if not runnable:
        return None
    if not facts["entrypoints"].value:
        return _question("entrypoints", "runtime",
            "Which command or URL starts the main product flow?",
            "This enables startup and runtime checks.",
            ("Enter command", "Enter URL"), "Enter command")
    if _needs(facts, "observability_requirements"):
        return _question("observability_requirements", "runtime",
            "Which runtime signals are required?",
            "This defines logs, health, metrics, and trace evidence.",
            ("Logs and health", "Add metrics", "Add metrics and traces", "None"),
            "Logs and health")
    return None
def _risk_question(facts: Mapping[str, ProfileFact]) -> Optional[SetupQuestion]:
    if facts["databases"].value and _needs(facts, "data_safety"):
        return _question("data_safety", "risk",
            "Which data recovery checks are required?",
            "This enables migration, rollback, backup, and restore evidence.",
            ("Migrations", "Migrations and rollback", "Full recovery"),
            "Migrations and rollback")
    if _needs(facts, "security_level"):
        return _question("security_level", "risk",
            "What level of data sensitivity does this project handle?",
            "This selects security checks and required evidence.",
            ("No sensitive data", "Internal", "Personal", "Payment",
             "Highly sensitive"), "Internal")
    return None
def _questions(facts: Mapping[str, ProfileFact],
               limit: int) -> tuple[SetupQuestion, ...]:
    kinds = set(cast(list[str], facts["project_type"].value or []))
    runnable = bool(kinds & {"web", "api", "cli", "worker"})
    candidates = (_product_question(facts, kinds),
                  _runtime_question(facts, runnable), _risk_question(facts))
    return tuple(question for question in candidates if question is not None)[:limit]
def _confirmed_mapping(raw: Mapping[object, object]) -> Optional[ProfileFact]:
    if raw.get("source") != "confirmed":
        return None
    try:
        return _loaded_fact(raw)
    except ValueError:
        return None

def _confirmed(raw: object) -> Optional[ProfileFact]:
    if isinstance(raw, ProfileFact):
        return raw if raw.source == "confirmed" else None
    if not isinstance(raw, Mapping):
        return None
    return _confirmed_mapping(raw)

def _existing_facts(existing: Optional[Mapping[str, object] | ProjectProfile]
                    ) -> Mapping[object, object]:
    if existing is None:
        return {}
    if isinstance(existing, ProjectProfile):
        return cast(Mapping[object, object], existing.facts)
    raw = existing.get("facts", existing)
    return raw if isinstance(raw, Mapping) else {}

def _merge_existing(facts: dict[str, ProfileFact],
                    existing: Optional[Mapping[str, object] | ProjectProfile]) -> None:
    for name, value in _existing_facts(existing).items():
        answer = _confirmed(value)
        if not isinstance(name, str) or answer is None:
            continue
        facts[name] = answer

def inspect_project(root: Path | str,
                    existing: Optional[Mapping[str, object] | ProjectProfile] = None,
                    question_limit: int = 3) -> ProjectProfile:
    """Inspect a repository without executing or changing it."""
    path = Path(root)
    if not path.is_dir():
        raise ValueError(f"project root is not a directory: {path}")
    if question_limit < 0:
        raise ValueError("question_limit cannot be negative")
    facts = _detected_facts(_snapshot(path))
    _merge_existing(facts, existing)
    return ProjectProfile(1, facts, _dimensions(facts),
                          _questions(facts, question_limit))

def profile_questions(profile: ProjectProfile) -> list[dict[str, JsonValue]]:
    return [question.as_dict() for question in profile.questions]

def _answer_is_ready(value: JsonValue) -> bool:
    return value is not None and value != "Decide later"

def _normalized_answer(key: str, value: JsonValue) -> JsonValue:
    if not isinstance(value, str):
        return value
    project_types = {"Web application": ["web"], "API": ["api"],
                     "CLI": ["cli"], "Library": ["library"],
                     "Worker": ["worker"], "Mixed": ["web", "api"]}
    if key == "project_type":
        return cast(JsonValue, project_types.get(value, [value.lower()]))
    if key in {"critical_user_stories", "entrypoints"}:
        return cast(JsonValue, [value])
    return value

def merge_answers(profile: ProjectProfile, answers: Mapping[str, JsonValue],
                  question_limit: int = 3) -> ProjectProfile:
    facts = dict(profile.facts)
    for key, value in answers.items():
        if not _answer_is_ready(value):
            continue
        value = _normalized_answer(key, value)
        json.dumps(value)
        facts[key] = ProfileFact(value, "confirmed", 1, "ready")
    return ProjectProfile(1, facts, _dimensions(facts),
                          _questions(facts, question_limit))

def _loaded_fact(raw: object) -> ProfileFact:
    if not isinstance(raw, Mapping):
        raise ValueError("profile fact must be an object")
    source = raw.get("source")
    status = raw.get("status")
    confidence = raw.get("confidence")
    if (not isinstance(source, str) or not isinstance(status, str)
            or not isinstance(confidence, (int, float))):
        raise ValueError("profile fact metadata is invalid")
    return ProfileFact(cast(JsonValue, raw.get("value")), source,
                       float(confidence), status)

def load_profile(path: Path | str) -> ProjectProfile:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("facts"), dict):
        raise ValueError("project profile must contain a facts object")
    facts = {str(name): _loaded_fact(value) for name, value in raw["facts"].items()}
    return ProjectProfile(1, facts, _dimensions(facts), _questions(facts, 3))

def write_profile(path: Path | str, profile: ProjectProfile) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(profile.to_json(), encoding="utf-8")
    return destination

generate_project_profile = inspect_project
detect_profile = inspect_project
# fmt: on
