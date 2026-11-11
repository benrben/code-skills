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
import gate_complexity  # noqa: E402
import gate_complexity_lizard  # noqa: E402
import gate_complexity_python  # noqa: E402
from gate_complexity_lizard import AnalysisError, SourceOutline  # noqa: E402
from gate_types import BLOCKED, FAIL, PASS, FunctionInfo, ToolPython  # noqa: E402

TOOL_PYTHON = ToolPython(sys.executable)

# Built in pieces so this file carries no real forgiveness comment.
FORGIVE = "lizard " + "forgive"

PYTHON_SAMPLE = """\
class Cart:
    def total(self, items):
        result = 0
        for item in items:
            if item.price > 0:
                if item.taxed:
                    result += item.price * 2
        return result


def helper(a, b):
    return a or b
"""

GO_SAMPLE = """\
package main

type Cart struct{}

func (c *Cart) Total(x int) int {
\tif x > 0 {
\t\treturn 1
\t}
\treturn 0
}

func helper(a, b int) int {
\tfor i := 0; i < a; i++ {
\t\tif i > b {
\t\t\tif i%2 == 0 {
\t\t\t\treturn i
\t\t\t}
\t\t}
\t}
\treturn 0
}
"""

JAVASCRIPT_SAMPLE = """\
class Cart {
  total(x) {
    if (x) { return 1; }
    return 0;
  }
}
function helper(a) {
  return a && a.b ? 1 : 2;
}
"""

TYPESCRIPT_SAMPLE = """\
export class Cart {
  total(x: number): number {
    if (x > 1) { return 1; }
    return 0;
  }
}
export function helper(a: string): number {
  return a.length;
}
"""

JAVA_SAMPLE = """\
public class Cart {
  public int total(int x) {
    if (x > 0 && x < 10) { return 1; }
    return 0;
  }
}
"""

RUST_SAMPLE = """\
struct Cart;
impl Cart {
    pub fn total(&self, x: i32) -> i32 {
        if x > 0 { 1 } else { 0 }
    }
}
fn helper(a: i32) -> i32 {
    match a { 1 => 2, 2 => 3, _ => 4 }
}
"""

FORGIVEN_SAMPLE = f"""\
#{FORGIVE} global
def hidden():
    #{FORGIVE}
    if a:
        return 1
    return 2


def partly(b):
    # {FORGIVE}s(cyclomatic_complexity)
    return b


# GENERATED CODE
def after_marker():
    return 3
"""


TWELVE_BRANCHES = "".join(
    f"    if x == {index}:\n        return {index}\n" for index in range(12)
)

# Valid Python whose continuation lines sit at or left of the def's indent;
# lizard alone ends each function on the line before them.
BRACKET_TRICK = "def f(x):\n    y = [\n1]\n" + TWELVE_BRANCHES + "    return y\n"
STRING_TRICK = (
    'def f(x):\n    query = (\n"SELECT 1"\n    )\n'
    + TWELVE_BRANCHES
    + "    return query\n"
)
METHOD_TRICK = (
    'class K:\n    def f(self, x):\n        y = (\n    "text"\n        )\n'
    + TWELVE_BRANCHES.replace("    ", "        ")
    + "        return y\n"
)

CHAIN = " else ".join(f"{index} if x == 1" for index in range(12)) + " else 12"

# Lizard alone reports only ``h`` here.
ONE_LINERS = f"def g(x): return {CHAIN}\n\n\ndef h(x):\n    return 1\n"
NAMED_LAMBDA = f"f = lambda x: {CHAIN}\nclass K:\n    key: object = lambda: 1\n"


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_error(message):
    return AnalysisError(None, message)


def is_method(path, name, long_name, text, start_line):
    outline = SourceOutline.of(path, text)
    return gate_complexity_lizard.is_method(name, long_name, outline, start_line)


def by_name(functions):
    return {function.name: function for function in functions}


