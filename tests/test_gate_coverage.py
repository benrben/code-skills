from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import gate_coverage  # noqa: E402
import gate_coverage_paths as paths  # noqa: E402
import gate_coverage_programs as programs  # noqa: E402
import gate_coverage_python as python_finder  # noqa: E402
import gate_coverage_readers as readers  # noqa: E402
import gate_coverage_recipes as recipes  # noqa: E402
from gate_types import FunctionInfo, ToolPython  # noqa: E402
from quality_commands import runner_for  # noqa: E402

# --- Recorded sample reports -------------------------------------------------

COVERAGE_PY_JSON = json.dumps(
    {
        "meta": {"format": 3, "version": "7.10.2", "branch_coverage": True},
        "files": {
            "/repo/app/calc.py": {
                "executed_lines": [1, 2, 3, 5],
                "missing_lines": [4],
                "excluded_lines": [9],
                "executed_branches": [[2, 3]],
                "missing_branches": [[2, 4]],
                "summary": {"percent_covered": 70.0},
            }
        },
        "totals": {"percent_covered": 70.0},
    }
)

ISTANBUL_JSON = json.dumps(
    {
        "/repo/src/math.js": {
            "path": "/repo/src/math.js",
            "statementMap": {
                "0": {"start": {"line": 1, "column": 0}, "end": {"line": 1}},
                "1": {"start": {"line": 2, "column": 2}, "end": {"line": 2}},
                "2": {"start": {"line": 3, "column": 2}, "end": {"line": 3}},
            },
            "s": {"0": 1, "1": 4, "2": 0},
            "branchMap": {
                "0": {
                    "loc": {"start": {"line": 2, "column": 2}},
                    "locations": [{"start": {"line": 2}}, {"start": {"line": 2}}],
                },
                "1": {"locations": [{"start": {"line": 3}}]},
                "2": {"locations": [{"start": {"line": 4}}]},
            },
            "b": {"0": [4, 0], "1": [1], "2": []},
        },
        "not-a-file": 3,
    }
)

LCOV = """TN:
SF:/repo/src/math.js
FN:1,add
FNDA:2,add
DA:1,2
DA:2,2
DA:3,0
BRDA:2,0,0,2
BRDA:2,0,1,0
BRDA:5,1,0,-
BRF:3
BRH:1
LF:3
LH:2
end_of_record
SF:src/other.js
DA:1,1
end_of_record
"""

COBERTURA = """<?xml version="1.0" encoding="utf-8"?>
<coverage line-rate="0.75" branch-rate="0.5" version="1.9" timestamp="1700000000">
  <sources>
    <source>/repo/</source>
  </sources>
  <packages>
    <package name="Calc" line-rate="0.75" branch-rate="0.5" complexity="2">
      <classes>
        <class name="Calc.Math" filename="src/Calc/Math.cs" line-rate="0.75">
          <methods>
            <method name="Abs" signature="(System.Int32)" line-rate="0.75">
              <lines>
                <line number="6" hits="2" branch="true" condition-coverage="50% (1/2)" />
              </lines>
            </method>
          </methods>
          <lines>
            <line number="5" hits="2" branch="false" />
            <line number="6" hits="2" branch="true" condition-coverage="50% (1/2)">
              <conditions><condition number="7" type="jump" coverage="50%" /></conditions>
            </line>
            <line number="7" hits="0" branch="false" />
            <line number="8" hits="2.0" branch="false" />
          </lines>
        </class>
        <class name="Calc.Abs" filename="/abs/elsewhere/Other.cs" />
        <class name="NoFile" filename="" />
      </classes>
    </package>
  </packages>
</coverage>
"""

JACOCO = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
    '<report name="demo"><sessioninfo id="host-1" start="1" dump="2"/>'
    '<package name="com/example"><class name="com/example/Calc" '
    'sourcefilename="Calc.java"><method name="abs" desc="(I)I" line="3">'
    '<counter type="INSTRUCTION" missed="2" covered="5"/></method></class>'
    '<sourcefile name="Calc.java">'
    '<line nr="3" mi="0" ci="3" mb="1" cb="1"/>'
    '<line nr="4" mi="2" ci="0" mb="0" cb="0"/>'
    '<line nr="5" mi="0" ci="2" mb="0" cb="0"/>'
    '<line nr="9" mi="0" ci="0" mb="0" cb="0"/>'
    '<counter type="LINE" missed="1" covered="2"/></sourcefile></package>'
    '<package name=""><sourcefile name="Main.java">'
    '<line nr="1" mi="0" ci="1" mb="0" cb="0"/></sourcefile></package>'
    "</report>"
)

GO_COVER = """mode: atomic
example.com/demo/calc/calc.go:3.24,4.12 1 3
example.com/demo/calc/calc.go:4.12,6.3 1 0
example.com/demo/calc/calc.go:7.2,7.10 1 3
example.com/demo/main.go:5.13,7.2 0 0
not a coverage line
"""

LLVM_COV_JSON = json.dumps(
    {
        "type": "llvm.coverage.json.export",
        "version": "2.0.1",
        "data": [
            {
                "files": [
                    {
                        "filename": "/repo/Sources/Calc/Calc.swift",
                        "segments": [
                            [1, 30, 3, True, True, False],
                            [2, 8, 3, True, False, False],
                            [2, 12, 1, True, True, False],
                            [4, 6, 3, True, False, False],
                            [4, 12, 0, True, True, False],
                            [6, 6, 3, True, False, False],
                            [7, 2, 0, False, False, False],
                            [9, 1, 0, False, True],
                            [10, 1, 0, False, False],
                        ],
                        "branches": [[2, 8, 2, 12, 1, 2, 0, 0, 4]],
                    },
                    {"filename": "/repo/Sources/Calc/Empty.swift", "segments": []},
                ]
            }
        ],
    }
)

RUBY_COVERAGE = json.dumps(
    {
        "files": {
            "/repo/lib/calc.rb": {
                "lines": [1, 1, 0, None, 1],
                "branches": [[2, 1, 1], [2, 2, 0]],
            },
            "/repo/lib/empty.rb": {"lines": None, "branches": None},
        }
    }
)


# coverage.py's default exclusion comment, in capitals; built from parts so
# this file never contains the marker the shortcuts gate looks for.
HIDING_COMMENT = "# PRAGMA" + ": NO COVER"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def file_coverage(lines=None, branches=None):
    return readers.FileCoverage(dict(lines or {}), dict(branches or {}))


