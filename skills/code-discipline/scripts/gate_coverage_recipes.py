"""Coverage recipes: how to run the project's own test command with coverage.

A recipe never runs anything. It describes the instrumented test command,
the environment, files to write first, setup and finalize commands, and
where the report will be. The tests gate runs the instrumented command once,
so tests and coverage come from the same execution.

Helper steps (cleaning a directory, copying a report whose path is only known
after the run, discovering compiled classes) are small ``python -c`` programs
from ``gate_coverage_programs``, run with the tool interpreter.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gate_coverage_programs import (
    COPY_PRINTED_PATH,
    FRESH_DIRECTORY,
    JACOCO_REPORT,
    PYTHON_FINALIZE,
    coverage_rc,
    python_startup,
    ruby_helper,
)
from gate_coverage_python import is_python_command, program_name, python_interpreter
from gate_coverage_readers import FORMATS
from gate_tests_native import MEASURED_SUFFIXES, native_build
from gate_types import ToolPython

Command = tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CoverageTools:
    """Pinned coverage tools the orchestrator has installed in its cache.

    ``tool_python.env`` should carry a ``PYTHONPATH`` that makes coverage.py
    importable; the Python recipe appends it to the project's ``sys.path``.
    """

    tool_python: ToolPython = dataclasses.field(default_factory=ToolPython)
    c8: str | None = None
    jacoco_agent: str | None = None
    jacoco_cli: str | None = None
    cargo_llvm_cov: str | None = None


@dataclasses.dataclass(frozen=True)
class Recipe:
    """What to run so the test command also writes a coverage report.

    Order: call ``prepare_recipe`` (writes ``files``, removes a stale report
    file), run ``setup``, run ``build`` (a failed build step is the test
    run's failure), run ``command`` (this is the test run), run
    ``finalize``, then read ``report`` as ``format`` with
    ``read_coverage_report(..., started_at=...)``. Every step runs with
    ``env`` merged over the inherited environment; ``env`` already contains
    any inherited value it extends. ``report`` may be a directory; every
    matching file inside is merged.

    Pass ``measures`` and ``prerequisite`` to ``evaluate_coverage``:
    ``measures`` lists the file extensions this runtime can measure (None:
    whatever the report covers), and ``prerequisite`` names what the project
    must provide, so a missing report explains itself.
    """

    command: Command
    env: Mapping[str, str]
    report: Path
    format: str
    setup: tuple[Command, ...] = ()
    finalize: tuple[Command, ...] = ()
    note: str = ""
    files: Mapping[Path, str] = dataclasses.field(default_factory=dict)
    measures: frozenset[str] | None = None
    prerequisite: str = ""
    build: tuple[Command, ...] = ()


def prepare_recipe(recipe: Recipe) -> None:
    """Create the report's directory, remove a report file left by an earlier
    run, and write the recipe's helper files.

    The orchestrator calls this before ``setup``; it is the only function in
    this module with side effects."""
    recipe.report.parent.mkdir(parents=True, exist_ok=True)
    if recipe.report.is_file():
        recipe.report.unlink()
    for path, content in recipe.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class _Plan:
    root: Path
    command: Command
    out_dir: Path
    tools: CoverageTools
    inherited: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def program(self) -> str:
        """The executable's bare name: ``.venv/bin/pytest`` gives ``pytest``."""
        return program_name(self.command[0]) if self.command else ""

    def helper(self, code: str, *arguments: str) -> Command:
        """A stdlib-only helper program. ``-S`` skips site initialization, so
        the Python recipe's startup hook never measures helper processes."""
        return (self.tools.tool_python.executable, "-S", "-c", code, *arguments)

    def fresh_directory(self, directory: Path) -> Command:
        return self.helper(FRESH_DIRECTORY, str(directory))

    def extended(self, name: str, option: str) -> str:
        """``option`` added after the inherited options in variable ``name``."""
        inherited = self.inherited.get(name, "")
        return f"{inherited} {option}" if inherited.strip() else option

    def spaceless_directory(self) -> Path | None:
        """A directory for paths that RUBYOPT or JAVA_TOOL_OPTIONS carry: both
        split on whitespace. ``out_dir`` when it has none, else a directory in
        the system temp folder named after ``out_dir``; None when both have
        whitespace."""
        if not _has_whitespace(str(self.out_dir)):
            return self.out_dir
        digest = hashlib.sha256(str(self.out_dir).encode("utf-8")).hexdigest()[:16]
        directory = Path(tempfile.gettempdir()) / f"code-discipline-coverage-{digest}"
        return None if _has_whitespace(str(directory)) else directory


def _has_whitespace(text: str) -> bool:
    return any(character.isspace() for character in text)


_NODE_PROGRAMS = {"npm", "pnpm", "yarn", "bun", "npx", "node", "jest", "vitest"}
_JVM_PROGRAMS = {"mvn", "mvnw", "gradle", "gradlew"}
_RUBY_PROGRAMS = {"bundle", "rspec", "rake", "ruby"}

# Directories under the root whose Python files are never production code.
_PYTHON_OMITTED = ("tests", "test", ".venv", "venv", "node_modules")
_NODE_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"})


# --- Python ------------------------------------------------------------------


def _python_recipe(plan: _Plan) -> Recipe | str:
    """The test command runs unchanged. A ``sitecustomize`` on PYTHONPATH
    starts coverage.py in every Python process it launches, so interpreter
    options, wrappers such as ``uv run``, and subprocesses all work."""
    python = python_interpreter(plan.root, plan.command)
    if python is None:
        return "no Python interpreter was found for the test command"
    directory = plan.out_dir / "python"
    startup = plan.out_dir / "python-startup"
    rcfile = plan.out_dir / "coveragerc"
    report = directory / "coverage.json"
    omitted = [plan.root / name for name in _PYTHON_OMITTED]
    tool_path = plan.tools.tool_python.env.get("PYTHONPATH", "")
    # The startup directory goes first so its sitecustomize wins (and then
    # runs any inherited one); inherited entries keep their order.
    python_path = [str(startup), plan.inherited.get("PYTHONPATH", "")]
    finalize = (*python, "-S", "-c", PYTHON_FINALIZE)
    return Recipe(
        command=plan.command,
        env={
            "PYTHONPATH": os.pathsep.join(part for part in python_path if part),
            "COVERAGE_PROCESS_START": str(rcfile),
        },
        report=report,
        format="coverage-json",
        setup=(plan.fresh_directory(directory),),
        finalize=((*finalize, str(rcfile), str(report), str(startup)),),
        note="coverage.py with branch measurement in every Python process",
        files={
            rcfile: coverage_rc(plan.root, directory, [*omitted, plan.out_dir]),
            startup / "sitecustomize.py": python_startup(
                [path for path in tool_path.split(os.pathsep) if path]
            ),
        },
        measures=frozenset({".py"}),
    )


# --- JavaScript / TypeScript -------------------------------------------------


def _package_json(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _script_name(command: Command) -> str | None:
    """The package.json script a package-manager command runs."""
    arguments = [argument for argument in command[1:] if not argument.startswith("-")]
    if not arguments:
        return None
    if arguments[0] in ("run", "run-script") and len(arguments) > 1:
        return arguments[1]
    return "test" if arguments[0] in ("t", "tst") else arguments[0]


def _node_framework(plan: _Plan, package: Mapping[str, Any]) -> str:
    """``jest``, ``vitest``, or ``other`` for the code the test command runs."""
    words = {Path(argument).name for argument in plan.command}
    if plan.program in ("npm", "pnpm", "yarn", "bun"):
        scripts = package.get("scripts", {})
        script = scripts.get(_script_name(plan.command) or "", "")
        words |= set(re.findall(r"[\w@/.-]+", str(script)))
    words = {Path(word).name for word in words}
    if "vitest" in words:
        return "vitest"
    return "jest" if "jest" in words else "other"


def _dependencies(package: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        value = package.get(section, {})
        if isinstance(value, dict):
            names.update(value)
    return names


def _with_arguments(plan: _Plan, extra: Sequence[str]) -> Command:
    """Append runner flags; npm needs ``--`` before arguments for the script."""
    if plan.program == "npm" and "--" not in plan.command:
        return (*plan.command, "--", *extra)
    return (*plan.command, *extra)


def _runner_flags(plan: _Plan, directory: Path) -> tuple[str, ...] | None:
    """Flags that make bun, Jest, or Vitest write LCOV themselves, or None when
    the command needs Node's V8 coverage plus c8 instead."""
    if plan.program == "bun" and plan.command[1:2] == ("test",):
        return ("--coverage", "--coverage-reporter=lcov", f"--coverage-dir={directory}")
    package = _package_json(plan.root)
    framework = _node_framework(plan, package)
    if framework == "jest":
        return (
            "--coverage",
            "--coverageProvider=v8",
            "--coverageReporters=lcov",
            f"--coverageDirectory={directory}",
        )
    providers = {"@vitest/coverage-v8", "@vitest/coverage-istanbul"}
    if framework == "vitest" and providers & _dependencies(package):
        return (
            "--coverage.enabled",
            "--coverage.reporter=lcov",
            f"--coverage.reportsDirectory={directory}",
        )
    return None


def _node_recipe(plan: _Plan) -> Recipe | str:
    directory = plan.out_dir / "node"
    report = directory / "lcov.info"
    setup = (plan.fresh_directory(directory),)
    flags = _runner_flags(plan, directory)
    if flags is None:
        return _c8_recipe(plan, directory, report, setup)
    command = _with_arguments(plan, flags)
    return Recipe(command, {}, report, "lcov", setup, measures=_NODE_EXTENSIONS)


def _c8_recipe(
    plan: _Plan, directory: Path, report: Path, setup: tuple[Command, ...]
) -> Recipe | str:
    if plan.tools.c8 is None:
        return "c8 is not installed in the tool cache (needed for Node V8 coverage)"
    v8 = directory / "v8"
    finalize = (
        plan.tools.c8,
        "report",
        "--temp-directory",
        str(v8),
        "--reporter=lcov",
        "--report-dir",
        str(directory),
        "--all",
        "--src",
        str(plan.root),
    )
    return Recipe(
        command=plan.command,
        env={"NODE_V8_COVERAGE": str(v8)},
        report=report,
        format="lcov",
        setup=setup,
        finalize=(finalize,),
        note="Node built-in V8 coverage converted to LCOV by c8",
        measures=_NODE_EXTENSIONS,
    )


# --- Go, Rust, .NET, Swift, PHP ----------------------------------------------


def _go_recipe(plan: _Plan) -> Recipe | str:
    if "test" not in plan.command:
        return "the Go test command does not run `go test`"
    position = plan.command.index("test") + 1
    report = plan.out_dir / "go-cover.out"
    flags = (f"-coverprofile={report}", "-covermode=atomic", "-coverpkg=./...")
    command = (*plan.command[:position], *flags, *plan.command[position:])
    return Recipe(command, {}, report, "go-cover", measures=frozenset({".go"}))


def _rust_recipe(plan: _Plan) -> Recipe | str:
    if plan.tools.cargo_llvm_cov is None:
        return "cargo-llvm-cov is not installed in the tool cache"
    if plan.command[1:2] != ("test",):
        return "the Rust test command does not run `cargo test`"
    report = plan.out_dir / "lcov.info"
    command = (
        plan.tools.cargo_llvm_cov,
        "llvm-cov",
        "--lcov",
        "--output-path",
        str(report),
        *plan.command[2:],
    )
    return Recipe(
        command,
        {},
        report,
        "lcov",
        note="cargo llvm-cov replaces cargo test",
        measures=frozenset({".rs"}),
    )


def _dotnet_recipe(plan: _Plan) -> Recipe | str:
    if plan.command[1:2] != ("test",):
        return "the .NET test command does not run `dotnet test`"
    directory = plan.out_dir / "dotnet"
    flags = ("--collect:XPlat Code Coverage", "--results-directory", str(directory))
    return Recipe(
        command=(*plan.command, *flags),
        env={},
        report=directory,
        format="cobertura",
        setup=(plan.fresh_directory(directory),),
        note="coverlet's XPlat Code Coverage collector, one report per test project",
        measures=frozenset({".cs"}),
        prerequisite="the coverlet.collector package in every test project",
    )


def _swift_recipe(plan: _Plan) -> Recipe | str:
    if plan.command[1:2] != ("test",):
        return "the Swift test command does not run `swift test`"
    report = plan.out_dir / "swift-coverage.json"
    copy = plan.helper(
        COPY_PRINTED_PATH, str(report), plan.command[0], "test", "--show-codecov-path"
    )
    return Recipe(
        command=(*plan.command, "--enable-code-coverage"),
        env={},
        report=report,
        format="llvm-cov-json",
        finalize=(copy,),
        measures=frozenset({".swift"}),
    )


def _php_recipe(plan: _Plan) -> Recipe | str:
    report = plan.out_dir / "cobertura.xml"
    return Recipe(
        command=(*plan.command, "--coverage-cobertura", str(report)),
        env={"XDEBUG_MODE": "coverage"},
        report=report,
        format="cobertura",
        note="PHPUnit's Cobertura report",
        measures=frozenset({".php"}),
        prerequisite=(
            "the pcov or Xdebug PHP extension (PHPUnit writes no coverage "
            "report without one; check with `php -m`)"
        ),
    )


# --- JVM ---------------------------------------------------------------------


def _jvm_recipe(plan: _Plan) -> Recipe | str:
    agent, cli = plan.tools.jacoco_agent, plan.tools.jacoco_cli
    if agent is None or cli is None:
        return "the JaCoCo agent and CLI jars are not installed in the tool cache"
    if _has_whitespace(agent):
        return f"the JaCoCo agent path has whitespace, which JAVA_TOOL_OPTIONS splits: {agent}"
    work = plan.spaceless_directory()
    if work is None:
        return f"no path without whitespace for the JaCoCo execution data of {plan.out_dir}"
    execution = work / "jacoco-exec" / "jacoco.exec"
    report = plan.out_dir / "jacoco" / "jacoco.xml"
    option = f"-javaagent:{agent}=destfile={execution},append=true"
    return Recipe(
        command=plan.command,
        env={"JAVA_TOOL_OPTIONS": plan.extended("JAVA_TOOL_OPTIONS", option)},
        report=report,
        format="jacoco",
        setup=(
            plan.fresh_directory(execution.parent),
            plan.fresh_directory(report.parent),
        ),
        finalize=(
            plan.helper(
                JACOCO_REPORT, cli, str(execution), str(report), str(plan.root)
            ),
        ),
        note="JaCoCo agent attached to every JVM the build starts",
        measures=frozenset({".java", ".kt"}),
    )


# --- Ruby --------------------------------------------------------------------


def _ruby_recipe(plan: _Plan) -> Recipe | str:
    work = plan.spaceless_directory()
    if work is None:
        return f"no path without whitespace for the RUBYOPT coverage helper of {plan.out_dir}"
    directory = plan.out_dir / "ruby"
    helper = work / "ruby_coverage.rb"
    return Recipe(
        command=plan.command,
        env={"RUBYOPT": plan.extended("RUBYOPT", f"-r{helper}")},
        report=directory,
        format="ruby-coverage",
        setup=(plan.fresh_directory(directory),),
        note="Ruby's built-in Coverage module, one report per Ruby process",
        files={helper: ruby_helper(plan.root, directory)},
        measures=frozenset({".rb"}),
    )


# --- C and C++ (CMake, Meson) -----------------------------------------------

_GCOV_COLLECTOR = Path(__file__).with_name("gate_coverage_gcov_collect.py")


def _native_recipe(plan: _Plan) -> Recipe | str:
    """Build with ``--coverage`` in a directory of the run's own, run the
    tests there, then turn the ``.gcda`` files into gcov text reports."""
    build = native_build(
        plan.root, plan.command, plan.out_dir, coverage=True, environ=plan.inherited
    )
    if build is None:
        return f"no coverage recipe for `{shlex.join(plan.command)}`"
    report = plan.out_dir / "gcov"
    collect = (plan.tools.tool_python.executable, "-S", str(_GCOV_COLLECTOR))
    return Recipe(
        command=build.command,
        env={},
        report=report,
        format="gcov",
        setup=(plan.fresh_directory(build.directory), plan.fresh_directory(report)),
        finalize=((*collect, str(build.directory), str(plan.root), str(report)),),
        note="gcov line and branch counts from a --coverage build",
        measures=MEASURED_SUFFIXES,
        build=build.steps,
    )


# --- Dispatch ----------------------------------------------------------------

_Builder = Callable[[_Plan], "Recipe | str"]

_BY_PROGRAM: dict[str, _Builder] = {
    "go": _go_recipe,
    "cargo": _rust_recipe,
    "dotnet": _dotnet_recipe,
    "swift": _swift_recipe,
    "phpunit": _php_recipe,
    "ctest": _native_recipe,
    "meson": _native_recipe,
    **{program: _node_recipe for program in _NODE_PROGRAMS},
    **{program: _jvm_recipe for program in _JVM_PROGRAMS},
    **{program: _ruby_recipe for program in _RUBY_PROGRAMS},
}


def _builder_for(plan: _Plan) -> _Builder | None:
    program = plan.program
    if is_python_command(plan.command):
        return _python_recipe
    if program == "php" and any(part.endswith("phpunit") for part in plan.command):
        return _php_recipe
    return _BY_PROGRAM.get(program)


def _substitute(text: str, values: Mapping[str, str]) -> str:
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def _configured_command(
    configured: Any, values: Mapping[str, str], test_command: Command
) -> Command:
    """Split a command string first and substitute afterwards, so a root
    with spaces stays one argument."""
    if isinstance(configured, str):
        configured = shlex.split(configured)
    if not configured:
        return test_command
    return tuple(_substitute(str(part), values) for part in configured)


def _configured_recipe(
    root: Path, out_dir: Path, test_command: Command, config: Mapping[str, Any]
) -> Recipe | str:
    report_value = config.get("report")
    if not report_value:
        return (
            "coverage.report is required when coverage.command or "
            "coverage.format is configured"
        )
    format_name = str(config.get("format", "auto"))
    if format_name != "auto" and format_name not in FORMATS:
        return (
            f"coverage.format {format_name!r} is not one of: auto, {', '.join(FORMATS)}"
        )
    values = {"{root}": str(root), "{out_dir}": str(out_dir)}
    report = Path(_substitute(str(report_value), values))
    return Recipe(
        command=_configured_command(config.get("command"), values, test_command),
        env={},
        report=report if report.is_absolute() else root / report,
        format=format_name,
        note="configured coverage adapter",
    )


def plan_coverage(
    root: Path,
    test_command: Sequence[str],
    out_dir: Path,
    tools: CoverageTools,
    configured: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Recipe | str:
    """The recipe for this test command, or why coverage cannot be measured
    (report that reason as the coverage gate's BLOCKED summary).

    ``configured`` is the ``coverage`` section of the project config; its
    ``command``/``report``/``format`` always win over inference. ``{root}``
    and ``{out_dir}`` are substituted in configured values. ``environ`` is
    the environment the test command inherits (default ``os.environ``);
    recipe variables such as PYTHONPATH extend it rather than replace it.
    """
    command = tuple(test_command)
    if configured and any(key in configured for key in ("command", "report", "format")):
        return _configured_recipe(root, out_dir, command, configured)
    if not command:
        return "no test command was found, so coverage cannot be measured"
    inherited = os.environ if environ is None else environ
    plan = _Plan(root, command, out_dir, tools, inherited)
    builder = _builder_for(plan)
    if builder is None:
        return f"no coverage recipe for `{shlex.join(command)}`"
    return builder(plan)