def function(
    name="run",
    path="app.py",
    start=1,
    end=5,
    cyclomatic=1,
    cognitive=0,
    nesting=0,
):
    return FunctionInfo(
        path, name, f"{name}()", start, end, cyclomatic, cognitive, nesting
    )


def local_changes(added):
    files = {
        path: diff_scope.FileChange(path, frozenset(lines))
        for path, lines in added.items()
    }
    return diff_scope.ChangeSet(diff_scope.LOCAL_CHANGES, "HEAD", files)


class AnalyzeFunctionsTests(unittest.TestCase):
    def analyze(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write(root, name, text) for name, text in files.items()]
            return gate_complexity.analyze_functions(TOOL_PYTHON, root, paths)

    def test_python_metrics_and_methods(self):
        functions, unsupported, errors = self.analyze({"src/cart.py": PYTHON_SAMPLE})
        self.assertEqual((unsupported, errors), ([], []))
        found = by_name(functions)
        total = found["total"]
        self.assertEqual(total.path, "src/cart.py")
        self.assertEqual((total.start_line, total.end_line), (2, 8))
        self.assertEqual(total.cyclomatic, 4)
        self.assertEqual(total.nesting, 3)
        self.assertGreater(total.cognitive, total.nesting)
        self.assertEqual(total.parameters, 2)
        self.assertTrue(total.is_method)
        self.assertFalse(found["helper"].is_method)

    def test_nesting_counts_open_blocks_not_sequential_statements(self):
        flat = "def flat(a, b, c, d, e, f):\n" + "".join(
            f"    if {name}:\n        print({name})\n" for name in "abcdef"
        )
        deep = (
            "def deep(a):\n"
            "    if a:\n"
            "        for b in a:\n"
            "            while b:\n"
            "                if b > 1:\n"
            "                    if b > 2:\n"
            "                        b -= 1\n"
        )
        braceless = "int f(int a) {\n" + "  if (a) a++;\n" * 6 + "  return a;\n}\n"
        functions, _, _ = self.analyze(
            {"flat.py": flat + "\n\n" + deep, "flat.c": braceless}
        )
        nesting = {f.name: f.nesting for f in functions}
        self.assertEqual(nesting, {"flat": 1, "deep": 5, "f": 1})

    def test_go_receiver_is_a_method(self):
        functions, _, _ = self.analyze({"cart.go": GO_SAMPLE})
        found = by_name(functions)
        self.assertTrue(found["Total"].is_method)
        self.assertFalse(found["helper"].is_method)
        self.assertEqual(found["helper"].cyclomatic, 4)
        self.assertEqual(found["helper"].nesting, 3)
        self.assertEqual(found["helper"].cognitive, 6)

    def test_javascript_and_typescript_class_members(self):
        functions, _, _ = self.analyze(
            {"web/cart.js": JAVASCRIPT_SAMPLE, "web/cart.ts": TYPESCRIPT_SAMPLE}
        )
        rows = {(f.path, f.name): f for f in functions}
        self.assertTrue(rows[("web/cart.js", "total")].is_method)
        self.assertFalse(rows[("web/cart.js", "helper")].is_method)
        self.assertEqual(rows[("web/cart.js", "helper")].cyclomatic, 3)
        self.assertTrue(rows[("web/cart.ts", "total")].is_method)
        self.assertFalse(rows[("web/cart.ts", "helper")].is_method)

    def test_java_qualified_name_and_rust_impl(self):
        functions, _, _ = self.analyze(
            {"src/Cart.java": JAVA_SAMPLE, "src/cart.rs": RUST_SAMPLE}
        )
        rows = {(f.path, f.bare_name): f for f in functions}
        java = rows[("src/Cart.java", "total")]
        self.assertEqual(java.name, "Cart::total")
        self.assertEqual(java.cyclomatic, 3)
        self.assertTrue(java.is_method)
        self.assertTrue(rows[("src/cart.rs", "total")].is_method)
        self.assertFalse(rows[("src/cart.rs", "helper")].is_method)
        self.assertEqual(rows[("src/cart.rs", "helper")].cyclomatic, 3)

    def test_forgiveness_comments_cannot_hide_functions(self):
        functions, _, _ = self.analyze({"gen.py": FORGIVEN_SAMPLE})
        self.assertEqual(
            [f.name for f in functions], ["hidden", "partly", "after_marker"]
        )
        self.assertEqual(by_name(functions)["hidden"].cyclomatic, 2)

    def test_unsupported_and_unreadable_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "lib/app.ex", "defmodule App do\nend\n")
            functions, unsupported, errors = gate_complexity.analyze_functions(
                TOOL_PYTHON, root, ["lib/app.ex", "missing.py"]
            )
        self.assertEqual(functions, [])
        self.assertEqual(unsupported, ["lib/app.ex"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].path, "missing.py")
        self.assertTrue(str(errors[0]).startswith("missing.py: cannot read file"))

    def test_bracket_and_string_continuations_cannot_end_a_function_early(self):
        functions, _, errors = self.analyze(
            {"brk.py": BRACKET_TRICK, "brk3.py": STRING_TRICK, "brk2.py": METHOD_TRICK}
        )
        self.assertEqual(errors, [])
        rows = {f.path: f for f in functions}
        self.assertEqual((rows["brk.py"].start_line, rows["brk.py"].end_line), (1, 28))
        self.assertEqual(rows["brk.py"].cyclomatic, 13)
        self.assertEqual(
            (rows["brk3.py"].start_line, rows["brk3.py"].end_line), (1, 29)
        )
        self.assertEqual(rows["brk3.py"].cyclomatic, 13)
        method = rows["brk2.py"]
        self.assertEqual((method.start_line, method.end_line), (2, 30))
        self.assertEqual(method.cyclomatic, 13)
        self.assertTrue(method.is_method)
        outcome = gate_complexity.evaluate_complexity(
            functions, diff_scope.repository_changes(), lambda *_: 100.0, {}
        )
        self.assertEqual(outcome.status, FAIL)

    def test_one_line_defs_and_named_lambdas_are_measured(self):
        functions, _, errors = self.analyze(
            {"one.py": ONE_LINERS, "lam.py": NAMED_LAMBDA}
        )
        self.assertEqual(errors, [])
        rows = {(f.path, f.name): f for f in functions}
        one_liner = rows[("one.py", "g")]
        self.assertEqual((one_liner.start_line, one_liner.end_line), (1, 1))
        self.assertEqual(one_liner.cyclomatic, 13)
        self.assertEqual(one_liner.parameters, 1)
        self.assertEqual(rows[("one.py", "h")].cyclomatic, 1)
        named = rows[("lam.py", "f")]
        self.assertEqual((named.start_line, named.cyclomatic), (1, 13))
        self.assertFalse(named.is_method)
        self.assertTrue(rows[("lam.py", "key")].is_method)

    def test_python_that_does_not_parse_keeps_lizards_reading(self):
        functions, _, errors = self.analyze(
            {"broken.py": "def run(x):\n    if x:\n        return (\n"}
        )
        self.assertEqual(errors, [])
        self.assertEqual([f.name for f in functions], ["run"])

    def test_function_lizard_cannot_remeasure_is_an_error(self):
        ghost = (
            "missed_functions = lambda text, measured: "
            "[MissedFunction('ghost', 1, 2, 'x = 1\\n')]\n"
        )
        helper = gate_complexity_lizard.LIZARD_HELPER
        self.assertTrue(helper.endswith("\nmain()\n"))
        patched = helper[: -len("main()\n")] + ghost + "main()\n"
        with mock.patch.object(gate_complexity_lizard, "LIZARD_HELPER", patched):
            functions, _, errors = self.analyze({"app.py": "x = 1\n"})
        self.assertEqual(functions, [])
        self.assertEqual(
            errors, [AnalysisError("app.py", "line 1: lizard cannot measure ghost")]
        )

    def test_python_nested_and_cpp_namespace_functions_are_free(self):
        functions, _, _ = self.analyze(
            {
                "n.py": "def build(items):\n    def unused_helper(x):\n"
                "        return x\n    return list(items)\n",
                "n.cpp": "namespace util {\nint unused_free(int x) {\n"
                "  return x * 2;\n}\n}\n",
            }
        )
        flags = {f.name: f.is_method for f in functions}
        self.assertEqual(
            flags,
            {"build": False, "build.unused_helper": False, "util::unused_free": False},
        )

    def test_no_paths_skips_the_helper(self):
        with mock.patch.object(gate_complexity_lizard.subprocess, "run") as run:
            result = gate_complexity.analyze_functions(TOOL_PYTHON, Path("."), [])
        self.assertEqual(result, ([], [], []))
        run.assert_not_called()


