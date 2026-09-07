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


def scale_percentage(value: float, maximum: float) -> float:
    return max(0.0, min(100.0, 100 * value / maximum))


def bullet_row(
    label: str, value: float, metric: DistributionMetric, maximum: float
) -> str:
    width = scale_percentage(value, maximum)
    limit = metric.limit
    over_limit = limit is not None and value > limit
    marker = ""
    if limit is not None:
        position = scale_percentage(limit, maximum)
        marker = (
            f'<i class="bullet-limit" style="left:{position:.5f}%" '
            f'title="Limit {number(limit, metric.unit)}" aria-hidden="true"></i>'
        )
    accessible = html.escape(f"{metric.label} {label}: {number(value, metric.unit)}")
    state = " over-limit" if over_limit else ""
    return (
        f'<div class="bullet-row{state}"><span class="bullet-label">{label}</span>'
        f'<div class="bullet-track" role="progressbar" aria-label="{accessible}" '
        f'aria-valuemin="0" aria-valuemax="{maximum:g}" aria-valuenow="{value:g}">'
        f'<span class="bullet-fill {label.lower()}" style="width:{width:.5f}%"></span>'
        f"{marker}</div><strong>{html.escape(number(value, metric.unit))}</strong></div>"
    )


def distribution_plot(metric: DistributionMetric) -> str:
    values = percentiles(metric.values)
    if not values:
        return '<div class="chart-empty">No measurements in this run</div>'
    maximum = scale_maximum(metric)
    rows = "".join(
        bullet_row(label, value, metric, maximum)
        for label, value in zip(("P50", "P75", "P95"), values, strict=True)
    )
    rows += bullet_row("MAX", max(metric.values), metric, maximum)
    return (
        f'<div class="bullet-chart" role="group" aria-label="{html.escape(metric.label)} distribution">'
        f'{rows}<div class="bullet-axis"><span>0</span><span>{html.escape(number(maximum, metric.unit))}</span></div></div>'
    )


def distribution_card(metric: DistributionMetric) -> str:
    status = metric_status(metric)
    label = html.escape(metric.label)
    limit = (
        "No configured limit"
        if metric.limit is None
        else f"Limit ≤ {number(metric.limit, metric.unit)}"
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
        f'<p class="distribution-sample">{html.escape(summary)}</p></article>'
    )


