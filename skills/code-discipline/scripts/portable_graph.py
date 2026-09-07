"""Portable dependency, secret, and history measurements."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class ModuleDependencyMetric:
    module: str
    fan_in: int
    fan_out: int


@dataclass(frozen=True, slots=True)
class DependencyCycle:
    members: tuple[str, ...]
    example_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DependencyReport:
    modules: tuple[ModuleDependencyMetric, ...]
    cycles: tuple[DependencyCycle, ...]
    internal_edges: int
    ignored_external_edges: int


@dataclass(frozen=True, slots=True)
class SecretFinding:
    path: str
    line: int
    column: int
    kind: str


@dataclass(frozen=True, slots=True)
class HistoryInput:
    path: str
    commit_count: int
    lines_added: int
    lines_deleted: int


@dataclass(frozen=True, slots=True)
class HotspotMetric:
    path: str
    commit_count: int
    churn: int
    complexity: Optional[float]
    score: Optional[float]
    status: str


def _finish_walk(
    start: str,
    graph: Mapping[str, set[str]],
    visited: set[str],
    finish_order: list[str],
) -> None:
    traversal_stack = [(start, False)]
    while traversal_stack:
        node, expanded = traversal_stack.pop()
        if expanded:
            finish_order.append(node)
        elif node not in visited:
            visited.add(node)
            traversal_stack.append((node, True))
            traversal_stack.extend(
                (target, False)
                for target in sorted(graph[node], reverse=True)
                if target not in visited
            )


def _finish_order(graph: Mapping[str, set[str]]) -> list[str]:
    visited: set[str] = set()
    result: list[str] = []
    for start in sorted(graph):
        if start not in visited:
            _finish_walk(start, graph, visited, result)
    return result


def _reverse_graph(graph: Mapping[str, set[str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {node: set() for node in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    return reverse


def _collect_component(
    start: str, reverse: Mapping[str, set[str]], visited: set[str]
) -> tuple[str, ...]:
    members: list[str] = []
    component_stack = [start]
    visited.add(start)
    while component_stack:
        node = component_stack.pop()
        members.append(node)
        for target in reverse[node]:
            if target not in visited:
                visited.add(target)
                component_stack.append(target)
    return tuple(sorted(members))


def _strongly_connected_components(
    graph: Mapping[str, set[str]],
) -> list[tuple[str, ...]]:
    reverse = _reverse_graph(graph)
    components: list[tuple[str, ...]] = []
    visited: set[str] = set()
    for start in reversed(_finish_order(graph)):
        if start not in visited:
            components.append(_collect_component(start, reverse, visited))
    return components


def _cycle_path(
    start: str, node: str, parents: Mapping[str, Optional[str]]
) -> tuple[str, ...]:
    path = [node]
    parent = parents[node]
    while parent is not None:
        path.append(parent)
        parent = parents[parent]
    return (start, *reversed(path), start)


def _queue_cycle_targets(
    graph: Mapping[str, set[str]],
    node: str,
    start: str,
    allowed: set[str],
    parents: dict[str, Optional[str]],
    pending: deque[str],
) -> None:
    for target in sorted((graph[node] & allowed) - {start}):
        if target not in parents:
            parents[target] = node
            pending.append(target)


def _example_cycle(
    graph: Mapping[str, set[str]], members: tuple[str, ...]
) -> tuple[str, ...]:
    if len(members) == 1:
        return (members[0], members[0])
    allowed = set(members)
    start = members[0]
    first = next(target for target in sorted(graph[start] & allowed) if target != start)
    pending = deque([first])
    parents: dict[str, Optional[str]] = {first: None}
    while pending:
        node = pending.popleft()
        if start in graph[node]:
            return _cycle_path(start, node, parents)
        _queue_cycle_targets(graph, node, start, allowed, parents, pending)
    raise ValueError("strongly connected component did not contain a cycle")


def _internal_graph(
    module_set: set[str], edges: Iterable[ResolvedDependency]
) -> tuple[dict[str, set[str]], int]:
    graph: dict[str, set[str]] = {module: set() for module in module_set}
    ignored = 0
    for edge in sorted(set(edges), key=lambda item: (item.source, item.target)):
        if edge.source in module_set and edge.target in module_set:
            graph[edge.source].add(edge.target)
        else:
            ignored += 1
    return graph, ignored


def _incoming_edges(graph: Mapping[str, set[str]]) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = {module: set() for module in graph}
    for source, targets in graph.items():
        for target in targets:
            incoming[target].add(source)
    return incoming


def _dependency_cycles(graph: Mapping[str, set[str]]) -> tuple[DependencyCycle, ...]:
    cycles = []
    for members in _strongly_connected_components(graph):
        if len(members) > 1 or members[0] in graph[members[0]]:
            cycles.append(DependencyCycle(members, _example_cycle(graph, members)))
    return tuple(sorted(cycles, key=lambda item: item.members))


def analyze_dependency_graph(
    modules: Iterable[str], edges: Iterable[ResolvedDependency]
) -> DependencyReport:
    """Analyze caller-resolved edges between explicitly supplied internal modules."""
    module_set = set(modules)
    graph, ignored = _internal_graph(module_set, edges)
    incoming = _incoming_edges(graph)
    metrics = tuple(
        ModuleDependencyMetric(module, len(incoming[module]), len(graph[module]))
        for module in sorted(module_set)
    )
    return DependencyReport(
        metrics,
        _dependency_cycles(graph),
        sum(len(targets) for targets in graph.values()),
        ignored,
    )


_SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "aws_access_key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("stripe_live_secret", re.compile(r"(?<![A-Za-z0-9])sk_live_[A-Za-z0-9]{16,}")),
)


def scan_secrets(sources: Mapping[str, str]) -> tuple[SecretFinding, ...]:
    """Return locations and kinds only; matched values never enter result objects."""
    findings: list[SecretFinding] = []
    for path in sorted(sources):
        source = sources[path]
        line_starts = [0]
        line_starts.extend(match.end() for match in re.finditer("\n", source))
        for kind, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(source):
                line_index = bisect_right(line_starts, match.start()) - 1
                findings.append(
                    SecretFinding(
                        path=path,
                        line=line_index + 1,
                        column=match.start() - line_starts[line_index] + 1,
                        kind=kind,
                    )
                )
    return tuple(
        sorted(
            set(findings),
            key=lambda item: (item.path, item.line, item.column, item.kind),
        )
    )


def _hotspot(
    item: HistoryInput, complexity_by_path: Mapping[str, float]
) -> HotspotMetric:
    if min(item.commit_count, item.lines_added, item.lines_deleted) < 0:
        raise ValueError("history counts must be non-negative")
    complexity = complexity_by_path.get(item.path)
    if complexity is not None and complexity < 0:
        raise ValueError("complexity must be non-negative")
    churn = item.lines_added + item.lines_deleted
    status = "measured" if complexity is not None else "missing_complexity"
    score = (
        float(item.commit_count * (1 + complexity)) if complexity is not None else None
    )
    return HotspotMetric(item.path, item.commit_count, churn, complexity, score, status)


def calculate_hotspots(
    history: Iterable[HistoryInput], complexity_by_path: Mapping[str, float]
) -> tuple[HotspotMetric, ...]:
    """Rank caller-supplied history by commits * (1 + complexity), without I/O."""
    results: list[HotspotMetric] = []
    seen: set[str] = set()
    for item in history:
        if item.path in seen:
            raise ValueError(f"duplicate history path: {item.path}")
        seen.add(item.path)
        results.append(_hotspot(item, complexity_by_path))
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.score is None,
                -(item.score or 0),
                -item.commit_count,
                -item.churn,
                item.path,
            ),
        )
    )