class HelperFailureTests(unittest.TestCase):
    def run_with(self, **patch):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "app.py", "def run():\n    return 1\n")
            with mock.patch.object(gate_complexity_lizard.subprocess, "run", **patch):
                return gate_complexity.analyze_functions(TOOL_PYTHON, root, ["app.py"])

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_missing_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "app.py", "def run():\n    return 1\n")
            missing = ToolPython(str(root / "no-python"))
            functions, _, errors = gate_complexity.analyze_functions(
                missing, root, ["app.py"]
            )
        self.assertEqual(functions, [])
        self.assertIsNone(errors[0].path)
        self.assertIn("cannot start", str(errors[0]))

    def test_timeout(self):
        error = subprocess.TimeoutExpired(["python"], 5)
        _, _, errors = self.run_with(side_effect=error)
        self.assertEqual(errors, [run_error("lizard timed out after 600 seconds")])

    def test_nonzero_exit_reports_stderr(self):
        failed = self.completed(1, stderr="ModuleNotFoundError: lizard")
        _, _, errors = self.run_with(return_value=failed)
        expected = "lizard exited with 1: ModuleNotFoundError: lizard"
        self.assertEqual(errors, [run_error(expected)])

    def test_output_that_is_not_json(self):
        _, _, errors = self.run_with(return_value=self.completed(stdout="oops"))
        expected = "lizard helper printed output that is not JSON"
        self.assertEqual(errors, [run_error(expected)])

    def test_output_that_is_not_an_object(self):
        _, _, errors = self.run_with(return_value=self.completed(stdout="[]"))
        self.assertIn("not a JSON object", str(errors[0]))

    def test_per_file_errors_are_passed_through(self):
        output = (
            '{"functions": [], "unsupported": [], '
            '"errors": [{"path": "app.py", "message": "boom"}]}'
        )
        _, _, errors = self.run_with(return_value=self.completed(stdout=output))
        self.assertEqual(errors, [AnalysisError("app.py", "boom")])
        self.assertEqual(str(errors[0]), "app.py: boom")


