"""Self-contained report charts: measured percentiles on a shared linear scale."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class DistributionMetric:
    label: str
    values: tuple[float, ...]
    limit: float | None
    unit: str = ""
    missing: int = 0


def percentiles(values: Sequence[float]) -> tuple[float, ...]:
    """Linear interpolation, including singleton samples without extrapolation."""
    ordered = sorted(values)
    if not ordered:
        return ()
    result = []
    for fraction in (0.5, 0.75, 0.95):
        position = (len(ordered) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        result.append(
            ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        )
    return tuple(result)


def scale_maximum(metric: DistributionMetric) -> float:
    ceiling = max(1, max(metric.values, default=0) * 1.1, (metric.limit or 0) * 1.25)
    magnitude = 10 ** math.floor(math.log10(ceiling))
    return float(math.ceil(ceiling / magnitude) * magnitude)


def metric_status(metric: DistributionMetric) -> str:
    if not metric.values:
        return "NOT MEASURED"
    if metric.limit is not None and max(metric.values) > metric.limit:
        return "FAIL"
    if metric.missing:
        return "PARTIAL"
    return "INFO" if metric.limit is None else "PASS"


def number(value: float, unit: str = "") -> str:
    return f"{value:.3g}{unit}"


def semicircle(radius: int) -> str:
    return f"M {132 - radius} 156 A {radius} {radius} 0 0 1 {132 + radius} 156"


def limit_marker(limit: float, maximum: float) -> str:
    angle = math.pi * (1 - limit / maximum)
    x1, y1 = 132 + 50 * math.cos(angle), 156 - 50 * math.sin(angle)
    x2, y2 = 132 + 116 * math.cos(angle), 156 - 116 * math.sin(angle)
    return (
        f'<line class="arc-limit" x1="{x1:.3f}" y1="{y1:.3f}" '
        f'x2="{x2:.3f}" y2="{y2:.3f}" aria-hidden="true" />'
    )


def percentile_arc(
    label: str, value: float, radius: int, metric: DistributionMetric
) -> str:
    maximum = scale_maximum(metric)
    length = 100 * value / maximum
    path = semicircle(radius)
    cap = "butt" if value == 0 else "round"
    accessible = html.escape(f"{metric.label} {label}: {number(value, metric.unit)}")
    arc = (
        f'<g class="arc-series {label.lower()}"><path class="arc-track" d="{path}" />'
        f'<path class="percentile-arc" data-percentile="{label}" d="{path}" '
        f'pathLength="100" stroke-dasharray="{length:.5f} 100" stroke-linecap="{cap}" '
        f'role="progressbar" aria-label="{accessible}" aria-valuemin="0" '
        f'aria-valuemax="{maximum:g}" aria-valuenow="{value:g}" />'
    )
    if metric.limit is not None and value > metric.limit:
        start = 100 * metric.limit / maximum
        arc += (
            f'<path class="arc-over-limit" d="{path}" pathLength="100" '
            f'stroke-dasharray="{length - start:.5f} 100" '
            f'stroke-dashoffset="{-start:.5f}" aria-hidden="true" />'
        )
    return arc + "</g>"


def distribution_plot(metric: DistributionMetric) -> str:
    values = percentiles(metric.values)
    if not values:
        return '<div class="arc-empty">No measurements in this run</div>'
    maximum = scale_maximum(metric)
    arcs = "".join(
        percentile_arc(label, value, radius, metric)
        for label, value, radius in zip(
            ("P50", "P75", "P95"), values, (62, 83, 104), strict=True
        )
    )
    marker = "" if metric.limit is None else limit_marker(metric.limit, maximum)
    axis_max = html.escape(number(maximum, metric.unit))
    return (
        f'<svg class="nested-arcs" viewBox="16 42 232 148" aria-label="{html.escape(metric.label)} distribution">'
        f'{arcs}{marker}<text x="22" y="182">0</text>'
        f'<text x="242" y="182" text-anchor="end">{axis_max}</text></svg>'
    )


def distribution_card(metric: DistributionMetric) -> str:
    status = metric_status(metric)
    label = html.escape(metric.label)
    limit = (
        "No configured limit"
        if metric.limit is None
        else f"Limit ≤ {number(metric.limit, metric.unit)}"
    )
    values = percentiles(metric.values)
    figures = "".join(
        f'<div class="percentile-value {name.lower()}"><dt>{name}</dt><dd>{html.escape(number(value, metric.unit))}</dd></div>'
        for name, value in zip(("P50", "P75", "P95"), values, strict=False)
    )
    summary = f"{len(metric.values)} measured"
    if metric.values:
        summary += f" · Max {number(max(metric.values), metric.unit)}"
    if metric.missing:
        summary += f" · {metric.missing} not measured"
    return (
        f'<article class="distribution-card" data-metric="{label}">'
        f'<div class="distribution-heading"><h3>{label}</h3>'
        f'<span class="metric-status {status.lower().replace(" ", "-")}">{status}</span></div>'
        f'<p class="distribution-limit">{html.escape(limit)}</p>{distribution_plot(metric)}'
        f'<dl class="percentile-values">{figures}</dl>'
        f'<p class="distribution-sample">{html.escape(summary)}</p></article>'
    )


def function_distributions(
    functions: Sequence[Any], metrics: dict[str, Any]
) -> list[DistributionMetric]:
    covered = [item for item in functions if item.coverage_measured]
    return [
        DistributionMetric(
            "CRAAP",
            tuple(item.craap_score for item in covered),
            metrics["craap_limit"],
            missing=len(functions) - len(covered),
        ),
        DistributionMetric(
            "Complexity",
            tuple(item.complexity for item in functions),
            metrics["complexity_limit"],
        ),
        DistributionMetric(
            "Function LOC",
            tuple(item.end_line - item.start_line + 1 for item in functions),
            None,
        ),
    ]


def distributions(report: Any, thresholds: dict[str, Any]) -> list[DistributionMetric]:
    return function_distributions(report.functions, thresholds["metrics"]) + [
        DistributionMetric(
            "File LOC",
            tuple(item.lines for item in report.files),
            thresholds["file_loc"]["max_lines"],
        ),
        DistributionMetric(
            "Test speed",
            tuple(item.duration_seconds for item in report.test_timings),
            thresholds["slow_tests"]["max_test_seconds"],
            "s",
        ),
    ]


def coverage_card(
    label: str, values: Sequence[float], limit: float, missing: int
) -> str:
    if not values:
        return f'<article class="coverage-card"><h3>{label}</h3><strong>—</strong><span>Not measured</span></article>'
    average = sum(values) / len(values)
    status = "FAIL" if min(values) < limit else ("PARTIAL" if missing else "PASS")
    return (
        f'<article class="coverage-card"><div><h3>{label}</h3>'
        f'<span class="metric-status {status.lower()}">{status}</span></div>'
        f"<strong>{average:.1f}%</strong><span>Target ≥ {limit:g}%</span>"
        f'<div class="coverage-track" role="progressbar" aria-label="{label}" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{average:g}">'
        f'<span class="{status.lower()}" style="width:{average:g}%"></span></div></article>'
    )


def failure_path_card(paths: Sequence[Any], limit: float) -> str:
    measured = [item for item in paths if item.coverage_measured]
    values = (
        [100 * sum(item.covered for item in measured) / len(measured)]
        if measured
        else []
    )
    return coverage_card(
        "Failure-path coverage", values, limit, len(paths) - len(measured)
    )


def coverage_overview(report: Any, thresholds: dict[str, Any]) -> str:
    functions = report.functions
    lines = [item.coverage_percent for item in functions if item.coverage_measured]
    branches = [
        item.branch_coverage_percent
        for item in functions
        if item.branch_coverage_measured
    ]
    metrics = thresholds["metrics"]
    return (
        '<div class="coverage-overview">'
        + "".join(
            (
                coverage_card(
                    "Line coverage",
                    lines,
                    metrics["coverage_limit"],
                    len(functions) - len(lines),
                ),
                coverage_card(
                    "Branch coverage",
                    branches,
                    metrics["branch_coverage_limit"],
                    len(functions) - len(branches),
                ),
                failure_path_card(
                    report.error_paths,
                    thresholds["error_handling"]["failure_path_coverage_limit"],
                ),
            )
        )
        + "</div>"
    )


def health_overview(report: Any, thresholds: dict[str, Any]) -> str:
    charts = "".join(
        distribution_card(metric) for metric in distributions(report, thresholds)
    )
    return (
        '<section class="health-overview" aria-labelledby="health-title">'
        '<div class="health-heading"><div><h2 id="health-title">Health overview</h2>'
        f"<p>{len(report.functions)} functions · {len(report.files)} files · {len(report.test_timings)} timed tests</p></div>"
        '<div class="percentile-legend"><span class="p50">P50 · median</span>'
        '<span class="p75">P75</span><span class="p95">P95</span></div></div>'
        f'{coverage_overview(report, thresholds)}<div class="distribution-grid">{charts}</div>'
        '<p class="distribution-note">Lower is better. Limits apply to every measured value; '
        "a passing percentile does not hide a failing maximum. Function LOC is informational.</p></section>"
    )


CHART_STYLES = """
.health-overview{margin:8px 0 16px;--p50:#34363b;--p75:#008d91;--p95:#0867f6}
.health-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.health-heading h2{margin:0 0 4px;font-size:18px;letter-spacing:-.025em}
.health-heading p,.distribution-note{margin:0;color:var(--secondary);font-size:13px}
.percentile-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}
.p50{color:var(--p50)}.p75{color:var(--p75)}.p95{color:var(--p95)}
.percentile-legend span::before{content:"";display:inline-block;width:14px;height:4px;border-radius:3px;background:currentColor;vertical-align:middle;margin-right:6px}
.distribution-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.distribution-card{min-width:0;padding:10px 12px 8px;background:var(--card);border:1px solid var(--line);border-radius:11px}
.distribution-heading{display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap}
.distribution-heading h3,.coverage-card h3{margin:0;font-size:13px;font-weight:600;letter-spacing:-.015em}
.metric-status{display:inline-block;padding:2px 5px;border:1px solid var(--line);border-radius:6px;font-size:10px;line-height:1.4;font-weight:600;color:var(--secondary);white-space:nowrap}
.metric-status.pass{color:#13763d;background:#eef9f1;border-color:#d4ebdc}
.metric-status.fail{color:#c92230;background:#fff0f0;border-color:#ffcbd0}
.metric-status.partial{color:#875a06;background:#fff8e9;border-color:#efdfb8}
.distribution-limit{margin:4px 0 0;font-size:11px;color:var(--secondary)}
.nested-arcs{display:block;width:100%;max-width:144px;height:auto;margin:6px auto 0;overflow:visible}
.nested-arcs path{fill:none;stroke-width:8}
.arc-track{stroke:#eceef1}.percentile-arc{stroke:currentColor}
.arc-over-limit{stroke:#df3444;stroke-linecap:butt}
.arc-limit{stroke:var(--ink);stroke-width:1.2;stroke-dasharray:4 4}
.nested-arcs text{fill:var(--secondary);font:19px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.percentile-values{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:6px 0 0;padding-top:7px;border-top:1px solid var(--line);gap:5px;text-align:center}
.percentile-values dt{font-size:11px;font-weight:500;margin-bottom:2px}
.percentile-values dd{font-size:clamp(13px,1.15vw,16px);font-weight:500;letter-spacing:-.04em;margin:0;font-variant-numeric:tabular-nums}
.distribution-sample{font-size:11px;line-height:1.4;color:var(--secondary);margin:7px 0 0;text-align:center}
.distribution-note{margin-top:8px;font-size:11px;line-height:1.5}
.arc-empty{display:grid;place-items:center;height:98px;color:var(--secondary);font-size:12px;text-align:center}
.coverage-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px}
.coverage-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.coverage-card>div:first-child{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.coverage-card>strong{display:inline-block;font-size:20px;font-weight:500;letter-spacing:-.04em;margin:5px 10px 5px 0}
.coverage-card>span{font-size:11px;color:var(--secondary)}
.coverage-track{height:5px;border-radius:5px;background:#eceef1;overflow:hidden}
.coverage-track>span{display:block;height:100%;background:#9299a3}
.coverage-track>.pass{background:#1a9857}.coverage-track>.fail{background:#dc3444}
.chart-ring{width:36px;height:36px;border-radius:50%;background:conic-gradient(var(--good) var(--gate-completion),#e5e8ed 0);position:relative;flex:none}
.chart-ring::after{content:"";position:absolute;inset:4px;border-radius:50%;background:var(--card)}
.gate-summary{display:flex;align-items:center;gap:10px;margin:8px 0 12px}
.gate-summary p{margin:3px 0;color:var(--secondary);font-size:13px}
@media(max-width:800px){.distribution-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.health-heading{align-items:flex-start;flex-direction:column}.coverage-overview{grid-template-columns:1fr}.coverage-card{padding:9px 12px}}
@media(max-width:580px){.distribution-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.distribution-card{padding:10px 9px 8px}}
@media(max-width:390px){.distribution-grid{grid-template-columns:1fr}.percentile-values dd{font-size:16px}}
@media(prefers-contrast:more){.arc-track{stroke:#b5bcc6}.distribution-card,.coverage-card{border-color:currentColor}.arc-limit{stroke-width:2}}
@media print{.distribution-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.distribution-card,.coverage-card{break-inside:avoid}.nested-arcs{print-color-adjust:exact}}
"""