# --- Readers -----------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def test_coverage_py_json_groups_branch_arcs_by_source_line(self):
        raw = readers.parse_coverage_py_json(COVERAGE_PY_JSON)
        calc = raw["/repo/app/calc.py"]
        self.assertEqual(calc.lines, {1: 1, 2: 1, 3: 1, 5: 1, 4: 0})
        self.assertEqual(calc.branches, {2: (1, 2)})

    def test_json_parsers_reject_other_formats(self):
        llvm_export = json.dumps({"type": "llvm.coverage.json.export", "data": []})
        cases = [
            (readers.parse_coverage_py_json, "[]"),
            (readers.parse_coverage_py_json, llvm_export),
            (readers.parse_coverage_py_json, RUBY_COVERAGE),
            (readers.parse_llvm_cov_json, COVERAGE_PY_JSON),
            (readers.parse_ruby_coverage, llvm_export),
            (readers.parse_istanbul_json, COVERAGE_PY_JSON),
            (readers.parse_istanbul_json, "[]"),
        ]
        for parser, text in cases:
            with self.assertRaises(readers.CoverageReportError, msg=parser.__name__):
                parser(text)
        self.assertEqual(readers.parse_istanbul_json("{}"), {})

    def test_xml_parsers_reject_other_formats(self):
        with self.assertRaisesRegex(readers.CoverageReportError, "Cobertura"):
            readers.parse_cobertura(JACOCO)
        with self.assertRaisesRegex(readers.CoverageReportError, "JaCoCo"):
            readers.parse_jacoco(COBERTURA)

    def test_istanbul_uses_statement_lines_and_branch_locations(self):
        raw = readers.parse_istanbul_json(ISTANBUL_JSON)
        math = raw["/repo/src/math.js"]
        self.assertEqual(math.lines, {1: 1, 2: 4, 3: 0})
        self.assertEqual(math.branches, {2: (1, 2), 3: (1, 1)})
        self.assertEqual(set(raw), {"/repo/src/math.js"})

    def test_lcov_reads_lines_and_branches_per_file(self):
        raw = readers.parse_lcov(LCOV)
        math = raw["/repo/src/math.js"]
        self.assertEqual(math.lines, {1: 2, 2: 2, 3: 0})
        self.assertEqual(math.branches, {2: (1, 2), 5: (0, 1)})
        self.assertEqual(raw["src/other.js"].lines, {1: 1})

    def test_repeated_lcov_records_merge_like_separate_reports(self):
        first = "SF:a.js\nDA:2,1\nBRDA:2,0,0,1\nBRDA:2,0,1,0\nend_of_record\n"
        second = "SF:a.js\nDA:2,0\nBRDA:2,0,0,0\nBRDA:2,0,1,3\nend_of_record\n"
        merged = readers.parse_lcov(first + second)["a.js"]
        self.assertEqual(merged.branches, {2: (2, 2)})
        self.assertEqual(merged.lines, {2: 1})

    def test_lcov_ignores_records_before_a_source_file(self):
        self.assertEqual(readers.parse_lcov("DA:1,1\nBRDA:1,0,0,1\n"), {})

    def test_cobertura_joins_single_source_and_reads_conditions(self):
        raw = readers.parse_cobertura(COBERTURA)
        math = raw["/repo/src/Calc/Math.cs"]
        self.assertEqual(math.lines, {5: 2, 6: 2, 7: 0, 8: 2})
        self.assertEqual(math.branches, {6: (1, 2)})
        self.assertIn("/abs/elsewhere/Other.cs", raw)

    def test_cobertura_with_several_sources_keeps_relative_names(self):
        text = COBERTURA.replace(
            "<source>/repo/</source>", "<source>/a</source><source>/b</source>"
        )
        self.assertIn("src/Calc/Math.cs", readers.parse_cobertura(text))

    def test_jacoco_reads_package_paths_instructions_and_branches(self):
        raw = readers.parse_jacoco(JACOCO)
        calc = raw["com/example/Calc.java"]
        self.assertEqual(calc.lines, {3: 3, 4: 0, 5: 2})
        self.assertEqual(calc.branches, {3: (1, 2)})
        self.assertEqual(raw["Main.java"].lines, {1: 1})

    def test_go_cover_strips_module_and_spreads_block_counts(self):
        raw = readers.parse_go_cover(GO_COVER, "example.com/demo")
        calc = raw["calc/calc.go"]
        self.assertEqual(calc.lines, {3: 3, 4: 3, 5: 0, 6: 0, 7: 3})
        self.assertNotIn("main.go", raw)

    def test_go_cover_without_module_keeps_import_paths(self):
        raw = readers.parse_go_cover(GO_COVER)
        self.assertIn("example.com/demo/calc/calc.go", raw)

    def test_go_module_path(self):
        self.assertEqual(
            readers.go_module_path('// c\nmodule "example.com/x"\n'), "example.com/x"
        )
        self.assertIsNone(readers.go_module_path("go 1.22\n"))

    def test_llvm_cov_follows_llvm_line_rules(self):
        raw = readers.parse_llvm_cov_json(LLVM_COV_JSON)
        calc = raw["/repo/Sources/Calc/Calc.swift"]
        self.assertEqual(calc.lines, {1: 3, 2: 3, 3: 1, 4: 1, 5: 0, 6: 0, 7: 3})
        self.assertEqual(calc.branches, {2: (2, 2)})
        self.assertEqual(raw["/repo/Sources/Calc/Empty.swift"].lines, {})

    def test_ruby_coverage_skips_nil_lines(self):
        raw = readers.parse_ruby_coverage(RUBY_COVERAGE)
        calc = raw["/repo/lib/calc.rb"]
        self.assertEqual(calc.lines, {1: 1, 2: 1, 3: 0, 5: 1})
        self.assertEqual(calc.branches, {2: (1, 2)})
        self.assertEqual(raw["/repo/lib/empty.rb"].lines, {})

    def test_merge_keeps_best_evidence(self):
        first = file_coverage({1: 0, 2: 3}, {2: (1, 2)})
        first.merge(file_coverage({1: 1, 3: 0}, {2: (2, 2), 4: (0, 2)}))
        self.assertEqual(first.lines, {1: 1, 2: 3, 3: 0})
        self.assertEqual(first.branches, {2: (2, 2), 4: (0, 2)})

    def test_merging_ruby_processes_unions_the_outcomes_taken(self):
        def process(then_hits, else_hits):
            branches = [[2, 1, then_hits], [2, 2, else_hits]]
            files = {"/repo/lib/calc.rb": {"lines": [1, 1], "branches": branches}}
            return readers.parse_ruby_coverage(json.dumps({"files": files}))

        merged = readers.FileCoverage()
        merged.merge(process(1, 0)["/repo/lib/calc.rb"])
        merged.merge(process(0, 1)["/repo/lib/calc.rb"])
        self.assertEqual(merged.branches, {2: (2, 2)})

    def test_named_outcomes_union_across_every_format_that_names_them(self):
        taken_else = COVERAGE_PY_JSON.replace(
            '"executed_branches": [[2, 3]], "missing_branches": [[2, 4]]',
            '"executed_branches": [[2, 4]], "missing_branches": [[2, 3]]',
        )
        self.assertNotEqual(taken_else, COVERAGE_PY_JSON)
        merged = readers.FileCoverage()
        for text in (COVERAGE_PY_JSON, taken_else):
            merged.merge(readers.parse_coverage_py_json(text)["/repo/app/calc.py"])
        self.assertEqual(merged.branches, {2: (2, 2)})

    def test_count_only_branches_merge_as_a_lower_bound(self):
        named = file_coverage()
        named.add_outcome(3, "a", 1)
        named.merge(file_coverage({}, {3: (1, 4), 5: (1, 2)}))
        self.assertEqual(named.branches, {3: (1, 4), 5: (1, 2)})

    def test_guess_format_by_name_and_content(self):
        named = {
            "lcov.info": "lcov",
            "cover.out": "go-cover",
            "coverage-final.json": "istanbul-json",
        }
        for name, expected in named.items():
            self.assertEqual(readers.guess_format(Path(name)), expected, name)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sniffed = {
                "report.xml": (JACOCO, "jacoco"),
                "coverage.xml": (COBERTURA, "cobertura"),
                "broken.xml": ("<coverage", "cobertura"),
                "coverage.json": (COVERAGE_PY_JSON, "coverage-json"),
                "swift-coverage.json": (LLVM_COV_JSON, "llvm-cov-json"),
                "ruby.json": (RUBY_COVERAGE, "ruby-coverage"),
                "istanbul.json": (ISTANBUL_JSON, "istanbul-json"),
                "broken.json": ("{", "coverage-json"),
            }
            for name, (text, expected) in sniffed.items():
                found = readers.guess_format(write(folder / name, text))
                self.assertEqual(found, expected, name)

    def test_guess_format_refuses_unknown_content(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name, text in (("x.json", "[1]"), ("y.json", "{}"), ("z.xml", "<a/>")):
                with self.assertRaisesRegex(readers.CoverageReportError, "format"):
                    readers.guess_format(write(folder / name, text))
            with self.assertRaisesRegex(readers.CoverageReportError, "format"):
                readers.guess_format(
                    write(folder / "results" / "run.trx", "<x/>").parent
                )


class PathResolutionTests(unittest.TestCase):
    def resolver(self, root, *known):
        return paths.ReportPathResolver(root, known)

    def test_segment_suffix_never_matches_inside_a_name(self):
        root = Path("/repo")
        self.assertIsNone(self.resolver(root, "app/studio.py").by_suffix("io.py"))
        self.assertIsNone(self.resolver(root, "io.py").by_suffix("/x/studio.py"))

    def test_strips_root_and_normalizes_separators(self):
        resolver = self.resolver(Path("/repo"), "src/a.py", ".github/x.py")
        self.assertEqual(resolver.exact("/repo/src/a.py"), "src/a.py")
        self.assertEqual(resolver.exact("file:///repo/src/a.py"), "src/a.py")
        self.assertEqual(resolver.exact(".\\src\\a.py"), "src/a.py")
        self.assertEqual(resolver.exact("./.github/x.py"), ".github/x.py")
        self.assertIsNone(resolver.exact("/other/src/a.py"))

    def test_longest_unique_suffix_wins(self):
        resolver = self.resolver(Path("/repo"), "src/util.py", "util.py")
        self.assertEqual(resolver.by_suffix("/ci/build/src/util.py"), "src/util.py")
        self.assertEqual(resolver.by_suffix("/ci/build/util.py"), "util.py")

    def test_shorter_report_path_needs_a_unique_repository_file(self):
        unique = self.resolver(Path("/repo"), "src/main/java/com/x/Calc.java")
        self.assertEqual(
            unique.by_suffix("com/x/Calc.java"), "src/main/java/com/x/Calc.java"
        )
        ambiguous = self.resolver(
            Path("/repo"),
            "a/src/main/java/com/x/Calc.java",
            "b/src/main/java/com/x/Calc.java",
        )
        self.assertIsNone(ambiguous.by_suffix("com/x/Calc.java"))

    def test_kotlin_sources_without_the_root_package_directories(self):
        report = readers.parse_jacoco(
            '<report name="k"><package name="org/example/app/network">'
            '<sourcefile name="Socket.kt"><line nr="3" mi="0" ci="2"/></sourcefile>'
            '</package><package name="org/example/app">'
            '<sourcefile name="Main.kt"><line nr="1" mi="1" ci="0"/></sourcefile>'
            "</package></report>"
        )
        known = ["src/main/kotlin/network/Socket.kt", "src/main/kotlin/Main.kt"]
        data = readers.resolve_report_paths(report, Path("/repo"), known)
        self.assertEqual(data["src/main/kotlin/network/Socket.kt"].lines, {3: 2})
        self.assertEqual(data["src/main/kotlin/Main.kt"].lines, {1: 0})

    def test_partial_tail_matches_stay_unique_and_need_a_directory_outside(self):
        resolver = self.resolver(Path("/repo"), "a/net/Socket.kt", "b/net/Socket.kt")
        self.assertIsNone(resolver.by_suffix("org/x/net/Socket.kt"))
        single = self.resolver(Path("/repo"), "src/util.py")
        self.assertIsNone(single.by_suffix("/site-packages/dep/util.py"))
        self.assertIsNone(single.by_suffix("C:/deps/lib/util.py"))
        self.assertEqual(single.by_suffix("dep/util.py"), "src/util.py")
        nested = self.resolver(Path("/repo"), "app/net/http.py")
        self.assertEqual(nested.by_suffix("/ci/lib/net/http.py"), "app/net/http.py")

    def test_empty_and_excluded_candidates(self):
        resolver = self.resolver(Path("/repo"), "src/a.py")
        self.assertIsNone(resolver.by_suffix("/repo/"))
        self.assertIsNone(resolver.by_suffix("x/src/a.py", excluded=["src/a.py"]))

    def test_exact_paths_win_over_dependency_suffixes(self):
        raw = {
            "/repo/src/lib.rs": file_coverage({1: 1}),
            "/home/u/.cargo/registry/dep/src/lib.rs": file_coverage({1: 0, 2: 0}),
            "/elsewhere/pkg/a.py": file_coverage({3: 1}),
            "/one/b.py": file_coverage({1: 1}),
            "/two/b.py": file_coverage({1: 0}),
        }
        known = ["src/lib.rs", "pkg/a.py", "b.py"]
        data = readers.resolve_report_paths(raw, Path("/repo"), known)
        self.assertEqual(data["src/lib.rs"].lines, {1: 1})
        self.assertEqual(data["pkg/a.py"].lines, {3: 1})
        self.assertNotIn("b.py", data)

    def test_resolved_root_is_stripped_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resolved = root.resolve().as_posix() + "/a.py"
            resolver = paths.ReportPathResolver(root, ["a.py"])
            self.assertEqual(resolver.exact(resolved), "a.py")


class ReadReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def test_reads_a_report_and_keys_it_by_repository_path(self):
        write(self.root / "app" / "calc.py", "x = 1\n")
        report = write(
            self.root / "out" / "coverage.json",
            COVERAGE_PY_JSON.replace("/repo/", f"{self.root}/"),
        )
        data = readers.read_coverage_report(report, "coverage-json", self.root)
        self.assertEqual(set(data), {"app/calc.py"})

    def test_auto_format_and_explicit_known_paths(self):
        report = write(self.root / "lcov.info", LCOV)
        data = readers.read_coverage_report(
            report, "auto", self.root, ["src/math.js", "src/other.js"]
        )
        self.assertEqual(set(data), {"src/math.js", "src/other.js"})

    def test_go_profile_uses_go_mod_module_path(self):
        write(self.root / "go.mod", "module example.com/demo\n\ngo 1.22\n")
        report = write(self.root / "cover.out", GO_COVER)
        data = readers.read_coverage_report(
            report, "go-cover", self.root, ["calc/calc.go"]
        )
        self.assertEqual(data["calc/calc.go"].lines[5], 0)

    def test_go_profile_without_go_mod(self):
        report = write(self.root / "cover.out", GO_COVER)
        data = readers.read_coverage_report(
            report, "go-cover", self.root, ["demo/calc/calc.go"]
        )
        self.assertIn("demo/calc/calc.go", data)

    def test_report_directory_merges_every_matching_file(self):
        first = COBERTURA.replace("<source>/repo/</source>", "")
        second = first.replace('hits="0"', 'hits="5"')
        write(self.root / "results" / "a" / "coverage.cobertura.xml", first)
        write(self.root / "results" / "b" / "coverage.cobertura.xml", second)
        write(self.root / "results" / "notes.txt", "ignored")
        data = readers.read_coverage_report(
            self.root / "results", "cobertura", self.root, ["src/Calc/Math.cs"]
        )
        self.assertEqual(data["src/Calc/Math.cs"].lines[7], 5)

    def test_report_directory_without_matching_files_is_an_error(self):
        write(self.root / "results" / "abc" / "run.trx", "<TestRun/>")
        with self.assertRaisesRegex(readers.CoverageReportError, "no \\*.xml"):
            readers.read_coverage_report(
                self.root / "results", "cobertura", self.root, ["A.cs"]
            )
        self.assertFalse(readers.report_is_fresh(self.root / "results", 0, "cobertura"))

    def test_report_directory_reads_only_files_this_run_wrote(self):
        hit_all = "SF:a.js\nDA:1,1\nDA:2,1\nend_of_record\n"
        fresh = "SF:a.js\nDA:1,1\nDA:2,0\nend_of_record\n"
        old = write(self.root / "coverage" / "zz-old.info", hit_all)
        write(self.root / "coverage" / "lcov.info", fresh)
        started = time.time()
        os.utime(old, (started - 100, started - 100))
        data = readers.read_coverage_report(
            self.root / "coverage", "lcov", self.root, ["a.js"], started_at=started
        )
        self.assertEqual(data["a.js"].lines, {1: 1, 2: 0})
        with self.assertRaisesRegex(readers.CoverageReportError, "written by this run"):
            readers.read_coverage_report(
                self.root / "coverage", "lcov", self.root, ["a.js"], started + 100
            )

    def test_auto_format_reads_a_wrong_named_llvm_export(self):
        report = write(self.root / "swift-coverage.json", LLVM_COV_JSON)
        data = readers.read_coverage_report(
            report, "auto", self.root, ["Sources/Calc/Calc.swift"]
        )
        self.assertEqual(data["Sources/Calc/Calc.swift"].lines[5], 0)

    def test_wrong_explicit_format_is_an_error_not_an_empty_report(self):
        report = write(self.root / "swift-coverage.json", LLVM_COV_JSON)
        with self.assertRaisesRegex(readers.CoverageReportError, "coverage.py"):
            readers.read_coverage_report(report, "coverage-json", self.root, [])

    def test_errors_are_reported_as_coverage_report_errors(self):
        broken_xml = write(self.root / "broken.xml", "<coverage>")
        broken_json = write(self.root / "broken.json", "{")
        cases = [
            (self.root / "missing.json", "coverage-json"),
            (broken_xml, "cobertura"),
            (broken_json, "coverage-json"),
            (broken_json, "no-such-format"),
        ]
        for path, format_name in cases:
            with self.assertRaises(readers.CoverageReportError, msg=format_name):
                readers.read_coverage_report(path, format_name, self.root, [])

    def test_unknown_format_message(self):
        with self.assertRaisesRegex(readers.CoverageReportError, "unknown"):
            readers.parse_report_text("", "nope", self.root)

    def test_report_freshness_for_files_and_directories(self):
        report = write(self.root / "r" / "lcov.info", LCOV)
        old = time.time() - 100
        os.utime(report, (old, old))
        self.assertFalse(readers.report_is_fresh(report, old + 3))
        self.assertTrue(readers.report_is_fresh(report, old))
        self.assertFalse(readers.report_is_fresh(self.root / "r", old + 3, "lcov"))
        self.assertTrue(readers.report_is_fresh(self.root / "r", old - 1, "lcov"))
        self.assertFalse(readers.report_is_fresh(self.root / "r", old - 1, "jacoco"))
        self.assertFalse(readers.report_is_fresh(self.root / "nothing", 0))
        with mock.patch.object(Path, "is_file", return_value=True):
            self.assertFalse(readers.report_is_fresh(self.root / "vanished", 0))

    def test_coarse_timestamps_still_count_as_fresh(self):
        report = write(self.root / "lcov.info", LCOV)
        started = time.time()
        truncated = float(int(started)) - 0.5
        os.utime(report, (truncated, truncated))
        self.assertTrue(readers.report_is_fresh(report, started))


# --- Recipes -----------------------------------------------------------------


class RecipeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / ".quality" / "coverage"
        self.addCleanup(self.temp.cleanup)
        self.tools = recipes.CoverageTools(
            tool_python=ToolPython(sys.executable, {"PYTHONPATH": "/cache/py"}),
            c8="/cache/node/bin/c8",
            jacoco_agent="/cache/jacocoagent.jar",
            jacoco_cli="/cache/jacococli.jar",
            cargo_llvm_cov="/cache/cargo-llvm-cov",
        )

    def plan(self, *command, tools=None, configured=None, environ=None):
        return recipes.plan_coverage(
            self.root,
            command,
            self.out,
            tools or self.tools,
            configured,
            environ if environ is not None else {},
        )

    def recipe(self, *command, **options):
        result = self.plan(*command, **options)
        self.assertIsInstance(result, recipes.Recipe, result)
        return result

    def run_command(self, command, env=None):
        return subprocess.run(
            list(command),
            cwd=self.root,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )


def which_only(**found):
    """A ``shutil.which`` stand-in that knows only the given programs."""
    return lambda name: found.get(name.replace(".", "_"))


class PythonRecipeTests(RecipeTestCase):
    def test_test_command_runs_unchanged_under_a_startup_hook(self):
        recipe = self.recipe(".venv/bin/python", "-m", "pytest", "-q")
        self.assertEqual(recipe.command, (".venv/bin/python", "-m", "pytest", "-q"))
        startup = self.out / "python-startup"
        rcfile = self.out / "coveragerc"
        self.assertEqual(
            recipe.env,
            {"PYTHONPATH": str(startup), "COVERAGE_PROCESS_START": str(rcfile)},
        )
        self.assertEqual(recipe.format, "coverage-json")
        self.assertEqual(recipe.measures, frozenset({".py"}))
        self.assertEqual(recipe.finalize[0][:3], (".venv/bin/python", "-S", "-c"))
        self.assertEqual(
            recipe.finalize[0][-3:], (str(rcfile), str(recipe.report), str(startup))
        )
        hook = recipe.files[startup / "sitecustomize.py"]
        self.assertIn("sys.path.extend(['/cache/py'])", hook)
        compile(hook, "sitecustomize.py", "exec")

    def test_rcfile_lets_no_comment_hide_code(self):
        recipe = self.recipe("python", "-m", "pytest")
        rc = recipe.files[self.out / "coveragerc"]
        self.assertNotIn("patch", rc)
        self.assertNotIn("pragma", rc.lower())
        self.assertIn("exclude_lines =", rc)
        self.assertTrue(rc.endswith("partial_branches =\n"))
        self.assertIn(str(self.root), rc)
        self.assertIn(f"{self.out}/*", rc)

    def test_inherited_python_path_is_kept_after_the_startup_hook(self):
        recipe = self.recipe("pytest", environ={"PYTHONPATH": "src"})
        startup = str(self.out / "python-startup")
        self.assertEqual(recipe.env["PYTHONPATH"], f"{startup}{os.pathsep}src")

    def test_interpreter_options_and_wrappers_run_unchanged(self):
        command = ("python3", "-X", "dev", "-m", "pytest")
        self.assertEqual(self.recipe(*command).command, command)
        for wrapper in ("uv", "poetry", "pdm", "hatch", "rye"):
            recipe = self.recipe(wrapper, "run", "pytest", "-q")
            self.assertEqual(recipe.command, (wrapper, "run", "pytest", "-q"))
            self.assertEqual(recipe.finalize[0][:4], (wrapper, "run", "python", "-S"))

    def test_commands_whose_runtime_is_unknown_have_no_recipe(self):
        for command in (("tox",), ("make", "test"), ("hatch", "test")):
            self.assertIn("no coverage recipe", self.plan(*command))

    def test_pytest_executable_uses_the_python_beside_it(self):
        write(self.root / "env" / "bin" / "python", "")
        recipe = self.recipe("env/bin/pytest", "-x")
        self.assertEqual(recipe.command, ("env/bin/pytest", "-x"))
        self.assertEqual(recipe.finalize[0][0], "env/bin/python")

    def test_bare_pytest_uses_the_interpreter_in_its_shebang(self):
        script = write(
            self.root / "bin" / "pytest", "#!/opt/pipx/venv/bin/python3.12\n"
        )
        with mock.patch.object(
            python_finder.shutil, "which", which_only(pytest=str(script))
        ):
            recipe = self.recipe("pytest", "-q")
        self.assertEqual(recipe.command, ("pytest", "-q"))
        self.assertEqual(recipe.finalize[0][0], "/opt/pipx/venv/bin/python3.12")

    def test_env_shebang_is_resolved_on_path(self):
        script = write(self.root / "bin" / "py.test", "#!/usr/bin/env python3 -u\n")
        found = which_only(py_test=str(script), python3="/opt/py/bin/python3")
        with mock.patch.object(python_finder.shutil, "which", found):
            self.assertEqual(
                python_finder.python_interpreter(self.root, ("py.test",)),
                ("/opt/py/bin/python3",),
            )

    def test_non_python_shebangs_are_ignored(self):
        cases = ["#!/bin/sh\n", "#!/usr/bin/env\n", "no shebang\n"]
        for text in cases:
            script = write(self.root / "bin" / "pytest", text)
            self.assertIsNone(python_finder.shebang_python(self.root, str(script)))
        self.assertIsNone(python_finder.shebang_python(self.root, "./missing"))

    def test_bare_pytest_uses_the_project_virtual_environment(self):
        write(self.root / ".venv" / "bin" / "python", "")
        with mock.patch.object(python_finder.shutil, "which", which_only()):
            recipe = self.recipe("pytest")
        self.assertEqual(
            recipe.finalize[0][0], str(self.root / ".venv" / "bin" / "python")
        )

    def test_bare_pytest_falls_back_to_python_on_path(self):
        found = which_only(python3="/usr/bin/python3")
        with mock.patch.object(python_finder.shutil, "which", found):
            recipe = self.recipe("pytest", tools=recipes.CoverageTools())
        self.assertEqual(recipe.finalize[0][0], "/usr/bin/python3")
        hook = recipe.files[self.out / "python-startup" / "sitecustomize.py"]
        self.assertIn("sys.path.extend([])", hook)

    def test_missing_interpreter_is_explained(self):
        with mock.patch.object(python_finder.shutil, "which", return_value=None):
            result = self.plan("pytest")
        self.assertIn("no Python interpreter", result)

    def bare_venv(self):
        """A virtual environment without coverage.py and with a read-only
        site-packages, like a system or Nix interpreter."""
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        venv = folder / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
        )
        python = next(
            path
            for path in (venv / "bin" / "python", venv / "Scripts" / "python.exe")
            if path.exists()
        )
        site_packages = [path for path in venv.rglob("site-packages") if path.is_dir()]
        for path in site_packages:
            path.chmod(0o555)
            self.addCleanup(path.chmod, 0o755)
        return str(python), site_packages

    def write_project(self):
        write(
            self.root / "src" / "calc.py",
            "def sign(value):\n"
            "    if value < 0:\n"
            "        return -1\n"
            "    return 1\n\n\n"
            "def hidden(value):  " + HIDING_COMMENT + "\n"
            "    return value\n",
        )
        write(
            self.root / "src" / "sitecustomize.py",
            "import os\nos.environ['PROJECT_SITE'] = 'ran'\n",
        )
        write(self.root / "tests" / "__init__.py", "")
        write(
            self.root / "tests" / "test_calc.py",
            "import os, subprocess, sys, unittest\n"
            "import calc\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_positive(self):\n"
            "        self.assertEqual(calc.sign(2), 1)\n"
            "        self.assertEqual(os.environ.get('PROJECT_SITE'), 'ran')\n\n"
            "    def test_negative_in_a_subprocess(self):\n"
            "        code = 'import calc; print(calc.sign(-3))'\n"
            "        out = subprocess.run([sys.executable, '-c', code],\n"
            "                             stdout=subprocess.PIPE, text=True)\n"
            "        self.assertEqual(out.stdout.strip(), '-1')\n",
        )

    def test_end_to_end_with_coverage_only_on_the_tool_path(self):
        python, site_packages = self.bare_venv()
        before = {path: sorted(path.iterdir()) for path in site_packages}
        self.write_project()
        coverage_package = importlib.util.find_spec("coverage")
        self.assertIsNotNone(coverage_package)
        tool_path = str(Path(coverage_package.origin).resolve().parents[1])
        tools = recipes.CoverageTools(
            tool_python=ToolPython(sys.executable, {"PYTHONPATH": tool_path})
        )
        command = (python, "-m", "unittest", "discover", "-s", "tests", "-t", ".")
        recipe = self.recipe(*command, tools=tools, environ={"PYTHONPATH": "src"})
        recipes.prepare_recipe(recipe)
        run = runner_for(self.root)
        for step in (*recipe.setup, recipe.command):
            result = run(step, 120, recipe.env)
            self.assertEqual(result.returncode, 0, result.output)
        result = run(recipe.finalize[0], 120)
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(
            before, {path: sorted(path.iterdir()) for path in site_packages}
        )
        data = readers.read_coverage_report(recipe.report, recipe.format, self.root)
        self.assertEqual(
            data["src/calc.py"].lines, {1: 1, 2: 1, 3: 1, 4: 1, 7: 1, 8: 0}
        )
        self.assertEqual(data["src/calc.py"].branches, {2: (2, 2)})
        self.assertNotIn("tests/test_calc.py", data)
        outcome = gate_coverage.evaluate_coverage(
            self.root,
            [self.root / "src" / "calc.py"],
            diff_scope.repository_changes(),
            data,
            [FunctionInfo("src/calc.py", "hidden", "hidden(value)", 7, 8)],
            {},
            report_fresh=readers.report_is_fresh(recipe.report, time.time() - 120),
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            (outcome.items[0].name, outcome.items[0].value), ("hidden", "8")
        )

    def test_a_test_that_deletes_the_code_it_ran_does_not_block_the_report(self):
        self.write_project()
        write(
            self.root / "tests" / "test_gone.py",
            "import importlib, os, sys, unittest\n\n\n"
            "class Gone(unittest.TestCase):\n"
            "    def test_runs_then_deletes_a_module(self):\n"
            "        with open('gone.py', 'w') as handle:\n"
            "            handle.write('def value():\\n    return 7\\n')\n"
            "        sys.path.insert(0, os.getcwd())\n"
            "        self.assertEqual(importlib.import_module('gone').value(), 7)\n"
            "        os.remove('gone.py')\n",
        )
        command = (
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
        )
        recipe = self.recipe(*command, environ={"PYTHONPATH": "src"})
        recipes.prepare_recipe(recipe)
        run = runner_for(self.root)
        for step in (*recipe.setup, recipe.command):
            result = run(step, 120, recipe.env)
            self.assertEqual(result.returncode, 0, result.output)
        result = run(recipe.finalize[0], 120)
        self.assertEqual(result.returncode, 0, result.output)
        data = readers.read_coverage_report(recipe.report, recipe.format, self.root)
        self.assertIn("src/calc.py", data)


