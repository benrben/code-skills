import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_coverage_gcov_collect as collect  # noqa: E402
import gate_coverage_readers as readers  # noqa: E402
from gate_coverage_data import CoverageReportError  # noqa: E402
from gate_coverage_gcov import parse_gcov  # noqa: E402

USAGE = "usage: gate_coverage_gcov_collect.py BUILD ROOT REPORT"
GCC_REPORT = """\
        -:    0:Source:/repo/src/calc.c
        -:    0:Graph:/build/calc.c.gcno
        -:    0:Runs:1
function calc_clamp called 3 returned 100% blocks executed 80%
        3:    7:int calc_clamp(int value, int low, int high) {
        3*:   8:    if (value < low) {
branch  0 taken 1 (fallthrough)
branch  1 taken 2
    #####:    9:        return low;
        -:   10:    }
        2:   11:    if (value > high) {
branch  0 never executed
branch  1 taken 2
branch  2 taken 0 (throw)
call    0 returned 2
    =====:   12:        throw_it();
unconditional  0 taken 1
        -:   13:    }
        -:    0:Source:/usr/include/stdio.h
        1:    4:static inline int f(void) { return 1; }
"""


class ParseGcovTests(unittest.TestCase):
    def test_lines_branches_and_marks(self) -> None:
        raw = parse_gcov("File 'calc.c'\nLines executed:80.00% of 5\n" + GCC_REPORT)
        calc = raw["/repo/src/calc.c"]
        self.assertEqual(calc.lines, {7: 3, 8: 3, 9: 0, 11: 2, 12: 0})
        self.assertEqual(calc.branches, {8: (2, 2), 11: (1, 2)})
        self.assertEqual(raw["/usr/include/stdio.h"].lines, {4: 1})

    def test_records_of_one_file_merge(self) -> None:
        text = (
            "        -:    0:Source:a.c\n        0:    1:x\nbranch  0 taken 0\n"
            "        -:    0:Source:a.c\n        4:    1:x\nbranch  0 taken 3\n"
        )
        coverage = parse_gcov(text)["a.c"]
        self.assertEqual(coverage.lines, {1: 4})
        self.assertEqual(coverage.branches, {1: (1, 1)})

    def test_text_without_a_record_is_not_gcov(self) -> None:
        with self.assertRaises(CoverageReportError):
            parse_gcov("branch  0 taken 1\n        1:    3:int x;\n")


class ReadGcovReportTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_report_directory_is_read_and_mapped_to_repository_paths(self) -> None:
        source = self.root / "src" / "calc.c"
        reports = self.root / "out"
        reports.mkdir()
        text = GCC_REPORT.replace("/repo/src/calc.c", str(source))
        (reports / "00000.gcov").write_text(text, encoding="utf-8")
        self.assertEqual(readers.guess_format(reports), "gcov")
        data = readers.read_coverage_report(reports, "auto", self.root, ["src/calc.c"])
        self.assertEqual(set(data), {"src/calc.c"})
        self.assertEqual(data["src/calc.c"].lines[9], 0)


