"""Project-wide quality coverage built from measured facts and confirmed intent."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROFILE_NAME = ".quality/project-profile.json"
PROFILE_KEYS = {
    "product.behaviors": "critical_user_stories",
    "security.risk_level": "security_level",
    "data.backup_restore_success": "data_safety",
    "observability.health_checks": "observability_requirements",
    "operations.runbook_coverage": "observability_requirements",
}
BASELINE_REQUIRED_METRICS = frozenset("secret_exposure_count known_vulnerabilities cyclomatic_complexity cognitive_complexity crap duplication dependency_cycles".split())  # fmt: skip
OPTIONAL_METRICS = frozenset("escaped_defect_rate sast_dast_findings remediation_age bus_factor mutation_score maintainability_index npath cohesion".split())  # fmt: skip


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    key: str
    title: str
    domain: str
    applicability_fact: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSpec:
    key: str
    label: str
    dimension: str
    why: str
    fact: str | None = None
    answer: str | None = None
    status_without_adapter: str = "needs_context"
    required: bool = False


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    key: str
    label: str
    status: str
    value: Any
    source: str
    why: str
    action: str | None = None
    priority: str = "recommended"


@dataclass(frozen=True, slots=True)
class QualityDimension:
    key: str
    title: str
    domain: str
    status: str
    summary: str
    metrics: tuple[MetricEvidence, ...]


DIMENSIONS: tuple[DimensionSpec, ...] = (
    DimensionSpec("product", "Product behavior", "product"),
    DimensionSpec("integration", "Integration & compatibility", "product", "has_api"),
    DimensionSpec("performance", "Performance efficiency", "deployment", "deployable"),
    DimensionSpec("reliability", "Reliability & recovery", "deployment", "deployable"),
    DimensionSpec("ui_accessibility", "UI & accessibility", "product", "has_ui"),
    DimensionSpec("security", "Application security", "security"),
    DimensionSpec("supply_chain", "Supply-chain integrity", "security"),
    DimensionSpec(
        "infrastructure", "Infrastructure", "deployment", "has_infrastructure"
    ),
    DimensionSpec("data_safety", "Data safety", "deployment", "has_database"),
    DimensionSpec("observability", "Observability", "operations", "deployable"),
    DimensionSpec("operations", "Delivery & operations", "operations", "deployable"),
    DimensionSpec("governance", "Governance & ownership", "operations"),
    DimensionSpec("language_semantics", "Code & language semantics", "code"),
)


def metric(
    key: str,
    label: str,
    dimension: str,
    why: str,
    *,
    fact: str | None = None,
    answer: str | None = None,
    unsupported: bool = False,
    required: bool = False,
) -> MetricSpec:
    return MetricSpec(
        key,
        label,
        dimension,
        why,
        fact,
        answer,
        "unsupported" if unsupported else "needs_context",
        required,
    )


# The catalog stays dense so this portable module remains below the file limit.
# fmt: off
METRICS: tuple[MetricSpec, ...] = (
    metric("core_behaviors", "Core user behaviors", "product", "Defines the behaviors the acceptance evidence must prove.", answer="product.behaviors", required=True),
    metric("acceptance_pass_rate", "Acceptance pass rate", "product", "Proves core user behavior end to end.", fact="product.acceptance_pass_rate"),
    metric("requirements_coverage", "Requirements coverage", "product", "Connects tests to promised behavior.", answer="product.requirements_coverage"),
    metric("functional_coverage", "Functional coverage", "product", "Shows how much specified behavior is exercised.", answer="product.functional_coverage"),
    metric("escaped_defect_rate", "Escaped defect rate", "product", "Shows defects reaching users.", answer="product.escaped_defect_rate"),
    metric("contract_pass_rate", "Contract pass rate", "integration", "Detects incompatible API or schema changes.", fact="integration.contract_pass_rate", answer="integration.contract_validation", required=True),
    metric("endpoint_coverage", "API endpoint coverage", "integration", "Shows which external entry points are exercised.", answer="integration.endpoint_coverage"),
    metric("interaction_success", "External interaction success", "integration", "Checks real boundaries with other systems.", answer="integration.interaction_success"),
    metric("compatibility_matrix", "Compatibility matrix pass rate", "integration", "Checks supported platforms and versions.", answer="integration.compatibility_matrix"),
    metric("test_suite_seconds", "Test suite duration", "performance", "Keeps developer feedback measurable.", fact="performance.test_suite_seconds"),
    metric("latency_percentiles", "P50/P95/P99 latency", "performance", "Tail latency catches slow user experiences.", answer="performance.latency_percentiles"),
    metric("throughput", "Throughput", "performance", "Shows useful work handled per unit time.", answer="performance.throughput"),
    metric("resource_use", "CPU and memory per operation", "performance", "Finds capacity and cost pressure.", answer="performance.resource_use"),
    metric("startup_time", "Startup and cold-start time", "performance", "Measures readiness and first-use delay.", answer="performance.startup_time"),
    metric("error_rate", "Production error rate", "reliability", "Measures real failed operations.", answer="reliability.error_rate"),
    metric("availability", "Availability and SLO compliance", "reliability", "Tracks whether the service meets its promise.", answer="reliability.availability"),
    metric("mttr", "Mean time to recover", "reliability", "Measures recovery speed after failure.", answer="reliability.mttr"),
    metric("recovery_success", "Recovery test success", "reliability", "Proves failover and restoration work.", answer="reliability.recovery_success", required=True),
    metric("flaky_test_rate", "Flaky test rate", "reliability", "Shows whether test evidence is repeatable.", fact="tests.flaky_pass"),
    metric("accessibility_violations", "Accessibility violations", "ui_accessibility", "Finds barriers against the WCAG baseline.", answer="ui.accessibility_violations", required=True),
    metric("task_completion", "User task completion", "ui_accessibility", "Measures whether people complete key workflows.", answer="ui.task_completion"),
    metric("user_error_rate", "User error rate", "ui_accessibility", "Finds confusing or unsafe interaction paths.", answer="ui.user_error_rate"),
    metric("cross_platform_ui", "Cross-platform UI pass rate", "ui_accessibility", "Checks supported browsers and devices.", answer="ui.cross_platform_pass_rate"),
    metric("secret_exposure_count", "Secret exposure count", "security", "Prevents committed credentials from shipping.", fact="security.secret_findings"),
    metric("known_vulnerabilities", "Known dependency vulnerabilities", "security", "Finds advisories for locked versions.", fact="security.known_vulnerabilities"),
    metric("risk_level", "Security risk level", "security", "Selects the right verification depth for the product.", answer="security.risk_level", required=True),
    metric("sast_dast_findings", "SAST and DAST findings", "security", "Covers source and runtime weaknesses.", answer="security.sast_dast_findings"),
    metric("remediation_age", "Vulnerability remediation age", "security", "Prevents known risks from remaining indefinitely.", answer="security.remediation_age"),
    metric("lockfile_coverage", "Lockfile coverage", "supply_chain", "Makes dependency versions reproducible.", answer="supply_chain.lockfile_coverage"),
    metric("license_issues", "Dependency license issues", "supply_chain", "Finds incompatible distribution obligations.", answer="supply_chain.license_issues"),
    metric("sbom_provenance", "SBOM and build provenance", "supply_chain", "Makes shipped inputs and build origin traceable.", answer="supply_chain.sbom_provenance"),
    metric("ci_pinning", "Pinned CI actions and tools", "supply_chain", "Reduces mutable build dependencies.", answer="supply_chain.ci_pinning"),
    metric("iac_validation", "Infrastructure validation", "infrastructure", "Finds invalid infrastructure before deployment.", answer="infrastructure.validation", required=True),
    metric("misconfiguration_findings", "Infrastructure misconfigurations", "infrastructure", "Finds unsafe runtime defaults.", answer="infrastructure.misconfigurations"),
    metric("container_findings", "Container image findings", "infrastructure", "Covers image vulnerabilities and hardening.", answer="infrastructure.container_findings"),
    metric("drift", "Configuration drift", "infrastructure", "Shows deployed state diverging from declared state.", answer="infrastructure.drift"),
    metric("migration_pass_rate", "Forward migration pass rate", "data_safety", "Proves upgrades preserve usable data.", answer="data.migration_pass_rate"),
    metric("rollback_pass_rate", "Rollback migration pass rate", "data_safety", "Proves safe rollback when releases fail.", answer="data.rollback_pass_rate"),
    metric("backup_restore", "Backup restore success", "data_safety", "Proves backups can actually recover data.", answer="data.backup_restore_success", required=True),
    metric("schema_compatibility", "Schema compatibility", "data_safety", "Prevents incompatible rolling deployments.", answer="data.schema_compatibility"),
    metric("signal_coverage", "Logs, metrics, and traces coverage", "observability", "Makes failures diagnosable across boundaries.", answer="observability.signal_coverage"),
    metric("health_checks", "Health and readiness checks", "observability", "Lets automation distinguish ready from broken.", answer="observability.health_checks", required=True),
    metric("alert_tests", "Alert test pass rate", "observability", "Proves important failures notify an owner.", answer="observability.alert_test_pass_rate"),
    metric("deployment_frequency", "Deployment frequency", "operations", "Shows delivery cadence.", answer="operations.deployment_frequency"),
    metric("change_lead_time", "Change lead time", "operations", "Measures commit-to-production flow.", answer="operations.change_lead_time"),
    metric("change_fail_rate", "Change fail rate", "operations", "Shows deployments requiring intervention.", answer="operations.change_fail_rate"),
    metric("deployment_rework", "Deployment rework rate", "operations", "Shows emergency follow-up deployment load.", answer="operations.deployment_rework_rate"),
    metric("runbook_coverage", "Runbook coverage", "operations", "Gives responders tested recovery steps.", answer="operations.runbook_coverage", required=True),
    metric("ownership_coverage", "Ownership coverage", "governance", "Makes review and incident responsibility explicit.", answer="governance.ownership_coverage"),
    metric("bus_factor", "Bus factor and knowledge concentration", "governance", "Finds single-person knowledge risk.", answer="governance.bus_factor"),
    metric("public_api_docs", "Public API documentation coverage", "governance", "Keeps supported interfaces understandable.", answer="governance.public_api_docs"),
    metric("policy_files", "Security, contribution, and release policy", "governance", "Makes maintenance expectations reviewable.", answer="governance.policy_files"),
    metric("test_pass_rate", "Test pass rate", "language_semantics", "Proves the exercised behavior currently passes.", fact="tests.pass_rate", required=True),
    metric("branch_coverage", "Branch coverage", "language_semantics", "Exercises decision outcomes, not just lines.", fact="coverage.branch_percent", required=True),
    metric("mutation_score", "Mutation score", "language_semantics", "Shows whether tests detect wrong behavior.", fact="mutation.score"),
    metric("cyclomatic_complexity", "Cyclomatic complexity", "language_semantics", "Counts independent control-flow paths.", fact="complexity.max_cyclomatic"),
    metric("cognitive_complexity", "Cognitive complexity", "language_semantics", "Estimates how hard control flow is to understand.", fact="complexity.max_cognitive"),
    metric("crap", "CRAP", "language_semantics", "Combines complexity with missing coverage.", fact="complexity.max_crap"),
    metric("duplication", "Duplication percentage", "language_semantics", "Shows repeated source that multiplies change cost.", fact="source.duplication_percent"),
    metric("dependency_cycles", "Dependency cycles", "language_semantics", "Finds circular architectural coupling.", fact="architecture.cycles"),
    metric("fan_in_out", "Fan-in and fan-out", "language_semantics", "Shows incoming and outgoing coupling.", fact="architecture.fan_in_out"),
    metric("hotspots", "Change hotspots and churn", "language_semantics", "Combines change frequency with complexity risk.", fact="history.hotspots"),
    metric("maintainability_index", "Maintainability index", "language_semantics", "Combines size, complexity, and Halstead volume.", answer="adapters.maintainability_index", unsupported=True),
    metric("npath", "NPath complexity", "language_semantics", "Shows the number of acyclic execution paths.", answer="adapters.npath", unsupported=True),
    metric("cohesion", "Class and module cohesion", "language_semantics", "Finds responsibilities that do not belong together.", answer="adapters.cohesion", unsupported=True),
    metric("static_smells", "Static bug and smell density", "language_semantics", "Adds language-specific semantic findings.", answer="adapters.static_smells", unsupported=True),
)
# fmt: on


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read project profile {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Project profile {path} must contain a JSON object")
    return value


def _mapping_value(container: Any, key: str) -> Any:
    if not isinstance(container, Mapping):
        return None
    return container.get(key)


def _profile_record(value: Any) -> tuple[Any, str] | None:
    if not isinstance(value, Mapping) or "value" not in value:
        return None
    return value.get("value"), str(value.get("source", "confirmed"))


def profile_value(profile: Mapping[str, Any], key: str) -> tuple[Any, str]:
    value = _mapping_value(profile.get("answers"), key)
    if value is None:
        value = _mapping_value(profile.get("facts"), PROFILE_KEYS.get(key, key))
    record = _profile_record(value)
    if record is not None:
        return record
    if value is not None:
        return value, "confirmed"
    return None, "missing"


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _applicable(
    spec: DimensionSpec, profile: Mapping[str, Any], facts: Mapping[str, Any]
) -> bool:
    dimensions = profile.get("dimensions", {})
    configured = dimensions.get(spec.key) if isinstance(dimensions, Mapping) else None
    if isinstance(configured, Mapping) and isinstance(
        configured.get("applicable"), bool
    ):
        return bool(configured["applicable"])
    override, _source = profile_value(profile, f"dimension.{spec.key}.applicable")
    if isinstance(override, bool):
        return override
    return spec.applicability_fact is None or bool(facts.get(spec.applicability_fact))


def _measured_metric(
    spec: MetricSpec, facts: Mapping[str, Any]
) -> MetricEvidence | None:
    if spec.fact is None or spec.fact not in facts:
        return None
    return _new_evidence(spec, "measured", facts[spec.fact], "quality gate")


def _new_evidence(
    spec: MetricSpec, status: str, value: Any, source: str, action: str | None = None
) -> MetricEvidence:
    priority = _metric_priority(spec)
    return MetricEvidence(
        spec.key, spec.label, status, value, source, spec.why, action, priority
    )


def _missing_metric_action(spec: MetricSpec) -> str:
    if spec.answer:
        return f"Provide {spec.answer}."
    return "Add a compatible metric adapter."


def _missing_metric_status(spec: MetricSpec) -> str:
    if (
        _metric_priority(spec) == "required"
        or spec.status_without_adapter == "unsupported"
    ):
        return spec.status_without_adapter
    return "not_configured"


def _metric_priority(spec: MetricSpec) -> str:
    if spec.required or spec.key in BASELINE_REQUIRED_METRICS:
        return "required"
    if spec.key in OPTIONAL_METRICS:
        return "optional"
    return "recommended"


def _metric_evidence(
    spec: MetricSpec, profile: Mapping[str, Any], facts: Mapping[str, Any]
) -> MetricEvidence:
    measured = _measured_metric(spec, facts)
    if measured is not None:
        return measured
    value, source = profile_value(profile, spec.answer or "")
    if _has_value(value):
        return _new_evidence(spec, "confirmed", value, source)
    return _new_evidence(
        spec,
        _missing_metric_status(spec),
        None,
        "missing",
        _missing_metric_action(spec),
    )


def _dimension_status(metrics: Sequence[MetricEvidence]) -> str:
    statuses = {item.status for item in metrics}
    if "needs_context" in statuses:
        return "needs_context"
    if "confirmed" in statuses:
        return "confirmed"
    return "measured"


def _count_metric_statuses(
    metrics: Sequence[MetricEvidence], statuses: set[str]
) -> int:
    return sum(item.status in statuses for item in metrics)


def _optional_adapter_suffix(optional: int) -> str:
    if optional:
        return f"; {optional} optional adapters are not configured"
    return ""


def _dimension_summary(status: str, metrics: Sequence[MetricEvidence]) -> str:
    available = _count_metric_statuses(metrics, {"measured", "confirmed"})
    if status == "not_applicable":
        return "Not applicable to the detected project surface."
    if status == "needs_context":
        missing = _count_metric_statuses(metrics, {"needs_context"})
        return f"{available}/{len(metrics)} metrics available; {missing} need project context or an adapter."
    optional = _count_metric_statuses(metrics, {"unsupported", "not_configured"})
    suffix = _optional_adapter_suffix(optional)
    return f"{available}/{len(metrics)} metrics are available{suffix}."


def _dimension_metrics(key: str) -> tuple[MetricSpec, ...]:
    return tuple(item for item in METRICS if item.dimension == key)


def _assess_dimension(
    dimension: DimensionSpec,
    profile: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> QualityDimension:
    if not _applicable(dimension, profile, facts):
        return QualityDimension(
            dimension.key,
            dimension.title,
            dimension.domain,
            "not_applicable",
            _dimension_summary("not_applicable", ()),
            (),
        )
    metrics = tuple(
        _metric_evidence(item, profile, facts)
        for item in _dimension_metrics(dimension.key)
    )
    status = _dimension_status(metrics)
    return QualityDimension(
        dimension.key,
        dimension.title,
        dimension.domain,
        status,
        _dimension_summary(status, metrics),
        metrics,
    )


def assess_quality_dimensions(
    profile: Mapping[str, Any], facts: Mapping[str, Any]
) -> tuple[QualityDimension, ...]:
    return tuple(_assess_dimension(item, profile, facts) for item in DIMENSIONS)


def _domain_dimensions(
    name: str, dimensions: Sequence[QualityDimension]
) -> list[QualityDimension]:
    return [
        item
        for item in dimensions
        if item.domain == name and item.status != "not_applicable"
    ]


def _dimension_keys(
    dimensions: Sequence[QualityDimension], status: str | None = None
) -> list[str]:
    if status is None:
        return [item.key for item in dimensions]
    return [item.key for item in dimensions if item.status == status]


def _domain_status(
    relevant: Sequence[QualityDimension], incomplete: Sequence[str]
) -> str:
    if not relevant:
        return "not_applicable"
    if incomplete:
        return "needs_context"
    return "certified"


def _domain_state(name: str, dimensions: Sequence[QualityDimension]) -> dict[str, Any]:
    relevant = _domain_dimensions(name, dimensions)
    incomplete = _dimension_keys(relevant, "needs_context")
    return {
        "status": _domain_status(relevant, incomplete),
        "dimensions": _dimension_keys(relevant),
        "needs": incomplete,
    }


def certification_states(
    dimensions: Sequence[QualityDimension], repository_passed: bool
) -> dict[str, Any]:
    states = {
        name: _domain_state(name, dimensions)
        for name in ("product", "security", "deployment", "operations", "code")
    }
    states["repository"] = {
        "status": "certified" if repository_passed else "needs_work",
        "dimensions": [],
        "needs": [],
    }
    complete = {"certified", "not_applicable"}
    incomplete = [
        name for name, value in states.items() if value["status"] not in complete
    ]
    states["full_profile"] = {
        "status": "needs_context" if incomplete else "certified",
        "dimensions": [],
        "needs": incomplete,
    }
    return states


def _percentage(part: int, total: int) -> float | None:
    return round(part / total * 100, 2) if total else None


def _gate_pass(analysis: Any, key: str) -> bool | None:
    gate = next((item for item in analysis.gates if item.key == key), None)
    if gate is None or not gate.applicable or gate.deferred:
        return None
    return bool(gate.passed)


def _function_facts(functions: Sequence[Any]) -> dict[str, Any]:
    if not functions:
        return {}
    return {
        "coverage.branch_percent": _average_branch_coverage(functions),
        "complexity.max_cyclomatic": _max_attribute(functions, "complexity"),
        "complexity.max_crap": round(_max_attribute(functions, "crap_score"), 2),
    }


def _average_branch_coverage(functions: Sequence[Any]) -> float | None:
    measured: list[float] = [
        float(item.branch_coverage_percent)
        for item in functions
        if item.branch_coverage_measured
    ]
    if not measured:
        return None
    return round(sum(measured) / len(measured), 2)


def _max_attribute(items: Sequence[Any], name: str) -> Any:
    return max(getattr(item, name) for item in items)


def _test_facts(analysis: Any) -> dict[str, Any]:
    execution = analysis.test_execution
    if not getattr(execution, "measured", False):
        return {}
    return {
        "tests.executed": execution.executed,
        "tests.pass_rate": _percentage(execution.passed, execution.executed),
        "tests.skipped": execution.skipped,
    }


def _mutation_facts(mutations: Sequence[Any]) -> dict[str, Any]:
    if not mutations:
        return {}
    killed = sum(not item.survived for item in mutations)
    return {"mutation.score": _percentage(killed, len(mutations))}


def _portable_facts(portable: Any) -> dict[str, Any]:
    if portable is None:
        return {}
    supported = _supported_portable_functions(portable.complexity)
    modules = portable.dependencies.modules
    facts = {
        "complexity.max_cognitive": _max_cognitive(supported),
        "complexity.unsupported_files": _count_portable_status(
            portable.complexity, "unsupported"
        ),
        "source.duplication_percent": portable.duplication.percentage,
        "architecture.cycles": len(portable.dependencies.cycles),
        "architecture.fan_in_out": _fan_facts(modules),
        "history.hotspots": len(portable.hotspots),
        "security.secret_findings": len(portable.secrets),
    }
    facts["architecture.max_fan_in"] = facts["architecture.fan_in_out"]["max_fan_in"]
    return facts


def _supported_portable_functions(sources: Sequence[Any]) -> list[Any]:
    return [
        item
        for source in sources
        if source.status == "supported"
        for item in source.functions
    ]


def _count_portable_status(sources: Sequence[Any], status: str) -> int:
    return sum(source.status == status for source in sources)


def _max_cognitive(functions: Sequence[Any]) -> int:
    return max((item.cognitive_complexity for item in functions), default=0)


def _fan_facts(modules: Sequence[Any]) -> dict[str, int]:
    return {
        "max_fan_in": max((item.fan_in for item in modules), default=0),
        "max_fan_out": max((item.fan_out for item in modules), default=0),
    }


def analysis_facts(analysis: Any) -> dict[str, Any]:
    facts = {
        "deployable": bool(getattr(analysis, "smoke_probes", ())),
        "product.acceptance_pass_rate": _percentage(
            sum(item.passed for item in analysis.smoke_probes),
            len(analysis.smoke_probes),
        ),
        "performance.test_suite_seconds": analysis.suite_duration_seconds,
        "tests.flaky_pass": _gate_pass(analysis, "flaky"),
        "integration.contract_pass_rate": _gate_pass(analysis, "contracts"),
        "security.known_vulnerabilities": len(analysis.vulnerabilities),
        "source.max_file_lines": max(
            (item.lines for item in analysis.files), default=0
        ),
    }
    facts.update(_test_facts(analysis))
    facts.update(_function_facts(analysis.functions))
    facts.update(_mutation_facts(analysis.mutations))
    facts.update(_portable_facts(analysis.portable_analysis))
    return {key: value for key, value in facts.items() if value is not None}


def profile_path(root: Path, config: Mapping[str, Any]) -> Path:
    section = config.get("project_quality", {})
    value = (
        section.get("profile", PROFILE_NAME)
        if isinstance(section, Mapping)
        else PROFILE_NAME
    )
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def attach_project_quality(
    analysis: Any, root: Path, config: Mapping[str, Any]
) -> None:
    profile = load_profile(profile_path(root, config))
    facts = analysis_facts(analysis)
    detected = profile.get("detected", {})
    if isinstance(detected, Mapping):
        facts.update(
            {
                key: value
                for key, value in detected.items()
                if key.startswith("has_") or key == "deployable"
            }
        )
    profile_facts = profile.get("facts", {})
    if isinstance(profile_facts, Mapping):
        facts.update(_surface_facts(profile_facts))
    dimensions = assess_quality_dimensions(profile, facts)
    analysis.project_profile = profile
    analysis.quality_dimensions = dimensions
    analysis.certifications = certification_states(dimensions, analysis.passed)


def _fact_value(facts: Mapping[str, Any], key: str) -> Any:
    value = facts.get(key)
    return value.get("value") if isinstance(value, Mapping) else value


def _surface_facts(facts: Mapping[str, Any]) -> dict[str, bool]:
    kinds = set(_fact_value(facts, "project_type") or [])
    return {
        "has_ui": "web" in kinds,
        "has_api": "api" in kinds or bool(_fact_value(facts, "api_specs")),
        "has_database": bool(_fact_value(facts, "databases")),
        "has_infrastructure": bool(_fact_value(facts, "infrastructure")),
        "deployable": bool(kinds & {"web", "api", "cli", "worker"}),
    }


def dimension_dicts(dimensions: Iterable[QualityDimension]) -> list[dict[str, Any]]:
    return [dataclasses.asdict(item) for item in dimensions]