class NodeRecipeTests(RecipeTestCase):
    def package(self, scripts=None, **dependencies):
        write(
            self.root / "package.json",
            json.dumps({"scripts": scripts or {}, "devDependencies": dependencies}),
        )

    def test_npm_jest_script_gets_jest_flags_after_separator(self):
        self.package({"test": "jest --runInBand"}, jest="29")
        recipe = self.recipe("npm", "test")
        self.assertEqual(recipe.command[:3], ("npm", "test", "--"))
        self.assertIn("--coverageProvider=v8", recipe.command)
        self.assertEqual(recipe.report, self.out / "node" / "lcov.info")
        self.assertEqual(recipe.format, "lcov")

    def test_pnpm_run_vitest_with_provider(self):
        self.package({"unit": "vitest run"}, **{"@vitest/coverage-v8": "1"})
        recipe = self.recipe("pnpm", "run", "unit")
        self.assertEqual(recipe.command[:3], ("pnpm", "run", "unit"))
        self.assertIn("--coverage.enabled", recipe.command)

    def test_vitest_without_provider_falls_back_to_c8(self):
        self.package({"test": "vitest run"})
        recipe = self.recipe("yarn", "test")
        self.assertEqual(recipe.command, ("yarn", "test"))
        self.assertEqual(
            recipe.env, {"NODE_V8_COVERAGE": str(self.out / "node" / "v8")}
        )
        self.assertEqual(recipe.finalize[0][:2], ("/cache/node/bin/c8", "report"))

    def test_direct_jest_and_existing_separator(self):
        recipe = self.recipe("node_modules/.bin/jest", "--ci")
        self.assertEqual(recipe.command[:2], ("node_modules/.bin/jest", "--ci"))
        self.assertIn("--coverage", recipe.command)
        self.package({"test": "jest"})
        recipe = self.recipe("npm", "test", "--", "--ci")
        self.assertEqual(recipe.command.count("--"), 1)

    def test_malformed_dependency_sections_are_ignored(self):
        write(
            self.root / "package.json",
            json.dumps({"scripts": {"test": "vitest"}, "dependencies": []}),
        )
        self.assertIn("NODE_V8_COVERAGE", self.recipe("npm", "test").env)

    def test_bun_test_runner(self):
        recipe = self.recipe("bun", "test")
        self.assertIn("--coverage-reporter=lcov", recipe.command)

    def test_node_test_without_c8_is_blocked(self):
        tools = recipes.CoverageTools(tool_python=ToolPython(sys.executable))
        self.assertIn("c8", self.plan("node", "--test", tools=tools))

    def test_npm_t_shorthand_and_unreadable_package(self):
        write(self.root / "package.json", "{broken")
        self.assertEqual(self.recipe("npm", "t").env["NODE_V8_COVERAGE"][-2:], "v8")
        write(self.root / "package.json", "[]")
        self.assertIn("NODE_V8_COVERAGE", self.recipe("npm", "--silent").env)
        self.package({"test": "jest"}, jest="29")
        self.assertIn("--coverage", self.recipe("npm", "t").command)