def function_distributions(
    functions: Sequence[Any], metrics: dict[str, Any]
) -> list[DistributionMetric]:
    covered = [item for item in functions if item.coverage_measured]
    return [
        DistributionMetric(
            "CRAP",
            tuple(item.crap_score for item in covered),
            metrics["crap_limit"],
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
    minimum = min(values)
    passed = sum(value >= limit for value in values)
    total = len(values) + missing
    status = "FAIL" if minimum < limit else ("PARTIAL" if missing else "PASS")
    measured = f"{passed}/{total} at target"
    if missing:
        measured += f" · {missing} not measured"
    return (
        f'<article class="coverage-card"><div><h3>{label}</h3>'
        f'<span class="metric-status {status.lower()}">{status}</span></div>'
        f"<strong>{minimum:.1f}%</strong><span>Minimum · target ≥ {limit:g}%</span>"
        f'<p class="coverage-count">{measured}</p>'
        f'<div class="coverage-track" role="progressbar" aria-label="{label} minimum" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{minimum:g}">'
        f'<span class="{status.lower()}" style="width:{minimum:g}%"></span></div></article>'
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


def health_overview(
    report: Any, thresholds: dict[str, Any], project_evidence: str = ""
) -> str:
    charts = "".join(
        distribution_card(metric) for metric in distributions(report, thresholds)
    )
    return (
        '<section class="health-overview" aria-labelledby="health-title">'
        '<div class="health-heading"><div><h2 id="health-title">Health overview</h2>'
        f"<p>{len(report.functions)} functions · {len(report.files)} files · {len(report.test_timings)} timed tests</p></div></div>"
        f"{coverage_overview(report, thresholds)}{project_evidence}"
        f'<div class="distribution-grid">{charts}</div>'
        '<p class="distribution-note">Each chart uses its own linear scale. Lower is better. The maximum is shown so an outlier cannot hide behind a passing percentile. Function LOC is informational.</p></section>'
    )


CHART_STYLES = """
.health-overview{margin:18px 0 20px;--p50:#4b5563;--p75:#007f83;--p95:#0867d8;--max:#5b3db7}
.health-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.health-heading h2{margin:0 0 4px;font-size:20px;letter-spacing:-.025em}
.health-heading p,.distribution-note{margin:0;color:var(--secondary);font-size:13px}
.distribution-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.distribution-card{min-width:0;padding:13px;background:var(--card);border:1px solid var(--line);border-radius:13px}
.distribution-heading{display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap}
.distribution-heading h3,.coverage-card h3{margin:0;font-size:14px;font-weight:650;letter-spacing:-.015em}
.metric-status{display:inline-block;padding:3px 7px;border:1px solid var(--line);border-radius:999px;font-size:12px;line-height:1.35;font-weight:700;color:var(--secondary);white-space:nowrap}
.metric-status.pass{color:var(--good);background:var(--good-soft);border-color:#b7dec9}
.metric-status.fail{color:var(--bad);background:var(--bad-soft);border-color:#f2c0bc}
.metric-status.partial{color:var(--warning);background:var(--warning-soft);border-color:#e5d3a7}
.distribution-limit{margin:5px 0 12px;font-size:12px;color:var(--secondary)}
.bullet-chart{display:grid;gap:7px;margin-top:10px}
.bullet-row{display:grid;grid-template-columns:30px minmax(56px,1fr) auto;align-items:center;gap:7px}
.bullet-label{font-size:12px;font-weight:750;color:var(--secondary)}
.bullet-row strong{min-width:34px;text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
.bullet-track{position:relative;height:8px;border-radius:99px;background:var(--track);overflow:visible}
.bullet-fill{display:block;height:100%;min-width:2px;border-radius:99px;background:var(--p50)}
.bullet-fill.p75{background:var(--p75)}.bullet-fill.p95{background:var(--p95)}.bullet-fill.max{background:var(--max)}
.bullet-row.over-limit .bullet-fill{background:var(--bad)}.bullet-row.over-limit strong{color:var(--bad)}
.bullet-limit{position:absolute;top:-3px;width:2px;height:14px;border-radius:1px;background:var(--ink);transform:translateX(-1px)}
.bullet-axis{display:flex;justify-content:space-between;margin-left:37px;color:var(--secondary);font-size:12px;font-variant-numeric:tabular-nums}
.distribution-sample{font-size:12px;line-height:1.4;color:var(--secondary);margin:10px 0 0}
.distribution-note{margin-top:8px;font-size:12px;line-height:1.5}
.chart-empty{display:grid;place-items:center;min-height:116px;color:var(--secondary);font-size:13px;text-align:center}
.coverage-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px}
.coverage-card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:13px}
.coverage-card>div:first-child{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.coverage-card>strong{display:inline-block;font-size:24px;font-weight:650;letter-spacing:-.04em;margin:7px 10px 5px 0}
.coverage-card>span{font-size:12px;color:var(--secondary)}
.coverage-count{margin:4px 0 9px;color:var(--secondary);font-size:12px}
.coverage-track{height:7px;border-radius:5px;background:var(--track);overflow:hidden}
.coverage-track>span{display:block;height:100%;background:#9299a3}
.coverage-track>.pass{background:var(--good)}.coverage-track>.fail{background:var(--bad)}
.gate-summary{display:flex;align-items:center;gap:12px;margin:8px 0 16px;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:var(--card)}
.gate-score{display:grid;min-width:78px;padding-right:14px;border-right:1px solid var(--line)}
.gate-score strong{font-size:21px;line-height:1.1;font-variant-numeric:tabular-nums}.gate-score span{font-size:12px;color:var(--secondary)}
.gate-score.pass strong{color:var(--good)}.gate-score.fail strong{color:var(--bad)}
.gate-summary p{margin:3px 0;color:var(--secondary);font-size:13px}
@media(max-width:800px){.distribution-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.health-heading{align-items:flex-start;flex-direction:column}.coverage-overview{grid-template-columns:1fr}.coverage-card{padding:9px 12px}}
@media(max-width:580px){.distribution-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.distribution-card{padding:10px 9px 8px}}
@media(max-width:390px){.distribution-grid{grid-template-columns:1fr}.gate-summary{align-items:flex-start}.gate-score{min-width:66px}}
@media(prefers-contrast:more){.distribution-card,.coverage-card{border-color:currentColor}.bullet-limit{width:3px}}
@media print{.distribution-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.distribution-card,.coverage-card{break-inside:avoid}.bullet-fill,.coverage-track>span{print-color-adjust:exact}}
"""
