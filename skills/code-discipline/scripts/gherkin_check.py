#!/usr/bin/env python3
"""Validate native BDD execution reports against official Gherkin pickles."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Step:
    line: int
    text: str
    document: str | None = None
    table: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True)
class Case:
    path: Path
    line: int
    steps: tuple[Step, ...]

    @property
    def key(self) -> tuple[Path, int]:
        return self.path, self.line


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    scenario_count: int
    step_count: int
    errors: tuple[str, ...] = ()


def object_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def list_value(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return value


def text_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def line_value(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected a positive source line")
    return value


def source_path(root: Path, value: Any) -> Path:
    name = text_value(value)
    if not name:
        raise ValueError("missing feature path")
    uri = urlparse(name)
    if uri.scheme == "file":
        name = unquote(uri.path)
    elif uri.scheme:
        raise ValueError(f"unsupported feature URI: {name}")
    return (root / name).resolve()


def location(root: Path, value: Any) -> tuple[Path, int]:
    name, separator, line = text_value(value).rpartition(":")
    if not separator or not line.isdecimal():
        raise ValueError(f"invalid source location: {value}")
    return source_path(root, name), line_value(int(line))


def ast_locations(node: Any) -> Iterator[tuple[str, int]]:
    if isinstance(node, list):
        for item in node:
            yield from ast_locations(item)
    elif isinstance(node, dict):
        yield from object_locations(node)


def object_locations(node: dict[str, Any]) -> Iterator[tuple[str, int]]:
    if "id" in node:
        yield node["id"], node["location"]["line"]
    for child in node.values():
        yield from ast_locations(child)


def scenario_ids(node: Any) -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from scenario_ids(item)
    elif isinstance(node, dict):
        for key, child in node.items():
            if key == "scenario":
                yield child["id"]
            yield from scenario_ids(child)


def require_executable_scenarios(document: Any, pickles: Sequence[Any]) -> None:
    compiled = {item["astNodeIds"][0] for item in pickles}
    if set(scenario_ids(document)) - compiled:
        raise ValueError("scenario outline has no executable Examples rows")


def runner_feature_path(path: Path) -> Path:
    return (
        path.with_suffix("")
        if path.name.endswith((".feature.yaml", ".feature.yml"))
        else path
    )


def feature_text(path: Path) -> str:
    if runner_feature_path(path) != path:
        adapter = importlib.import_module("gherkin_yaml")
        return str(adapter.render_feature(path))
    return path.read_text(encoding="utf-8")


def compile_feature(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parser_module = importlib.import_module("gherkin.parser")
    compiler_module = importlib.import_module("gherkin.pickles.compiler")
    errors = importlib.import_module("gherkin.errors")
    try:
        document = parser_module.Parser().parse(feature_text(path))
        document["uri"] = str(runner_feature_path(path))
        pickles = list(compiler_module.Compiler().compile(document))
    except errors.ParserError as error:
        raise ValueError(f"{path}: invalid Gherkin: {error}") from error
    require_executable_scenarios(document, pickles)
    return pickles, dict(ast_locations(document))


def pickle_argument(argument: dict[str, Any]) -> tuple[str | None, Any]:
    if "docString" in argument:
        return argument["docString"]["content"], None
    if "dataTable" in argument:
        rows = argument["dataTable"]["rows"]
        return None, tuple(
            tuple(cell["value"] for cell in row["cells"]) for row in rows
        )
    return None, None


def pickle_step(step: dict[str, Any], lines: dict[str, int]) -> Step:
    document, table = pickle_argument(step.get("argument", {}))
    return Step(lines[step["astNodeIds"][0]], step["text"], document, table)


def feature_cases(path: Path) -> list[Case]:
    pickles, lines = compile_feature(path)
    if not pickles:
        raise ValueError(f"{path}: no executable scenarios or Examples rows")
    cases = []
    for item in pickles:
        steps = tuple(pickle_step(step, lines) for step in item["steps"])
        if not steps:
            raise ValueError(f"{path}: scenario {item['name']!r} has no steps")
        cases.append(Case(Path(item["uri"]), lines[item["astNodeIds"][-1]], steps))
    return cases


def unique_feature_paths(root: Path, features: Sequence[Path]) -> list[Path]:
    if not features:
        raise ValueError("configure at least one .feature.yaml or .feature file")
    paths = [source_path(root, str(path)) for path in features]
    if len(paths) != len({runner_feature_path(path) for path in paths}):
        raise ValueError("duplicate feature paths in inventory")
    return paths


def expected_cases(
    root: Path, features: Sequence[Path]
) -> dict[tuple[Path, int], Case]:
    cases: dict[tuple[Path, int], Case] = {}
    for path in unique_feature_paths(root, features):
        cases.update((case.key, case) for case in feature_cases(path))
    return cases


def passed_result(value: Any, context: str) -> None:
    status = object_value(value).get("status")
    if status != "passed":
        raise ValueError(f"{context}: expected passed, received {status!r}")


def check_hooks(node: dict[str, Any], context: str) -> None:
    for key in ("before", "after", "before_all", "after_all"):
        for hook in list_value(node.get(key, [])):
            passed_result(object_value(hook).get("result"), f"{context}: {key} hook")


def check_node(node: dict[str, Any], context: str, require_status: bool) -> None:
    if require_status or "status" in node:
        passed_result(node, context)
    check_hooks(node, context)


def native_table(value: Any) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(text_value(cell) for cell in list_value(row)) for row in list_value(value)
    )


def behave_argument(step: dict[str, Any]) -> tuple[str | None, Any]:
    document = None
    table = None
    if "text" in step:
        document = text_value(step["text"])
    if "table" in step:
        data = object_value(step["table"])
        table = native_table([data.get("headings"), *list_value(data.get("rows"))])
    return document, table


def cucumber_argument(step: dict[str, Any]) -> tuple[str | None, Any]:
    document = None
    table = None
    arguments = list_value(step.get("arguments", []))
    if len(arguments) > 1:
        raise ValueError("a Gherkin step must have at most one argument")
    for argument in arguments:
        data = object_value(argument)
        if "content" in data:
            document = text_value(data["content"])
        elif "rows" in data:
            table = cucumber_table(data["rows"])
        else:
            raise ValueError("unsupported Cucumber step argument")
    return cucumber_legacy_argument(step, document, table)


def cucumber_table(rows: Any) -> tuple[tuple[str, ...], ...]:
    return native_table([object_value(row).get("cells") for row in list_value(rows)])


def cucumber_legacy_argument(
    step: dict[str, Any], document: str | None, table: Any
) -> tuple[str | None, Any]:
    if "doc_string" in step:
        document = text_value(object_value(step["doc_string"]).get("value"))
    if "rows" in step:
        table = cucumber_table(step["rows"])
    return document, table


def reported_step(
    root: Path, path: Path, step: dict[str, Any], format_name: str
) -> Step:
    if format_name == "behave-json":
        step_path, line = location(root, step.get("location"))
        if step_path != path:
            raise ValueError(f"{path}: step location points to another feature")
        document, table = behave_argument(step)
    else:
        line = line_value(step.get("line"))
        document, table = cucumber_argument(step)
    return Step(line, text_value(step.get("name")), document, table)


def reported_steps(
    root: Path, path: Path, value: Any, format_name: str
) -> tuple[Step, ...]:
    steps = []
    for item in list_value(value):
        step = object_value(item)
        passed_result(
            step.get("result"), f"{path}: step or hook {step.get('name', '')!r}"
        )
        check_hooks(step, str(path))
        if step.get("hidden") is True:
            continue
        steps.append(reported_step(root, path, step, format_name))
    return tuple(steps)


def reported_case(
    root: Path,
    path: Path,
    item: dict[str, Any],
    format_name: str,
    background: tuple[Step, ...],
) -> Case:
    check_node(
        item, f"{path}: scenario {item.get('name', '')!r}", format_name == "behave-json"
    )
    if format_name == "behave-json":
        case_path, line = location(root, item.get("location"))
        if case_path != path:
            raise ValueError(f"{path}: scenario location points to another feature")
    else:
        line = line_value(item.get("line"))
    return Case(
        path,
        line,
        background + reported_steps(root, path, item.get("steps"), format_name),
    )


def feature_report(
    root: Path, feature: dict[str, Any], format_name: str
) -> tuple[Path, list[Case]]:
    if format_name == "behave-json":
        path, _ = location(root, feature.get("location"))
    else:
        path = source_path(root, feature.get("uri"))
    check_node(feature, str(path), format_name == "behave-json")
    cases = []
    background: tuple[Step, ...] = ()
    for value in list_value(feature.get("elements")):
        item = object_value(value)
        if item.get("type") == "background":
            background = report_background(root, path, item, format_name)
        elif item.get("type") == "scenario":
            cases.append(reported_case(root, path, item, format_name, background))
            background = ()
        else:
            raise ValueError(
                f"{path}: unsupported report element type {item.get('type')!r}"
            )
    return path, cases


def report_background(
    root: Path, path: Path, item: dict[str, Any], format_name: str
) -> tuple[Step, ...]:
    check_node(item, f"{path}: background", False)
    if format_name == "behave-json":
        return ()  # Behave declarations have no results; each scenario repeats these steps.
    return reported_steps(root, path, item.get("steps"), format_name)


def compare_cases(
    expected: dict[tuple[Path, int], Case], actual: Sequence[Case]
) -> None:
    seen = set()
    for case in actual:
        label = f"{case.path}:{case.line}"
        if case.key in seen:
            raise ValueError(f"{label}: duplicate scenario execution")
        seen.add(case.key)
        compare_case(expected, case)
    missing = set(expected) - seen
    if missing:
        labels = ", ".join(f"{path}:{line}" for path, line in sorted(missing))
        raise ValueError(f"missing scenario executions: {labels}")


def compare_case(expected: dict[tuple[Path, int], Case], case: Case) -> None:
    label = f"{case.path}:{case.line}"
    if case.key not in expected:
        raise ValueError(f"{label}: unexpected scenario execution")
    if case.steps != expected[case.key].steps:
        raise ValueError(
            f"{label}: executed steps, locations, or arguments differ from Gherkin"
        )


def read_report(
    root: Path, report: Path, format_name: str, paths: set[Path]
) -> list[Case]:
    data = json.loads((root / report).read_text(encoding="utf-8"))
    cases = []
    seen = set()
    for value in list_value(data):
        path, feature = feature_report(root, object_value(value), format_name)
        if path not in paths:
            raise ValueError(f"unexpected feature report: {path}")
        if path in seen:
            raise ValueError(f"duplicate feature report: {path}")
        seen.add(path)
        cases.extend(feature)
    return cases


def validate(
    root: Path, report_path: Path, format_name: str, feature_paths: Sequence[Path]
) -> ValidationResult:
    try:
        if format_name not in ("behave-json", "cucumber-json"):
            raise ValueError(f"unsupported report format: {format_name}")
        expected = expected_cases(root, feature_paths)
        actual = read_report(
            root, report_path, format_name, {case.path for case in expected.values()}
        )
        compare_cases(expected, actual)
    except ImportError as error:
        return ValidationResult(
            False, 0, 0, (f"install gherkin-official and PyYAML: {error}",)
        )
    except (OSError, UnicodeError, ValueError) as error:
        return ValidationResult(False, 0, 0, (str(error),))
    return ValidationResult(True, len(actual), sum(len(case.steps) for case in actual))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--format", choices=("behave-json", "cucumber-json"), required=True
    )
    parser.add_argument("--feature", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    result = validate(args.root, args.report, args.format, args.feature)
    if result.passed:
        print(
            f"Gherkin PASS: {result.scenario_count} scenarios, {result.step_count} steps; all passed"
        )
        return 0
    print("Gherkin FAIL: " + "; ".join(result.errors))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