class OtherLanguageRecipeTests(RecipeTestCase):
    def test_go_adds_profile_flags_after_test(self):
        recipe = self.recipe("go", "test", "-json", "./...")
        report = self.out / "go-cover.out"
        self.assertEqual(
            recipe.command,
            (
                "go",
                "test",
                f"-coverprofile={report}",
                "-covermode=atomic",
                "-coverpkg=./...",
                "-json",
                "./...",
            ),
        )
        self.assertEqual(recipe.format, "go-cover")
        self.assertIn("go test", self.plan("go", "vet"))

    def test_rust_replaces_cargo_test_with_llvm_cov(self):
        recipe = self.recipe("cargo", "test", "--frozen", "--offline")
        self.assertEqual(
            recipe.command[:5],
            (
                "/cache/cargo-llvm-cov",
                "llvm-cov",
                "--lcov",
                "--output-path",
                str(self.out / "lcov.info"),
            ),
        )
        self.assertEqual(recipe.command[-2:], ("--frozen", "--offline"))
        self.assertIn("cargo test", self.plan("cargo", "build"))
        missing = recipes.CoverageTools()
        self.assertIn("cargo-llvm-cov", self.plan("cargo", "test", tools=missing))

    def test_jvm_attaches_jacoco_agent(self):
        recipe = self.recipe("./mvnw", "-o", "-q", "test")
        self.assertEqual(recipe.command, ("./mvnw", "-o", "-q", "test"))
        self.assertIn(
            "-javaagent:/cache/jacocoagent.jar", recipe.env["JAVA_TOOL_OPTIONS"]
        )
        self.assertEqual(recipe.format, "jacoco")
        self.assertEqual(recipe.measures, frozenset({".java", ".kt"}))
        self.assertIn(
            "JaCoCo", self.plan("gradle", "test", tools=recipes.CoverageTools())
        )
        kept = self.recipe("mvn", "test", environ={"JAVA_TOOL_OPTIONS": "-Xmx1g"})
        self.assertTrue(kept.env["JAVA_TOOL_OPTIONS"].startswith("-Xmx1g -javaagent:"))

    def test_dotnet_collects_cobertura_into_a_directory(self):
        recipe = self.recipe("dotnet", "test", "--no-restore")
        self.assertIn("--collect:XPlat Code Coverage", recipe.command)
        self.assertEqual(recipe.report, self.out / "dotnet")
        self.assertEqual(recipe.format, "cobertura")
        self.assertIn("coverlet.collector", recipe.prerequisite)
        self.assertIn("dotnet test", self.plan("dotnet", "build"))

    def test_swift_enables_code_coverage(self):
        recipe = self.recipe("swift", "test")
        self.assertEqual(recipe.command, ("swift", "test", "--enable-code-coverage"))
        self.assertEqual(recipe.format, "llvm-cov-json")
        self.assertIn("swift test", self.plan("swift", "build"))

    def test_php_unit_writes_cobertura(self):
        recipe = self.recipe("vendor/bin/phpunit")
        self.assertIn("--coverage-cobertura", recipe.command)
        self.assertEqual(recipe.env, {"XDEBUG_MODE": "coverage"})
        self.assertIn("pcov or Xdebug", recipe.prerequisite)
        self.assertEqual(self.recipe("php", "vendor/bin/phpunit").format, "cobertura")

    def test_ruby_loads_the_coverage_helper(self):
        recipe = self.recipe("bundle", "exec", "rspec")
        helper = self.out / "ruby_coverage.rb"
        self.assertEqual(recipe.env, {"RUBYOPT": f"-r{helper}"})
        self.assertIn(
            "Coverage.start(lines: true, branches: true)", recipe.files[helper]
        )
        self.assertIn("target[1], count]", recipe.files[helper])
        self.assertEqual(recipe.report, self.out / "ruby")
        self.assertEqual(recipe.format, "ruby-coverage")
        kept = self.recipe("rspec", environ={"RUBYOPT": "-W0"})
        self.assertEqual(kept.env["RUBYOPT"], f"-W0 -r{helper}")

    def test_ruby_helper_quotes_paths(self):
        text = programs.ruby_helper(Path("/it's/a\\b"), Path("/out"))
        self.assertIn("'/it\\'s/a\\\\b'", text)

    def test_whitespace_in_the_root_moves_option_paths_to_the_temp_folder(self):
        self.root = self.root / "my project"
        self.out = self.root / ".quality" / "coverage"
        ruby = self.recipe("rspec")
        helper = next(iter(ruby.files))
        self.assertEqual(ruby.env["RUBYOPT"], f"-r{helper}")
        self.assertFalse(any(character.isspace() for character in str(helper)))
        self.assertEqual(ruby.report, self.out / "ruby")
        jvm = self.recipe("mvn", "test")
        options = jvm.env["JAVA_TOOL_OPTIONS"]
        self.assertEqual(len(options.split()), 1)
        self.assertEqual(jvm.report, self.out / "jacoco" / "jacoco.xml")

    def test_whitespace_everywhere_blocks_ruby_and_jvm(self):
        self.out = self.root / "my project" / "out"
        with mock.patch.object(
            recipes.tempfile, "gettempdir", return_value="/tmp/with space"
        ):
            self.assertIn("whitespace", self.plan("rspec"))
            self.assertIn("whitespace", self.plan("mvn", "test"))
        spaced = recipes.CoverageTools(
            jacoco_agent="/my tools/jacocoagent.jar", jacoco_cli="/c/cli.jar"
        )
        self.assertIn("JaCoCo agent path", self.plan("mvn", "test", tools=spaced))

    def test_unknown_and_empty_commands_explain_what_is_missing(self):
        self.assertIn("no coverage recipe", self.plan("ctest"))
        self.assertIn("no test command", self.plan())
        self.assertIsInstance(
            recipes.plan_coverage(self.root, ["go", "test"], self.out, self.tools),
            recipes.Recipe,
        )

    def test_windows_executable_suffixes_are_ignored(self):
        self.assertEqual(self.recipe("dotnet.exe", "test").format, "cobertura")