def completed(
    command: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(list(command), returncode, stdout, stderr)


class FakeTools:
    """``which`` and ``run`` for the collector, with canned gcov output."""

    def __init__(self, *on_path: str, version: str = "", output: str = "") -> None:
        self.on_path = set(on_path)
        self.version = version
        self.output = output
        self.returncode = 0
        self.commands: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return name if name in self.on_path else None

    def run(
        self, command: Sequence[str], cwd: Path
    ) -> "subprocess.CompletedProcess[str]":
        self.commands.append(list(command))
        if "--version" in command:
            return completed(command, stdout=self.version)
        if "--find" in command:
            return completed(command, 0 if "found" in self.version else 1)
        if self.returncode:
            return completed(command, self.returncode, stderr="bad data\n")
        return completed(command, stdout=self.output)


class CompilerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.build = Path(directory.name).resolve()

    def test_cmake_records_the_compiler(self) -> None:
        folder = self.build / "CMakeFiles" / "4.0.0"
        folder.mkdir(parents=True)
        (folder / "CMakeCCompiler.cmake").write_text(
            'set(CMAKE_C_COMPILER "/usr/bin/gcc-13")\n'
            'set(CMAKE_C_COMPILER_ARG1 "")\nset(CMAKE_C_COMPILER_ID "GNU")\n',
            encoding="utf-8",
        )
        identity = collect.compiler_identity(self.build)
        self.assertEqual(identity, ("GNU", "/usr/bin/gcc-13"))

    def test_cmake_file_without_an_id_falls_through(self) -> None:
        folder = self.build / "CMakeFiles" / "4.0.0"
        folder.mkdir(parents=True)
        (folder / "CMakeCCompiler.cmake").write_text("# empty\n", encoding="utf-8")
        self.assertEqual(collect.compiler_identity(self.build), ("", ""))

    def test_cmake_id_without_a_path(self) -> None:
        folder = self.build / "CMakeFiles" / "4.0.0"
        folder.mkdir(parents=True)
        (folder / "CMakeCXXCompiler.cmake").write_text(
            'set(CMAKE_CXX_COMPILER_ID "Clang")\n', encoding="utf-8"
        )
        self.assertEqual(collect.compiler_identity(self.build), ("Clang", ""))

    def test_meson_records_the_compiler(self) -> None:
        info = self.build / "meson-info"
        info.mkdir()
        compilers = {"host": {"c": {"id": "clang", "exelist": ["clang-17"]}}}
        (info / "intro-compilers.json").write_text(
            json.dumps(compilers), encoding="utf-8"
        )
        self.assertEqual(collect.compiler_identity(self.build), ("clang", "clang-17"))

    def test_meson_without_compilers_or_readable_json(self) -> None:
        info = self.build / "meson-info"
        info.mkdir()
        path = info / "intro-compilers.json"
        path.write_text('{"host": {}}', encoding="utf-8")
        self.assertEqual(collect.compiler_identity(self.build), ("", ""))
        path.write_text("[", encoding="utf-8")
        self.assertEqual(collect.compiler_identity(self.build), ("", ""))

    def test_unreadable_cmake_file_reads_as_empty(self) -> None:
        self.assertEqual(collect._read(self.build / "missing.cmake"), "")


class GcovCommandTests(unittest.TestCase):
    build = Path("/build")

    def command(self, compiler_id: str, compiler: str, tools: FakeTools) -> list[str]:
        return collect.gcov_command(
            compiler_id, compiler, self.build, tools.which, tools.run
        )

    def test_clang_prefers_a_versioned_llvm_cov_beside_the_compiler(self) -> None:
        tools = FakeTools("/opt/llvm/bin/llvm-cov-17", "llvm-cov")
        command = self.command("Clang", "/opt/llvm/bin/clang-17", tools)
        self.assertEqual(command, ["/opt/llvm/bin/llvm-cov-17", "gcov"])

    def test_clang_uses_llvm_cov_on_path(self) -> None:
        tools = FakeTools("llvm-cov")
        self.assertEqual(self.command("Clang", "cc", tools), ["llvm-cov", "gcov"])

    def test_apple_clang_uses_the_llvm_gcov_wrapper(self) -> None:
        tools = FakeTools("gcov", version="Apple LLVM version 21.0.0")
        self.assertEqual(self.command("AppleClang", "/usr/bin/cc", tools), ["gcov"])

    def test_clang_falls_back_to_xcrun(self) -> None:
        tools = FakeTools("gcov", "xcrun", version="gcov (GCC) 13 found")
        self.assertEqual(
            self.command("AppleClang", "cc", tools), ["xcrun", "llvm-cov", "gcov"]
        )

    def test_clang_without_llvm_cov(self) -> None:
        with self.assertRaisesRegex(collect.CollectError, "llvm-cov is not on PATH"):
            self.command("Clang", "clang", FakeTools("xcrun"))

    def test_gcc_prefers_the_gcov_named_like_the_compiler(self) -> None:
        tools = FakeTools("gcov-13", "gcov")
        self.assertEqual(self.command("GNU", "gcc-13", tools), ["gcov-13"])
        self.assertEqual(self.command("GNU", "c++", tools), ["gcov"])
        self.assertEqual(self.command("", "", tools), ["gcov"])

    def test_gcc_without_gcov(self) -> None:
        with self.assertRaisesRegex(collect.CollectError, "gcov is not on PATH"):
            self.command("GNU", "gcc", FakeTools())


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.root = base / "repo"
        self.build = base / "build"
        self.report = base / "report"
        (self.root / "src").mkdir(parents=True)
        objects = self.build / "obj"
        objects.mkdir(parents=True)
        (objects / "calc.c.gcda").write_bytes(b"")
        (objects / "main.c.gcda").write_bytes(b"")

    def gcov_output(self) -> str:
        return (
            "        -:    0:Source:../repo/src/calc.c\n        1:    1:int a;\n"
            f"        -:    0:Source:{self.build}/generated.c\n        1:    1:x\n"
            "        -:    0:Source:/usr/include/stdio.h\n        1:    2:y\n"
        )

    def test_kept_records_are_under_the_root_with_absolute_paths(self) -> None:
        text = collect.kept_records(self.gcov_output(), self.build, self.root)
        source = Path(os.path.realpath(self.root / "src" / "calc.c"))
        self.assertEqual(
            text, f"        -:    0:Source:{source}\n        1:    1:int a;\n"
        )
        self.assertEqual(collect.kept_records("noise\n", self.build, self.root), "")

    def test_a_build_directory_inside_the_root_is_left_out(self) -> None:
        build = self.root / "build"
        text = (
            f"        -:    0:Source:{build}/gen.c\n        1:    1:x\n"
            "        -:    0:Source:src/a.c\n        1:    1:y\n"
        )
        kept = collect.kept_records(text, self.root / "out", self.root)
        self.assertNotIn("gen.c", collect.kept_records(text, build, self.root))
        self.assertIn("gen.c", kept)

    def test_collect_writes_one_report_per_data_file(self) -> None:
        tools = FakeTools(output=self.gcov_output())
        count = collect.collect(self.build, self.root, self.report, ["gcov"], tools.run)
        self.assertEqual(count, 2)
        self.assertEqual(
            sorted(path.name for path in self.report.iterdir()),
            ["00000.gcov", "00001.gcov"],
        )
        self.assertEqual(tools.commands[0][:4], ["gcov", "-b", "-c", "-t"])

    def test_collect_errors(self) -> None:
        tools = FakeTools(output="        -:    0:Source:/elsewhere/a.c\n")
        with self.assertRaisesRegex(collect.CollectError, "names no file under"):
            collect.collect(self.build, self.root, self.report, ["gcov"], tools.run)
        tools.returncode = 3
        with self.assertRaisesRegex(collect.CollectError, "gcov failed: bad data"):
            collect.collect(self.build, self.root, self.report, ["gcov"], tools.run)
        with self.assertRaisesRegex(collect.CollectError, "no .gcda coverage data"):
            collect.collect(self.root, self.root, self.report, ["gcov"], tools.run)

    def test_last_line_of_a_silent_failure(self) -> None:
        result = completed(["gcov"], 5)
        self.assertEqual(collect._last_line(result), "exit 5")

    def test_main(self) -> None:
        tools = FakeTools("gcov", output=self.gcov_output())
        arguments = [str(self.build), str(self.root), str(self.report)]
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(collect.main(arguments, tools.run, tools.which), 0)
            missing = FakeTools().which
            self.assertEqual(collect.main(arguments, tools.run, missing), 1)
            self.assertEqual(collect.main([], tools.run, tools.which), 2)
        lines = printed.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("wrote 2 gcov reports"))
        self.assertEqual(lines[1:], ["gcov is not on PATH", USAGE])

    def test_run_tool_reports_a_tool_that_cannot_start(self) -> None:
        result = collect.run_tool([sys.executable, "-c", "print('ok')"], self.root)
        self.assertEqual(result.stdout.strip(), "ok")
        with self.assertRaisesRegex(collect.CollectError, "did not run"):
            collect.run_tool([str(self.root / "missing-tool")], self.root)

    def test_script_runs_standalone(self) -> None:
        script = SCRIPTS / "gate_coverage_gcov_collect.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), USAGE)


if __name__ == "__main__":
    unittest.main()
