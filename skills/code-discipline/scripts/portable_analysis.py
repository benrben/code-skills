"""Portable, source-only measurements with explicit evidence boundaries."""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from portable_graph import (
    DependencyReport,
    HotspotMetric,
    SecretFinding,
    analyze_dependency_graph,
    calculate_hotspots,
    scan_secrets,
)
from portable_graph import HistoryInput as HistoryInput
from portable_graph import ResolvedDependency as ResolvedDependency


@dataclass(frozen=True, slots=True)
class FunctionComplexity:
    path: str
    name: str
    line: int
    end_line: int
    cognitive_complexity: int
    max_nesting: int


@dataclass(frozen=True, slots=True)
class SourceComplexity:
    path: str
    language: str
    status: str
    functions: tuple[FunctionComplexity, ...]
    message: str = ""


@dataclass(frozen=True, slots=True)
class DuplicateLocation:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    first: DuplicateLocation
    second: DuplicateLocation
    line_count: int


@dataclass(frozen=True, slots=True)
class FileDuplication:
    path: str
    code_lines: int
    duplicated_lines: int
    percentage: float


@dataclass(frozen=True, slots=True)
class DuplicationReport:
    pairs: tuple[DuplicatePair, ...]
    files: tuple[FileDuplication, ...]
    total_code_lines: int
    duplicated_lines: int
    percentage: float
    total_pair_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class PortableAnalysisReport:
    complexity: tuple[SourceComplexity, ...]
    duplication: DuplicationReport
    dependencies: DependencyReport
    secrets: tuple[SecretFinding, ...]
    hotspots: tuple[HotspotMetric, ...]