class ConfiguredRecipeTests(RecipeTestCase):
    def test_configured_command_and_report_win(self):
        recipe = self.recipe(
            "go",
            "test",
            configured={
                "command": "make cov OUT={out_dir}/r.info",
                "report": "{root}/r.info",
                "format": "lcov",
            },
        )
        self.assertEqual(recipe.command, ("make", "cov", f"OUT={self.out}/r.info"))
        self.assertEqual(recipe.report, self.root / "r.info")

    def test_configured_report_only_keeps_the_test_command(self):
        recipe = self.recipe("npm", "test", configured={"report": "coverage/lcov.info"})
        self.assertEqual(recipe.command, ("npm", "test"))
        self.assertEqual(recipe.report, self.root / "coverage" / "lcov.info")
        self.assertEqual(recipe.format, "auto")

    def test_configured_command_keeps_a_spaced_root_in_one_argument(self):
        self.root = self.root / "My Project"
        self.out = self.root / ".quality" / "out"
        recipe = self.recipe(
            "make",
            "test",
            configured={
                "command": "make coverage OUT={out_dir}/lcov.info",
                "report": "{out_dir}/lcov.info",
                "format": "lcov",
            },
        )
        self.assertEqual(
            recipe.command, ("make", "coverage", f"OUT={self.out}/lcov.info")
        )
        self.assertEqual(recipe.report, self.out / "lcov.info")

    def test_configured_command_list(self):
        recipe = self.recipe(
            configured={"command": ["run", "{root}"], "report": "/abs/r.xml"}
        )
        self.assertEqual(recipe.command, ("run", str(self.root)))
        self.assertEqual(recipe.report, Path("/abs/r.xml"))

    def test_configured_errors(self):
        self.assertIn("coverage.report", self.plan(configured={"command": "x"}))
        result = self.plan(configured={"report": "r", "format": "clover"})
        self.assertIn("clover", result)


