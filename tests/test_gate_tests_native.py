from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_coverage_recipes as recipes  # noqa: E402
import gate_tests  # noqa: E402
import gate_tests_command  # noqa: E402
import gate_tests_native as native  # noqa: E402
import quality_config  # noqa: E402
import quality_run_tests  # noqa: E402
from gate_coverage_recipes import CoverageTools, Recipe  # noqa: E402
from gate_tests_parsers_other import parse_ctest, parse_meson  # noqa: E402
from gate_types import CommandRun, ToolPython  # noqa: E402
from quality_tools import ToolSet  # noqa: E402

CMAKE_LISTS = "project(calc C)\nenable_testing()\nadd_test(NAME a COMMAND a)\n"
EVERYTHING = ("cmake", "ctest", "meson", "ninja", "make", "cc")
NO_ENV: Mapping[str, str] = {}


def which_of(*available: str) -> Callable[[str], str | None]:
    return lambda name: name if name in available else None


class ProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")


class ProjectKindTests(ProjectTestCase):
    def test_cmake_needs_testing_declared_at_the_root(self) -> None:
        self.assertIsNone(native.project_kind(self.root))
        self.write("CMakeLists.txt", "project(calc C)\n")
        self.assertIsNone(native.project_kind(self.root))
        self.write("CMakeLists.txt", "project(calc)\ninclude(CTest)\n")
        self.assertEqual(native.project_kind(self.root), "cmake")

    def test_meson(self) -> None:
        self.write("meson.build", "project('calc', 'c')\n")
        self.assertEqual(native.project_kind(self.root), "meson")