class _CognitiveVisitor(ast.NodeVisitor):
    def __init__(self, function_name: str) -> None:
        self.function_name = function_name
        self.score = 0
        self.nesting = 0
        self.max_nesting = 0

    def _control(self) -> None:
        self.score += 1 + self.nesting
        self.max_nesting = max(self.max_nesting, self.nesting + 1)

    def _visit_nested(self, nodes: Iterable[ast.AST]) -> None:
        self.nesting += 1
        try:
            for node in nodes:
                self.visit(node)
        finally:
            self.nesting -= 1

    def _ignore_nested_scope(self, node: ast.AST) -> None:
        return

    visit_FunctionDef = _ignore_nested_scope
    visit_AsyncFunctionDef = _ignore_nested_scope
    visit_Lambda = _ignore_nested_scope
    visit_ClassDef = _ignore_nested_scope

    def visit_If(self, node: ast.If) -> None:
        self._visit_if(node)

    def _visit_if(self, node: ast.If) -> None:
        self._control()
        self.visit(node.test)
        self._visit_nested(node.body)
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            self._visit_if(node.orelse[0])
        else:
            self._visit_nested(node.orelse)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._control()
        self.visit(node.test)
        self._visit_nested((node.body, node.orelse))

    def _visit_loop(self, node: ast.AST) -> None:
        self._control()
        for field in ("target", "iter", "test"):
            value = getattr(node, field, None)
            if isinstance(value, ast.AST):
                self.visit(value)
        self._visit_nested(getattr(node, "body", ()))
        self._visit_nested(getattr(node, "orelse", ()))

    visit_For = _visit_loop
    visit_AsyncFor = _visit_loop
    visit_While = _visit_loop

    def visit_Try(self, node: ast.Try) -> None:
        for statement in node.body:
            self.visit(statement)
        for handler in node.handlers:
            self._control()
            if handler.type is not None:
                self.visit(handler.type)
            self._visit_nested(handler.body)
        for statement in itertools.chain(node.orelse, node.finalbody):
            self.visit(statement)

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self._control()
            if case.guard is not None:
                self.visit(case.guard)
            self._visit_nested(case.body)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.score += 1
        self.generic_visit(node)

    def visit_Break(self, node: ast.Break) -> None:
        self.score += 1

    def visit_Continue(self, node: ast.Continue) -> None:
        self.score += 1

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == self.function_name:
            self.score += 1
        self.generic_visit(node)

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
        result_nodes: Iterable[ast.AST],
    ) -> None:
        saved_nesting = self.nesting
        try:
            for generator in node.generators:
                self._control()
                self.visit(generator.iter)
                self.nesting += 1
                self.max_nesting = max(self.max_nesting, self.nesting)
                self.visit(generator.target)
                for condition in generator.ifs:
                    self._control()
                    self.visit(condition)
            for result_node in result_nodes:
                self.visit(result_node)
        finally:
            self.nesting = saved_nesting

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, (node.key, node.value))


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.functions: list[FunctionComplexity] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._collect(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._collect(node)

    def _collect(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        visitor = _CognitiveVisitor(node.name)
        for statement in node.body:
            visitor.visit(statement)
        qualified_name = ".".join((*self.scope, node.name))
        self.functions.append(
            FunctionComplexity(
                path=self.path,
                name=qualified_name,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                cognitive_complexity=visitor.score,
                max_nesting=visitor.max_nesting,
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def analyze_python_complexity(path: str, source: str) -> SourceComplexity:
    """Measure Python functions, or return explicit parse-error evidence."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        location = f"line {error.lineno or '?'}"
        if error.offset is not None:
            location += f", column {error.offset}"
        return SourceComplexity(path, "python", "parse_error", (), location)
    collector = _FunctionCollector(path)
    collector.visit(tree)
    return SourceComplexity(path, "python", "supported", tuple(collector.functions))


def analyze_source_complexity(
    path: str, source: str, language: str
) -> SourceComplexity:
    """Dispatch validated parsers; unsupported syntax is never approximated."""
    normalized = language.strip().lower()
    if normalized in {"python", "py"}:
        return analyze_python_complexity(path, source)
    label = normalized or "unknown"
    return SourceComplexity(
        path, label, "unsupported", (), f"cognitive complexity unsupported for {label}"
    )


_COMMENT_PREFIXES = ("#", "//", "/*", "* ", "*/", "--")


def _normalized_code_lines(source: str) -> tuple[Optional[str], ...]:
    normalized: list[Optional[str]] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        normalized.append(
            line if line and not line.startswith(_COMMENT_PREFIXES) else None
        )
    return tuple(normalized)


def _matching_run(
    first_lines: tuple[Optional[str], ...],
    second_lines: tuple[Optional[str], ...],
    first_index: int,
    second_index: int,
    step: int,
) -> int:
    length = 0
    while 0 <= first_index < len(first_lines) and 0 <= second_index < len(second_lines):
        first = first_lines[first_index]
        if first is None or first != second_lines[second_index]:
            break
        length += 1
        first_index += step
        second_index += step
    return length


def _ranges_overlap(first_start: int, second_start: int, length: int) -> bool:
    first_range = range(first_start, first_start + length)
    second_range = range(second_start, second_start + length)
    return (
        first_range.start < second_range.stop and second_range.start < first_range.stop
    )


def _maximal_duplicate(
    first_path: str,
    first_start: int,
    second_path: str,
    second_start: int,
    normalized: Mapping[str, tuple[Optional[str], ...]],
    minimum_lines: int,
) -> Optional[tuple[str, int, int, str, int, int]]:
    first_lines = normalized[first_path]
    second_lines = normalized[second_path]
    left = _matching_run(
        first_lines, second_lines, first_start - 1, second_start - 1, -1
    )
    right = minimum_lines + _matching_run(
        first_lines,
        second_lines,
        first_start + minimum_lines,
        second_start + minimum_lines,
        1,
    )
    first_start -= left
    second_start -= left
    length = left + right
    if first_path == second_path and _ranges_overlap(first_start, second_start, length):
        return None
    locations = sorted(
        (
            (first_path, first_start, first_start + length),
            (second_path, second_start, second_start + length),
        )
    )
    return (*locations[0], *locations[1])


def _duplicate_windows(
    normalized: Mapping[str, tuple[Optional[str], ...]], minimum_lines: int
) -> dict[tuple[str, ...], list[tuple[str, int]]]:
    windows: dict[tuple[str, ...], list[tuple[str, int]]] = {}
    for path in sorted(normalized):
        for key, location in _windows_for_file(path, normalized[path], minimum_lines):
            windows.setdefault(key, []).append(location)
    return windows


def _windows_for_file(
    path: str, lines: tuple[Optional[str], ...], minimum_lines: int
) -> list[tuple[tuple[str, ...], tuple[str, int]]]:
    result: list[tuple[tuple[str, ...], tuple[str, int]]] = []
    for start in range(len(lines) - minimum_lines + 1):
        window = lines[start : start + minimum_lines]
        if all(line is not None for line in window):
            key = tuple(line for line in window if line is not None)
            result.append((key, (path, start)))
    return result


def _duplicate_candidates(
    windows: Mapping[tuple[str, ...], list[tuple[str, int]]],
    normalized: Mapping[str, tuple[Optional[str], ...]],
    minimum_lines: int,
) -> set[tuple[str, int, int, str, int, int]]:
    candidates: set[tuple[str, int, int, str, int, int]] = set()
    for locations in windows.values():
        if len(locations) < 2:
            continue
        first = locations[0]
        for second in locations[1:]:
            candidate = _maximal_duplicate(*first, *second, normalized, minimum_lines)
            if candidate is not None:
                candidates.add(candidate)
    return candidates


def _contains_duplicate(
    kept: tuple[str, int, int, str, int, int],
    candidate: tuple[str, int, int, str, int, int],
) -> bool:
    same_files = (candidate[0], candidate[3]) == (kept[0], kept[3])
    first_inside = kept[1] <= candidate[1] <= candidate[2] <= kept[2]
    second_inside = kept[4] <= candidate[4] <= candidate[5] <= kept[5]
    return same_files and first_inside and second_inside


def _maximal_candidates(
    candidates: set[tuple[str, int, int, str, int, int]],
) -> list[tuple[str, int, int, str, int, int]]:
    ordered = sorted(candidates, key=lambda item: (-(item[2] - item[1]), item))
    maximal: list[tuple[str, int, int, str, int, int]] = []
    for candidate in ordered:
        if not any(_contains_duplicate(kept, candidate) for kept in maximal):
            maximal.append(candidate)
    return maximal


def _duplicated_by_path(
    sources: Mapping[str, str],
    maximal: Iterable[tuple[str, int, int, str, int, int]],
) -> dict[str, set[int]]:
    duplicated: dict[str, set[int]] = {path: set() for path in sources}
    for (
        first_path,
        first_start,
        first_end,
        second_path,
        second_start,
        second_end,
    ) in maximal:
        duplicated[first_path].update(range(first_start, first_end))
        duplicated[second_path].update(range(second_start, second_end))
    return duplicated


def _duplicate_pairs(
    maximal: list[tuple[str, int, int, str, int, int]], maximum_pairs: int
) -> tuple[DuplicatePair, ...]:
    return tuple(
        DuplicatePair(
            DuplicateLocation(item[0], item[1] + 1, item[2]),
            DuplicateLocation(item[3], item[4] + 1, item[5]),
            item[2] - item[1],
        )
        for item in maximal[:maximum_pairs]
    )


def _file_duplication(
    path: str,
    normalized: Mapping[str, tuple[Optional[str], ...]],
    duplicated_by_path: Mapping[str, set[int]],
) -> FileDuplication:
    code_lines = sum(line is not None for line in normalized[path])
    duplicated_lines = len(duplicated_by_path[path])
    return FileDuplication(
        path, code_lines, duplicated_lines, _percentage(duplicated_lines, code_lines)
    )


def _percentage(part: int, total: int) -> float:
    return part / total * 100 if total else 0.0


def _build_duplication_report(
    maximal: list[tuple[str, int, int, str, int, int]],
    files: list[FileDuplication],
    maximum_pairs: int,
) -> DuplicationReport:
    total_code_lines = sum(item.code_lines for item in files)
    duplicated_lines = sum(item.duplicated_lines for item in files)
    return DuplicationReport(
        _duplicate_pairs(maximal, maximum_pairs),
        tuple(files),
        total_code_lines,
        duplicated_lines,
        _percentage(duplicated_lines, total_code_lines),
        len(maximal),
        len(maximal) > maximum_pairs,
    )


def find_duplicate_blocks(
    sources: Mapping[str, str], minimum_lines: int = 6, maximum_pairs: int = 100
) -> DuplicationReport:
    """Find exact normalized, contiguous code blocks and report both locations."""
    if minimum_lines < 2:
        raise ValueError("minimum_lines must be at least 2")
    if maximum_pairs < 1:
        raise ValueError("maximum_pairs must be positive")
    normalized = {
        path: _normalized_code_lines(source) for path, source in sources.items()
    }
    candidates = _duplicate_candidates(
        _duplicate_windows(normalized, minimum_lines), normalized, minimum_lines
    )
    maximal = _maximal_candidates(candidates)
    duplicated_by_path = _duplicated_by_path(sources, maximal)
    files = [
        _file_duplication(path, normalized, duplicated_by_path)
        for path in sorted(sources)
    ]
    return _build_duplication_report(maximal, files, maximum_pairs)


def _inferred_language(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"py": "python"}.get(suffix, suffix or "unknown")


def analyze_portable_sources(
    sources: Mapping[str, str],
    languages: Optional[Mapping[str, str]] = None,
    resolved_edges: Iterable[ResolvedDependency] = (),
    history: Iterable[HistoryInput] = (),
) -> PortableAnalysisReport:
    """Run every source-only analyzer without retaining source text in the report."""
    complexity = tuple(
        analyze_source_complexity(
            path,
            sources[path],
            (languages or {}).get(path, _inferred_language(path)),
        )
        for path in sorted(sources)
    )
    complexity_by_path = {
        item.path: float(
            sum(function.cognitive_complexity for function in item.functions)
        )
        for item in complexity
        if item.status == "supported"
    }
    return PortableAnalysisReport(
        complexity=complexity,
        duplication=find_duplicate_blocks(sources),
        dependencies=analyze_dependency_graph(sources, resolved_edges),
        secrets=scan_secrets(sources),
        hotspots=calculate_hotspots(history, complexity_by_path),
    )