class PureHelperTests(unittest.TestCase):
    def test_neutralize_keeps_lines_and_disarms_markers(self):
        text = f"a\n#{FORGIVE}\n# {FORGIVE.title()}s(x)\n// GENERATED CODE\n"
        result = gate_complexity_lizard.neutralize_forgiveness(text)
        self.assertEqual(result.count("\n"), text.count("\n"))
        self.assertNotIn("forgive", result.lower())
        self.assertNotIn("GENERATED CODE", result)

    def test_crap_formula(self):
        self.assertEqual(gate_complexity.crap_score(5, 100.0), 5.0)
        self.assertEqual(gate_complexity.crap_score(5, 0.0), 30.0)
        self.assertAlmostEqual(gate_complexity.crap_score(10, 50.0), 22.5)
        self.assertEqual(gate_complexity.crap_score(3, 150.0), 3.0)
        self.assertEqual(gate_complexity.crap_score(3, -10.0), 12.0)

    def test_is_method_rules(self):
        lines = "class A:\n    # note\n\n    def run(self):\n        pass\n"
        self.assertTrue(is_method("a.py", "run", "run( self )", lines, 4))
        after_field = "class A:\n    size = 1\n    def run(self):\n"
        self.assertTrue(is_method("a.py", "run", "run", after_field, 3))
        self.assertTrue(is_method("A.java", "A::run", "A::run()", "", 1))
        self.assertTrue(is_method("a.js", "obj.save", "obj.save ( )", "", 1))
        self.assertTrue(is_method("a.go", "run", "(a A)run", "", 1))
        self.assertFalse(is_method("a.py", "run", "run", lines, 99))
        self.assertFalse(is_method("a.py", "run", "run", "def run():\n", 1))
        nested = "def outer():\n    def inner():\n        pass\n"
        self.assertFalse(is_method("a.py", "inner", "inner", nested, 2))
        orphan = "    def stray():\n        pass\n"
        self.assertFalse(is_method("a.py", "stray", "stray", orphan, 1))

    def test_python_nested_functions_are_not_methods(self):
        # Lizard names a nested Python function ``outer.inner``.
        nested = "def load(path):\n    def _unused_helper():\n        pass\n"
        self.assertFalse(
            is_method(
                "g.py", "load._unused_helper", "load._unused_helper( )", nested, 2
            )
        )
        under_record = "def f(record):\n    def helper():\n        pass\n"
        self.assertFalse(is_method("f.py", "f.helper", "f.helper( )", under_record, 2))

    def test_javascript_owner_must_be_a_type_declaration(self):
        render = "function render(record) {\n  function unusedHelper() {}\n}\n"
        self.assertFalse(
            is_method("f.js", "unusedHelper", "unusedHelper ( )", render, 2)
        )
        uses_object = "function f(object, module) {\n  function g() {}\n}\n"
        self.assertFalse(is_method("f.js", "g", "g ( )", uses_object, 2))
        self.assertFalse(is_method("b.js", "(anonymous)", "(anonymous)( x )", "", 1))
        private = "export default class K {\n  #run() {}\n}\n"
        self.assertTrue(is_method("k.js", "#run", "#run ( )", private, 2))
        sealed = "public sealed class Cart\n{\n    int Total() { return 1; }\n}\n"
        self.assertTrue(is_method("c.cs", "Total", "Total()", sealed, 3))
        generic_impl = "impl<T> Cart<T> {\n    fn total(&self) {}\n}\n"
        self.assertTrue(is_method("c.rs", "total", "total & self", generic_impl, 2))

    def test_cpp_namespace_functions_are_not_methods(self):
        source = (
            "namespace util {\nint unused_free(int x) { return x; }\n}\n"
            "namespace a::b {\nint g() { return 1; }\n}\n"
            "int Cart::total() { return 1; }\n"
        )
        self.assertFalse(is_method("n.cpp", "util::unused_free", "", source, 2))
        self.assertFalse(is_method("n.cpp", "a::b::g", "a::b::g()", source, 5))
        self.assertTrue(is_method("n.cpp", "Cart::total", "Cart::total()", source, 7))

    def test_owner_lookup_is_linear_in_class_length(self):
        methods = 20000
        text = "class Huge:\n" + "".join(
            f"    def m{index}(self):\n        return {index}\n"
            for index in range(methods)
        )
        started = time.perf_counter()
        outline = SourceOutline.of("huge.py", text)
        flags = [
            gate_complexity_lizard.is_method(f"m{index}", "", outline, 2 + 2 * index)
            for index in range(methods)
        ]
        self.assertTrue(all(flags))
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_missed_functions_lists_only_what_lizard_got_wrong(self):
        missed = gate_complexity_python.missed_functions
        source = "def ok(x):\n    return x\n\n\ndef short(x): return x\n"
        found = missed(source, {1: 2})
        self.assertEqual(
            [(f.name, f.start_line, f.end_line) for f in found], [("short", 5, 5)]
        )
        self.assertEqual(found[0].source, "def short(x):\n    return x\n")
        self.assertEqual(missed(source, {1: 2, 5: 5}), [])
        self.assertEqual([f.name for f in missed(BRACKET_TRICK, {1: 2})], ["f"])
        self.assertEqual(missed("def broken(:\n", {}), [])
        self.assertEqual(missed("x = 1\0", {}), [])

    def test_missed_functions_normalizes_named_lambdas(self):
        source = (
            "f = lambda a, *b: a or b\n"
            "g: object = lambda: 1\n"
            "a, b = lambda: 1, 2\n"
            "h = i = lambda: 1\n"
            "j: int\n"
            "values[0] = lambda: 1\n"
            "async def run():\n    return 1\n"
        )
        found = gate_complexity_python.missed_functions(source, {7: 8})
        self.assertEqual([f.name for f in found], ["f", "g"])
        self.assertEqual(found[0].source, "def f(a, *b):\n    return a or b\n")
        self.assertEqual((found[1].start_line, found[1].end_line), (2, 2))

    def test_percentile(self):
        self.assertIsNone(gate_complexity.percentile([], 0.5))
        values = [float(value) for value in range(1, 21)]
        self.assertEqual(gate_complexity.percentile(values, 0.5), 10.0)
        self.assertEqual(gate_complexity.percentile(values, 0.95), 19.0)
        self.assertEqual(gate_complexity.percentile([7.0], 0.0), 7.0)

    def test_limits_from_document_section_or_defaults(self):
        limits = gate_complexity.ComplexityLimits
        document = {"complexity": {"cyclomatic_limit": 8, "crap_limit": 12}}
        self.assertEqual(
            limits.from_thresholds(document), limits(cyclomatic=8, crap=12)
        )
        self.assertEqual(
            limits.from_thresholds({"nesting_limit": 2}), limits(nesting=2)
        )
        self.assertEqual(limits.from_thresholds({}), limits())