class HelperProgramTests(RecipeTestCase):
    def fake_tool(self, name, body):
        tool = write(
            self.root / "fakebin" / name, f"#!{sys.executable}\nimport sys\n{body}\n"
        )
        tool.chmod(0o755)
        return {"PATH": f"{tool.parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def test_fresh_directory_removes_stale_output(self):
        recipe = self.recipe("dotnet", "test")
        write(recipe.report / "old" / "coverage.cobertura.xml", "stale")
        result = self.run_command(recipe.setup[0])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(list(recipe.report.iterdir()), [])

    def test_swift_finalize_copies_the_printed_report(self):
        source = write(self.root / ".build" / "codecov" / "pkg.json", LLVM_COV_JSON)
        env = self.fake_tool("swift", f"print('building')\nprint({str(source)!r})")
        recipe = self.recipe("swift", "test")
        recipes.prepare_recipe(recipe)
        result = self.run_command(recipe.finalize[0], env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(recipe.report.read_text(), LLVM_COV_JSON)

    def test_jacoco_finalize_passes_build_directories_to_the_cli(self):
        record = self.root / "java-args.json"
        env = self.fake_tool(
            "java",
            f"import json\nopen({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))",
        )
        (self.root / "target" / "classes").mkdir(parents=True)
        (self.root / "src" / "main" / "java").mkdir(parents=True)
        recipe = self.recipe("mvn", "test")
        result = self.run_command(recipe.finalize[0], env)
        self.assertEqual(result.returncode, 0, result.stdout)
        arguments = json.loads(record.read_text())
        self.assertEqual(arguments[:3], ["-jar", "/cache/jacococli.jar", "report"])
        self.assertIn(str(self.root / "target" / "classes"), arguments)
        self.assertIn("--sourcefiles", arguments)
        self.assertEqual(arguments[-2:], ["--xml", str(recipe.report)])

    def test_jacoco_finalize_fails_without_compiled_classes(self):
        recipe = self.recipe("gradle", "test")
        result = self.run_command(recipe.finalize[0])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no compiled classes", result.stdout)

    def test_helper_programs_skip_site_initialization(self):
        recipe = self.recipe("dotnet", "test")
        self.assertEqual(recipe.setup[0][:3], (sys.executable, "-S", "-c"))

    def test_prepare_recipe_removes_a_stale_report_file(self):
        recipe = self.recipe("go", "test")
        write(recipe.report, "mode: atomic\n")
        recipes.prepare_recipe(recipe)
        self.assertFalse(recipe.report.exists())

    def test_prepare_recipe_writes_helpers_and_report_directory(self):
        recipe = self.recipe("rspec")
        recipes.prepare_recipe(recipe)
        helper = self.out / "ruby_coverage.rb"
        self.assertTrue(helper.read_text().startswith('require "coverage"'))
        recipes.prepare_recipe(self.recipe("go", "test"))
        self.assertTrue(self.out.is_dir())


# --- Evaluation --------------------------------------------------------------

CALC = """def sign(value):
    if value < 0:
        return -1
    return 1


def double(value):
    # explain
    result = value * 2

    return result
"""


def function(name, start, end, path="calc.py"):
    return FunctionInfo(path, name, name, start, end)


FUNCTIONS = [function("sign", 1, 4), function("double", 7, 11)]


class EvaluateCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.calc = write(self.root / "calc.py", CALC)

    def evaluate(self, data, changes=None, functions=FUNCTIONS, files=None, **options):
        thresholds = options.pop("thresholds", {})
        return gate_coverage.evaluate_coverage(
            self.root,
            files if files is not None else [self.calc],
            changes or diff_scope.repository_changes(),
            data,
            functions,
            thresholds,
            report_fresh=options.pop("report_fresh", True),
            **options,
        )

    def changed(self, *lines, path="calc.py"):
        change = diff_scope.FileChange(path, frozenset(lines))
        return diff_scope.ChangeSet(diff_scope.LOCAL_CHANGES, "HEAD", {path: change})

    def test_uncovered_lines_fail_and_are_grouped_by_function(self):
        data = {
            "calc.py": file_coverage(
                {1: 1, 2: 1, 3: 0, 4: 1, 7: 1, 9: 0, 11: 0}, {2: (2, 2)}
            )
        }
        outcome = self.evaluate(data)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            [(item.name, item.line, item.value) for item in outcome.items],
            [("sign", 3, "3"), ("double", 9, "9, 11")],
        )
        self.assertEqual(outcome.measurements["total_lines"], 7)
        self.assertEqual(outcome.measurements["branch_coverage_percent"], 100.0)
        self.assertIn("Required: 90% lines", outcome.summary)

    def test_only_changed_lines_count_in_incremental_scope(self):
        data = {"calc.py": file_coverage({1: 1, 2: 1, 3: 0, 4: 1})}
        outcome = self.evaluate(data, self.changed(1, 2, 4, 5))
        self.assertEqual(outcome.status, "pass")
        self.assertIn("Changed lines: 100.0% line coverage (3/3)", outcome.summary)
        self.assertEqual(outcome.measurements["scope"], "changed lines")
        self.assertIn("branch coverage not reported", outcome.summary)
        self.assertIn("Branch coverage not reported", outcome.details[-1])

    def test_untouched_files_are_out_of_scope(self):
        other = write(self.root / "other.py", "x = 1\n")
        data = {"other.py": file_coverage({1: 0})}
        changes = self.changed(1, path="README.md")
        outcome = self.evaluate(data, changes, files=[self.calc, other])
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.summary, "No changed executable lines.")

    def test_file_missing_from_report_was_never_loaded(self):
        outcome = self.evaluate({})
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            [(item.name, item.value) for item in outcome.items],
            [("sign", "1-4"), ("double", "7, 9, 11")],
        )
        self.assertEqual(outcome.measurements["never_loaded"], ["calc.py"])
        self.assertIn("calc.py: never loaded by tests.", outcome.details)

    def test_never_loaded_module_level_code_counts_as_uncovered(self):
        script = write(
            self.root / "tools" / "migrate.py",
            "import sys\n\n# Move rows.\nfor row in sys.argv:\n"
            "    if row == 'x':\n        raise SystemExit(row)\n",
        )
        change = diff_scope.FileChange("tools/migrate.py", is_new=True, all_lines=True)
        changes = diff_scope.ChangeSet(
            diff_scope.LOCAL_CHANGES, "HEAD", {"tools/migrate.py": change}
        )
        outcome = self.evaluate(
            {"calc.py": file_coverage({1: 1})}, changes, [], files=[script]
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.items[0].value, "1, 4-6")
        self.assertIn("tools/migrate.py: never loaded by tests.", outcome.details)

    def test_files_in_unmeasured_languages_are_not_never_loaded(self):
        menu = write(self.root / "static" / "menu.js", "function t() {\n  go();\n}\n")
        data = {"calc.py": file_coverage({1: 1, 2: 1, 3: 1, 4: 1})}
        outcome = self.evaluate(
            data, files=[self.calc, menu], functions=[], measures={".py"}
        )
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.measurements["not_measured"], ["static/menu.js"])
        self.assertEqual(outcome.measurements["never_loaded"], [])
        self.assertIn(
            "static/menu.js: not measured (this test run cannot measure .js files).",
            outcome.details,
        )

    def test_a_report_listing_the_file_measures_it_whatever_the_language(self):
        data = {"calc.py": file_coverage({1: 1, 2: 0})}
        outcome = self.evaluate(data, measures={".js"}, functions=[])
        self.assertEqual(outcome.status, "fail")

    def test_only_unmeasured_changes_are_blocked(self):
        names = [f"static/m{index}.js" for index in range(7)] + ["static/Makefile"]
        files = [write(self.root / name, "go();\n") for name in names]
        write(self.root / "static" / "blank.js", "\n")
        files.append(self.root / "static" / "blank.js")
        outcome = self.evaluate({}, files=files, functions=[], measures={".py"})
        self.assertEqual(outcome.status, "blocked")
        self.assertIn("static/m0.js", outcome.summary)
        self.assertIn("and 3 more", outcome.summary)
        self.assertEqual(len(outcome.details), 8)
        self.assertIn("cannot measure these files", outcome.details[-1])

    def test_branch_shortfall_lists_partial_conditions(self):
        data = {"calc.py": file_coverage({1: 1, 2: 1, 3: 1, 4: 1}, {2: (1, 2)})}
        outcome = self.evaluate(data)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(len(outcome.items), 1)
        self.assertEqual(outcome.items[0].metric, "branch outcomes taken")
        self.assertEqual(outcome.items[0].value, "line 2 (1/2)")
        self.assertEqual(outcome.measurements["branch_coverage_percent"], 50.0)

    def test_limits_come_from_thresholds(self):
        data = {"calc.py": file_coverage({1: 1, 2: 1, 3: 0, 4: 1}, {2: (1, 2)})}
        loose = {"coverage": {"line_coverage_limit": 70, "branch_coverage_limit": 50}}
        self.assertEqual(self.evaluate(data, thresholds=loose).status, "pass")
        section = {"line_coverage_limit": 80}
        self.assertEqual(self.evaluate(data, thresholds=section).status, "fail")
        limits = gate_coverage.CoverageLimits.from_thresholds({"coverage": 3})
        self.assertEqual(limits, gate_coverage.CoverageLimits())

    def test_items_are_capped(self):
        many = "".join(f"def f{index}():\n    return {index}\n" for index in range(60))
        path = write(self.root / "many.py", many)
        functions = [
            function(f"f{index}", 2 * index + 1, 2 * index + 2, "many.py")
            for index in range(60)
        ]
        lines = {2 * index + 2: 0 for index in range(60)}
        outcome = self.evaluate(
            {"many.py": file_coverage(lines)}, files=[path], functions=functions
        )
        self.assertEqual(len(outcome.items), gate_coverage.MAX_ITEMS)
        self.assertIn("... and 10 more not listed.", outcome.details)

    def test_module_level_lines_have_no_function_name(self):
        outcome = self.evaluate({"calc.py": file_coverage({1: 0})}, functions=[])
        self.assertEqual(outcome.items[0].name, "")

    def test_missing_or_stale_report_is_blocked(self):
        stale = self.evaluate({"calc.py": file_coverage({1: 1})}, report_fresh=False)
        self.assertEqual(stale.status, "blocked")
        missing = self.evaluate(None, problem="cannot read r.json: bad")
        self.assertEqual(missing.status, "blocked")
        self.assertIn("cannot read r.json", missing.summary)
        php = self.evaluate(None, prerequisite="the pcov or Xdebug PHP extension")
        self.assertIn("did not write a fresh coverage report", php.summary)
        self.assertIn("needs the pcov or Xdebug PHP extension.", php.summary)

    def test_blocked_coverage_names_the_prerequisite(self):
        outcome = gate_coverage.blocked_coverage("c8 is missing.", ["detail"])
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.details, ["detail"])

    def test_added_lines_past_the_end_are_ignored(self):
        outcome = self.evaluate({}, self.changed(1, 400))
        self.assertEqual(outcome.items[0].value, "1")


class HelperTests(unittest.TestCase):
    def test_compact_ranges(self):
        self.assertEqual(
            gate_coverage.compact_ranges([9, 3, 4, 5, 5, 11]), "3-5, 9, 11"
        )
        self.assertEqual(gate_coverage.compact_ranges([]), "")

    def test_function_line_coverage_for_crap(self):
        data = {"a.py": file_coverage({1: 1, 2: 0, 3: 1, 4: 1, 10: 0})}
        self.assertEqual(gate_coverage.function_line_coverage(data, "a.py", 1, 4), 75.0)
        self.assertIsNone(gate_coverage.function_line_coverage(data, "a.py", 5, 9))
        self.assertEqual(gate_coverage.function_line_coverage(data, "b.py", 1, 4), 0.0)
        lookup = gate_coverage.coverage_lookup(data)
        self.assertEqual(lookup("a.py", 10, 10), 0.0)
        python_only = gate_coverage.coverage_lookup(data, {".py"})
        self.assertIsNone(python_only("static/menu.js", 1, 4))
        self.assertEqual(python_only("b.py", 1, 4), 0.0)


if __name__ == "__main__":
    unittest.main()
