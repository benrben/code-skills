"""Serialize quality-loop results and write the machine-readable state atomically."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence


def function_measurement(function: Any) -> dict[str, Any]:
    return {
        "path": function.path,
        "name": function.name,
        "line": function.start_line,
        "covered_lines": function.covered_lines,
        "total_lines": function.total_lines,
        "coverage_percent": round(function.coverage_percent, 2),
        "complexity": function.complexity,
        "craap_score": round(function.craap_score, 2),
        "passed": function.passed,
    }


def file_measurement(file: Any) -> dict[str, Any]:
    return {
        "path": file.path,
        "lines": file.lines,
        "limit": file.limit,
        "passed": file.passed,
    }


def mutation_failure(mutation: Any) -> dict[str, Any]:
    return {
        "id": mutation.mutant_id,
        "path": mutation.path,
        "line": mutation.line,
        "column": mutation.column,
        "change": f"{mutation.original} -> {mutation.replacement}",
        "status": mutation.status or ("Survived" if mutation.survived else "Killed"),
        "static": bool(getattr(mutation, "static", False)),
    }


def dependency_failure(violation: Any) -> dict[str, Any]:
    return {
        "source": violation.source,
        "line": violation.line,
        "source_module": violation.source_module,
        "target": violation.target,
        "target_module": violation.target_module,
        "rule": violation.rule,
    }


def state_status(analysis: Any, error: str | None) -> str:
    if error:
        return "error"
    if analysis.passed:
        return "pass"
    return "ready_for_full" if analysis.ready_for_full else "fail"


def command_state(command: Any) -> dict[str, Any]:
    return {
        "command": command.command,
        "returncode": command.returncode,
        "timed_out": command.timed_out,
        "duration_seconds": round(command.duration_seconds, 3),
    }


def gate_status(result: Any) -> str:
    if result.deferred:
        return "deferred"
    if not result.applicable:
        return "not_applicable"
    return "pass" if result.passed else "fail"


def gate_state(result: Any) -> dict[str, Any]:
    return {
        "key": result.key,
        "status": gate_status(result),
        "summary": result.summary,
        "details": result.details[:100],
        "commands": [command_state(item) for item in result.command_results],
    }


def quality_gate_for(analysis: Any) -> Any:
    return next((result for result in analysis.gates if result.key == "quality"), None)


def metrics_state(analysis: Any, quality_gate: Any) -> dict[str, Any]:
    return {
        "certified": bool(analysis.functions and quality_gate and quality_gate.passed),
        "functions": [function_measurement(item) for item in analysis.functions],
        "files": [file_measurement(item) for item in analysis.files],
    }


def count_state(
    analysis: Any,
    failing_functions: Sequence[Any],
    survivors: Sequence[Any],
    violations: Sequence[Any],
) -> dict[str, int]:
    outcomes = [gate_status(item) for item in analysis.gates]
    return {
        "checks_total": len(outcomes),
        "checks_executed": len(outcomes) - outcomes.count("deferred"),
        "checks_deferred": outcomes.count("deferred"),
        "checks_applicable": outcomes.count("pass") + outcomes.count("fail"),
        "checks_passing": outcomes.count("pass"),
        "functions_total": len(analysis.functions),
        "functions_failing": len(failing_functions),
        "files_total": len(analysis.files),
        "files_failing_loc": sum(not item.passed for item in analysis.files),
        "mutants_total": len(analysis.mutations),
        "mutants_surviving": len(survivors),
        "mutants_static": sum(
            bool(getattr(item, "static", False)) for item in analysis.mutations
        ),
        "dependency_violations": len(violations),
    }


def failed_check_state(gates: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "title": item.title,
            "summary": item.summary,
            "details": item.details[:100],
        }
        for item in gates
        if item.applicable and not item.deferred and not item.passed
    ]


def failed_file_state(files: Sequence[Any]) -> list[dict[str, Any]]:
    return [file_measurement(item) for item in files if not item.passed][:200]


def failed_tool_state(failed_setup: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "command": item.command,
            "returncode": item.returncode,
            "output": item.stdout[-4000:],
        }
        for item in failed_setup
    ]


def failure_state(
    analysis: Any,
    failing_functions: Sequence[Any],
    survivors: Sequence[Any],
    violations: Sequence[Any],
    failed_setup: Sequence[Any],
) -> dict[str, Any]:
    return {
        "checks": failed_check_state(analysis.gates),
        "functions": [function_measurement(item) for item in failing_functions[:200]],
        "files": failed_file_state(analysis.files),
        "surviving_mutants": [mutation_failure(item) for item in survivors[:200]],
        "dependencies": [dependency_failure(item) for item in violations[:200]],
        "tool_setup": failed_tool_state(failed_setup),
    }


def failing_function_items(analysis: Any) -> list[Any]:
    return sorted(
        (item for item in analysis.functions if not item.passed),
        key=lambda item: (-item.craap_score, item.coverage_percent, item.path),
    )


def surviving_mutation_items(analysis: Any) -> list[Any]:
    return [item for item in analysis.mutations if item.survived]


def failed_setup_items(analysis: Any) -> list[Any]:
    return [item for item in analysis.tool_setup if item.returncode != 0]


def repository_certified(analysis: Any) -> bool:
    return bool(analysis.passed and not analysis.scope.incremental)


def state_fix_prompt(gate: ModuleType, analysis: Any) -> str | None:
    return None if analysis.passed else gate.master_fix_prompt(analysis)


def analysis_state(
    gate: ModuleType,
    analysis: Any,
    html_path: Path,
    state_path: Path,
    exit_code: int,
    error: str | None = None,
) -> dict[str, Any]:
    failing_functions = failing_function_items(analysis)
    survivors = surviving_mutation_items(analysis)
    violations = analysis.dependency_violations
    failed_setup = failed_setup_items(analysis)
    quality_gate = quality_gate_for(analysis)
    return {
        "schema_version": 1,
        "status": state_status(analysis, error),
        "mode": analysis.mode,
        "certified": repository_certified(analysis),
        "scope_certified": analysis.passed,
        "ready_for_full": analysis.ready_for_full,
        "exit_code": exit_code,
        "repository": analysis.root,
        "generated_at": analysis.generated_at,
        "artifacts": {"html": str(html_path), "state": str(state_path)},
        "rerun_command": analysis.rerun_command,
        "full_rerun_command": gate.without_fast_flag(analysis.rerun_command or ""),
        "scope": {
            "kind": analysis.scope.kind,
            "reference": analysis.scope.reference,
            "changed_files": list(analysis.scope.paths),
        },
        "metrics": metrics_state(analysis, quality_gate),
        "thresholds": analysis.thresholds,
        "gates": [gate_state(result) for result in analysis.gates],
        "counts": count_state(analysis, failing_functions, survivors, violations),
        "failures": failure_state(
            analysis, failing_functions, survivors, violations, failed_setup
        ),
        "fix_prompt": state_fix_prompt(gate, analysis),
        "error": error,
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