class EvaluateComplexityTests(unittest.TestCase):
    thresholds = {
        "complexity": {
            "cyclomatic_limit": 10,
            "cognitive_limit": 15,
            "nesting_limit": 4,
            "crap_limit": 30,
        }
    }

    def evaluate(self, functions, changes=None, lookup=None, **extra):
        return gate_complexity.evaluate_complexity(
            functions,
            changes or diff_scope.repository_changes(),
            lookup,
            self.thresholds,
            **extra,
        )

    def test_only_functions_touching_added_lines_are_judged(self):
        old = function("old", start=1, end=10, cyclomatic=20)
        new = function("new", start=20, end=25, cyclomatic=12)
        other = function("other", path="lib.py", start=1, end=3, cyclomatic=30)
        outcome = self.evaluate([old, new, other], local_changes({"app.py": {22}}))
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual([item.name for item in outcome.items], ["new"])
        self.assertEqual(outcome.items[0].metric, "cyclomatic")
        self.assertEqual(outcome.items[0].value, "12")
        self.assertEqual(outcome.items[0].limit, "10")
        self.assertEqual(outcome.measurements["functions_in_scope"], 1)
        self.assertEqual(outcome.measurements["functions_analyzed"], 3)
        self.assertEqual(
            outcome.summary, "Changed functions over the complexity limits: 1 of 1."
        )

    def test_untouched_change_passes_with_nothing_to_measure(self):
        outcome = self.evaluate([function()], local_changes({"other.py": {1}}))
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.summary, "No changed functions to measure.")
        self.assertEqual(outcome.measurements["cyclomatic"]["max"], None)

    def test_each_metric_over_its_limit_is_an_item(self):
        heavy = function("heavy", cyclomatic=11, cognitive=16, nesting=5)
        outcome = self.evaluate([heavy], lookup=lambda path, start, end: 0.0)
        metrics = [item.metric for item in outcome.items]
        self.assertEqual(metrics, ["cyclomatic", "cognitive", "nesting", "CRAP"])
        self.assertEqual(outcome.items[3].value, "132.00 (line coverage 0%)")
        self.assertIn("app.py:1 heavy: cognitive 16 (limit 15)", outcome.details)
        self.assertEqual(
            outcome.summary, "Functions over the complexity limits: 1 of 1."
        )

    def test_crap_uses_coverage_lookup(self):
        calls = []

        def lookup(path, start, end):
            calls.append((path, start, end))
            return 100.0 if path == "covered.py" else 0.0

        covered = function("covered", path="covered.py", cyclomatic=8)
        bare = function("bare", path="bare.py", cyclomatic=6)
        outcome = self.evaluate([covered, bare], lookup=lookup)
        self.assertEqual(calls, [("covered.py", 1, 5), ("bare.py", 1, 5)])
        self.assertEqual([item.name for item in outcome.items], ["bare"])
        self.assertEqual(outcome.items[0].metric, "CRAP")
        rows = outcome.measurements["rows"]
        self.assertEqual([row["name"] for row in rows], ["bare", "covered"])
        self.assertEqual(rows[0]["crap"], 42.0)
        self.assertFalse(rows[0]["passed"])
        self.assertTrue(rows[1]["passed"])
        self.assertEqual(outcome.measurements["crap"]["max"], 42.0)
        self.assertEqual(outcome.measurements["crap_not_computed"], 0)

    def test_missing_coverage_skips_crap_and_says_so(self):
        functions = [function("a", cyclomatic=9), function("b", path="b.py")]
        lookups = {"app.py": None, "b.py": 50.0}
        outcome = self.evaluate(
            functions, lookup=lambda path, start, end: lookups[path]
        )
        self.assertEqual(outcome.status, PASS)
        self.assertIn(
            "CRAP not computed for 1 function: no line coverage data.",
            outcome.details,
        )
        self.assertEqual(outcome.measurements["crap_not_computed"], 1)
        self.assertTrue(outcome.summary.startswith("Functions within limits: 2 of 2"))

    def test_without_lookup_crap_is_never_computed(self):
        outcome = self.evaluate([function(cyclomatic=10)])
        self.assertEqual(outcome.status, PASS)
        self.assertIsNone(outcome.measurements["rows"][0]["crap"])
        self.assertIsNone(outcome.measurements["crap"]["p50"])

    def test_report_rows_are_capped_and_ranked(self):
        functions = [
            function(
                f"f{index}", start=index * 10, end=index * 10 + 5, cyclomatic=index
            )
            for index in range(1, 26)
        ]
        outcome = self.evaluate(functions)
        rows = outcome.measurements["rows"]
        self.assertEqual(len(rows), gate_complexity.REPORT_ROWS)
        self.assertEqual(rows[0]["name"], "f25")
        self.assertEqual(outcome.measurements["cyclomatic"]["max"], 25)
        self.assertEqual(outcome.measurements["cyclomatic"]["p50"], 13)

    def test_lizard_errors_block_when_nothing_failed(self):
        outcome = self.evaluate([], errors=["lizard exited with 1: boom"])
        self.assertEqual(outcome.status, BLOCKED)
        self.assertEqual(
            outcome.summary,
            "Lizard could not analyze the code: lizard exited with 1: boom",
        )
        self.assertIn("Lizard error: lizard exited with 1: boom", outcome.details)

    def test_failures_win_over_errors_and_unsupported_is_noted(self):
        outcome = self.evaluate(
            [function(nesting=6)],
            unsupported=["lib/z.ex", "lib/a.ex"],
            errors=["x.py: boom"],
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertIn(
            "Not analyzed (lizard has no reader for these files): lib/a.ex, lib/z.ex",
            outcome.details,
        )
        self.assertEqual(outcome.measurements["not_analyzed"], ["lib/a.ex", "lib/z.ex"])

    def test_unsupported_and_errors_outside_the_change_are_ignored(self):
        changes = local_changes({"new.py": {2}})
        outcome = self.evaluate(
            [function("run", path="new.py", start=1, end=3)],
            changes,
            unsupported=["vendor/old.ex"],
            errors=[AnalysisError("legacy/old.py", "RecursionError: boom")],
        )
        self.assertEqual(outcome.status, PASS)
        self.assertFalse(any("old" in detail for detail in outcome.details))
        self.assertEqual(outcome.measurements["not_analyzed"], [])

    def test_errors_in_the_change_or_without_a_path_block(self):
        changes = local_changes({"new.py": {2}, "lib/new.ex": {1}})
        for error in (
            AnalysisError("new.py", "RecursionError: boom"),
            AnalysisError(None, "lizard timed out after 600 seconds"),
            "lizard exited with 1: boom",
        ):
            with self.subTest(error=error):
                outcome = self.evaluate(
                    [], changes, unsupported=["lib/new.ex"], errors=[error]
                )
                self.assertEqual(outcome.status, BLOCKED)
                self.assertIn(str(error), outcome.summary)
                self.assertEqual(outcome.measurements["not_analyzed"], ["lib/new.ex"])

    def test_languages_measured_from_indentation_are_named(self):
        ruby = function("run", path="lib/cart.rb", nesting=0)
        outcome = self.evaluate([ruby, function()])
        self.assertIn(
            "Nesting measured from indentation (lizard tracks no nesting in "
            "these languages): lib/cart.rb",
            outcome.details,
        )
        self.assertEqual(
            outcome.measurements["nesting_from_indentation"], ["lib/cart.rb"]
        )

    def test_ruby_and_lua_nesting_is_measured_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "deep.rb", RUBY_DEEP)
            write(root, "deep.lua", LUA_DEEP)
            functions, _, errors = gate_complexity.analyze_functions(
                TOOL_PYTHON, root, [root / "deep.rb", root / "deep.lua"]
            )
        self.assertEqual(errors, [])
        measured = {function.path: function for function in functions}
        # each-block > if > while > if > unless: the innermost body is 5 deep.
        self.assertEqual(measured["deep.rb"].nesting, 5)
        self.assertEqual(measured["deep.lua"].nesting, 5)
        # The nesting increments 1 + 2 + 3 + 4 are added to lizard's count.
        self.assertGreaterEqual(measured["deep.rb"].cognitive, 10 + 4)
        self.assertGreaterEqual(measured["deep.lua"].cognitive, 10 + 5)

    def test_end_to_end_with_lizard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "cart.go", GO_SAMPLE)
            functions, unsupported, errors = gate_complexity.analyze_functions(
                TOOL_PYTHON, root, [root / "cart.go"]
            )
        strict = {"complexity": {"cyclomatic_limit": 3, "nesting_limit": 2}}
        outcome = gate_complexity.evaluate_complexity(
            functions,
            local_changes({"cart.go": {14}}),
            None,
            strict,
            unsupported=unsupported,
            errors=errors,
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(
            [(item.name, item.metric) for item in outcome.items],
            [("helper", "cyclomatic"), ("helper", "nesting")],
        )


RUBY_DEEP = """def deep(items, limit)
  total = 0
  items.each do |item|
    if item > 0
      while limit > 0
        if item.even?
          unless item > 100
            total += 1
          end
        end
        limit -= 1
      end
    end
  end
  total
end
"""

LUA_DEEP = """function deep(items, limit)
  local total = 0
  for _, item in ipairs(items) do
    if item > 0 then
      while limit > 0 do
        if item % 2 == 0 then
          if item < 100 then
            total = total + 1
          end
        end
        limit = limit - 1
      end
    end
  end
  return total
end
"""


if __name__ == "__main__":
    unittest.main()