class ToolchainTests(ProjectTestCase):
    def problem(self, *tools: str, environ: Mapping[str, str] = NO_ENV) -> str | None:
        return native.toolchain_problem(self.root, which_of(*tools), environ)

    def test_cmake_toolchain(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        self.assertEqual(self.problem(), "cmake is not on PATH")
        self.assertEqual(self.problem("cmake"), "ctest is not on PATH")
        self.assertEqual(
            self.problem("cmake", "ctest", "cc"), "neither ninja nor make is on PATH"
        )
        self.assertEqual(
            self.problem("cmake", "ctest", "make"), "no C or C++ compiler is on PATH"
        )
        self.assertIsNone(self.problem("cmake", "ctest", "ninja", "cc"))
        environ = {"CMAKE_GENERATOR": "Ninja", "CC": "gcc-13 -m64"}
        self.assertIsNone(self.problem("cmake", "ctest", "gcc-13", environ=environ))

    def test_meson_toolchain_and_other_projects(self) -> None:
        self.assertIsNone(self.problem())
        self.write("meson.build", "project('calc', 'c')\n")
        self.assertEqual(self.problem("meson"), "ninja is not on PATH")
        self.assertIsNone(self.problem("meson", "ninja", "clang"))
        with mock.patch.dict("os.environ", {"CC": "", "CXX": ""}):
            self.assertIsNotNone(native.toolchain_problem(self.root, which_of()))

    def test_inferred_test_commands(self) -> None:
        self.assertIsNone(native.native_test_command(self.root, which_of()))
        self.write("meson.build", "project('calc', 'c')\n")
        self.assertIsNone(native.native_test_command(self.root, which_of()))
        command = native.native_test_command(self.root, which_of(*EVERYTHING))
        self.assertEqual(command, ["meson", "test", "--print-errorlogs"])
        self.write("CMakeLists.txt", CMAKE_LISTS)
        command = native.native_test_command(self.root, which_of(*EVERYTHING))
        self.assertEqual(command, ["ctest", "--output-on-failure"])

    def test_gate_inference_and_problem_use_project_local_tools(self) -> None:
        self.write("meson.build", "project('calc', 'c')\n")
        local = self.root / ".venv" / "bin"
        local.mkdir(parents=True)
        (local / "meson").write_text("", encoding="utf-8")
        (local / "meson").chmod(0o755)
        which = which_of("ninja", "cc")
        command = gate_tests_command.infer_test_command(self.root, which)
        self.assertEqual(command, [str(local / "meson"), "test", "--print-errorlogs"])
        self.assertIsNone(gate_tests_command.native_toolchain_problem(self.root, which))
        self.assertEqual(
            gate_tests_command.native_toolchain_problem(self.root, which_of()),
            "ninja is not on PATH",
        )


class NativeBuildTests(ProjectTestCase):
    def build(
        self,
        *command: str,
        coverage: bool = False,
        environ: Mapping[str, str] = NO_ENV,
        tools: Sequence[str] = EVERYTHING,
    ) -> native.NativeBuild | None:
        return native.native_build(
            self.root,
            command,
            self.root / "out",
            coverage=coverage,
            environ=environ,
            which=which_of(*tools),
        )

    def test_cmake_plain_build(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        build = self.build("ctest", "--output-on-failure")
        assert build is not None
        directory = str(self.root / "out" / "cmake-build")
        configure, compile_step = build.steps
        self.assertEqual(
            configure,
            ("cmake", "-S", str(self.root), "-B", directory)
            + ("-DCMAKE_BUILD_TYPE=Debug", "-G", "Ninja"),
        )
        self.assertEqual(
            compile_step[:5], ("cmake", "--build", directory, "--config", "Debug")
        )
        self.assertEqual(
            build.command,
            ("ctest", "--test-dir", directory, "-C", "Debug", "--output-on-failure"),
        )

    def test_cmake_coverage_keeps_the_inherited_flags(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        environ = {"CFLAGS": "-Wall", "CMAKE_GENERATOR": "Unix Makefiles"}
        build = self.build("/opt/bin/ctest.exe", coverage=True, environ=environ)
        assert build is not None
        configure = build.steps[0]
        self.assertEqual(configure[0], "/opt/bin/cmake")
        self.assertIn("-DCMAKE_C_FLAGS=-Wall --coverage", configure)
        self.assertIn("-DCMAKE_CXX_FLAGS=--coverage", configure)
        self.assertIn("-DCMAKE_EXE_LINKER_FLAGS=--coverage", configure)
        self.assertNotIn("-G", configure)

    def test_meson_builds(self) -> None:
        self.write("meson.build", "project('calc', 'c')\n")
        directory = str(self.root / "out" / "meson-build")
        build = self.build("meson", "test", "--print-errorlogs", coverage=True)
        assert build is not None
        self.assertEqual(
            build.steps,
            (
                ("meson", "setup", "--buildtype=debug", "-Db_coverage=true")
                + (directory, str(self.root)),
                ("meson", "compile", "-C", directory),
            ),
        )
        self.assertEqual(
            build.command, ("meson", "test", "-C", directory, "--print-errorlogs")
        )
        plain = self.build("meson", "test")
        assert plain is not None
        self.assertNotIn("-Db_coverage=true", plain.steps[0])

    def test_commands_that_name_their_own_build_are_left_alone(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        self.write("meson.build", "project('calc', 'c')\n")
        self.assertIsNone(self.build("ctest", "--test-dir", "build"))
        self.assertIsNone(self.build("ctest", "--test-dir=build"))
        self.assertIsNone(self.build("meson", "test", "-C", "build"))
        self.assertIsNone(self.build("meson", "compile"))
        self.assertIsNone(self.build("pytest"))
        self.assertIsNone(self.build())

    def test_commands_without_their_build_file(self) -> None:
        self.assertIsNone(self.build("ctest"))
        self.assertIsNone(self.build("meson", "test"))
        with mock.patch.dict("os.environ", {"CMAKE_GENERATOR": "Ninja"}):
            self.write("CMakeLists.txt", CMAKE_LISTS)
            build = native.native_build(self.root, ["ctest"], self.root, coverage=False)
        assert build is not None
        self.assertNotIn("-G", build.steps[0])

    def test_unreadable_build_file(self) -> None:
        self.assertEqual(native._read(self.root / "missing"), "")


class ParserTests(unittest.TestCase):
    def test_ctest_summaries(self) -> None:
        text = (
            "50% tests passed, 2 tests failed out of 4\n\n"
            "The following tests did not run:\n"
            "\t  3 - skip (Skipped)\n\t  4 - off (Disabled)\n\n"
            "The following tests FAILED:\n\t  2 - bad (Failed)\n"
        )
        execution = parse_ctest(text)
        assert execution is not None
        self.assertEqual(
            (execution.executed, execution.passed, execution.failed, execution.skipped),
            (5, 1, 2, 2),
        )
        passing = gate_tests.parse_test_execution("100% tests passed out of 3\n")
        self.assertEqual((passing.parser, passing.passed), ("ctest", 3))
        self.assertIsNone(parse_ctest("No tests were found!!!\n"))

    def test_meson_summaries(self) -> None:
        lines = "Ok:  1\nExpected Fail:  1\nFail:  1\nSkipped:  1\n\nFull log\n"
        execution = parse_meson(lines)
        assert execution is not None
        self.assertEqual(
            (execution.executed, execution.passed, execution.failed),
            (4, 1, 1),
        )
        self.assertEqual((execution.skipped, execution.xfailed), (2, 1))
        one_line = (
            "Ok: 3  Expected Fail: 0  Fail: 1  Unexpected Pass: 1  "
            "Skipped: 0  Timeout: 1\n"
        )
        execution = parse_meson(one_line)
        assert execution is not None
        self.assertEqual((execution.executed, execution.failed), (6, 3))
        self.assertIsNone(parse_meson("Fail: 1\n"))


TOOL_PYTHON = ToolPython(sys.executable, {})
TOOLS = ToolSet(TOOL_PYTHON, CoverageTools(tool_python=TOOL_PYTHON))


class NativeRecipeTests(ProjectTestCase):
    def test_cmake_recipe(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        out = self.root / "out"
        recipe = recipes.plan_coverage(
            self.root, ["ctest"], out, TOOLS.coverage_tools, environ={}
        )
        assert isinstance(recipe, Recipe)
        self.assertEqual(recipe.format, "gcov")
        self.assertEqual(recipe.report, out / "gcov")
        self.assertEqual(recipe.command[:2], ("ctest", "--test-dir"))
        self.assertIn("-DCMAKE_C_FLAGS=--coverage", recipe.build[0])
        self.assertEqual(len(recipe.setup), 2)
        finalize = recipe.finalize[0]
        self.assertTrue(finalize[2].endswith("gate_coverage_gcov_collect.py"))
        self.assertEqual(
            finalize[3:], (str(out / "cmake-build"), str(self.root), str(out / "gcov"))
        )
        self.assertIn(".c", recipe.measures or ())
        self.assertNotIn(".h", recipe.measures or ())

    def test_a_projects_own_build_has_no_recipe(self) -> None:
        self.write("CMakeLists.txt", CMAKE_LISTS)
        problem = recipes.plan_coverage(
            self.root, ["ctest", "--test-dir", "build"], self.root, TOOLS.coverage_tools
        )
        self.assertIsInstance(problem, str)
        self.assertIn("no coverage recipe for `ctest --test-dir build`", str(problem))


class FakeRunner:
    def __init__(self, fail: str = "", output: str = "") -> None:
        self.fail = fail
        self.output = output
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: Sequence[str],
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        self.commands.append(tuple(command))
        if self.fail and self.fail in command:
            return CommandRun(tuple(command), 2, self.output, 0.1)
        return CommandRun(tuple(command), 0, "100% tests passed out of 2\n", 0.1)


class RunNativeTestsTests(ProjectTestCase):
    def request(
        self, runner: FakeRunner, **config: object
    ) -> quality_run_tests.TestRunRequest:
        return quality_run_tests.TestRunRequest(
            self.root,
            {"test": {"command": ["ctest"]}, **config},
            quality_config.default_thresholds(),
            TOOLS,
            runner,
            self.root / "out",
            ["src/calc.c"],
        )

    def setUp(self) -> None:
        super().setUp()
        self.write("CMakeLists.txt", CMAKE_LISTS)

    def test_build_then_test_with_coverage(self) -> None:
        runner = FakeRunner(fail="-DCMAKE_BUILD_TYPE=Debug")
        result = quality_run_tests.run_tests(self.request(FakeRunner()))
        self.assertEqual(result.outcome.status, "pass")
        self.assertIn("gcov", result.problem)
        result = quality_run_tests.run_tests(self.request(runner))
        self.assertEqual(result.outcome.status, "fail")
        self.assertIn("The build step `cmake -S", result.outcome.summary)
        self.assertEqual(result.problem, quality_run_tests.BUILD_FAILED)
        self.assertEqual(len(runner.commands), 3)

    def test_a_build_that_cannot_start_blocks(self) -> None:
        runner = FakeRunner(fail="--build", output="sh: cmake: command not found\n")
        result = quality_run_tests.run_tests(self.request(runner))
        self.assertEqual(result.outcome.status, "blocked")

    def test_plain_run_builds_without_coverage(self) -> None:
        runner = FakeRunner()
        request = self.request(runner, coverage={"format": "lcov"})
        result = quality_run_tests.run_tests(request)
        self.assertEqual(result.outcome.status, "pass")
        self.assertIn("coverage.report is required", result.problem)
        self.assertNotIn("-DCMAKE_C_FLAGS=--coverage", runner.commands[0])
        self.assertEqual(runner.commands[-1][:2], ("ctest", "--test-dir"))
        failing = FakeRunner(fail="--build", output="error: x\n")
        result = quality_run_tests.run_tests(
            self.request(failing, coverage={"format": "lcov"})
        )
        self.assertEqual(result.outcome.status, "fail")

    def test_setup_failure_still_builds(self) -> None:
        runner = FakeRunner(fail="-S")
        recipe = Recipe(
            ("ctest", "--test-dir", "b"),
            {},
            self.root / "out" / "gcov",
            "gcov",
            setup=(("setup-step",),),
            build=(("cmake", "-S", "x"),),
        )
        runner.fail = "setup-step"
        result = quality_run_tests._run_with_recipe(self.request(runner), recipe)
        self.assertIn("coverage setup failed", result.problem)
        self.assertEqual(runner.commands[1], ("cmake", "-S", "x"))

    def test_missing_toolchain_is_the_blocked_reason(self) -> None:
        request = self.request(FakeRunner(), test={})
        with (
            mock.patch.object(
                quality_run_tests, "infer_test_command", return_value=None
            ),
            mock.patch.object(
                quality_run_tests,
                "native_toolchain_problem",
                return_value="cmake is not on PATH",
            ),
        ):
            result = quality_run_tests.run_tests(request)
        self.assertEqual(result.outcome.status, "blocked")
        self.assertEqual(
            result.outcome.summary, "No test command was found: cmake is not on PATH."
        )


if __name__ == "__main__":
    unittest.main()
