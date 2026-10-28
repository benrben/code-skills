import dataclasses
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "code-discipline" / "scripts" / "portable_analysis.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))


def load_script():
    spec = importlib.util.spec_from_file_location(
        "portable_analysis_test_module", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


analysis = load_script()
graph = sys.modules[analysis.analyze_dependency_graph.__module__]


class ComplexityTests(unittest.TestCase):
    def test_python_ast_reports_cognitive_complexity_and_nesting(self):
        source = """\
def evaluate(a, b, items):
    if a and b:
        for item in items:
            if item:
                continue
    elif a:
        return 1
    return 0
"""
        result = analysis.analyze_source_complexity(
            "logic.py", source, language="python"
        )

        self.assertEqual(result.status, "supported")
        self.assertEqual(len(result.functions), 1)
        metric = result.functions[0]
        self.assertEqual(metric.name, "evaluate")
        self.assertEqual(metric.cognitive_complexity, 9)
        self.assertEqual(metric.max_nesting, 3)
        self.assertEqual((metric.line, metric.end_line), (1, 8))

    def test_nested_functions_are_measured_separately(self):
        source = """\
def outer(flag):
    def inner(other):
        if other:
            return True
        return False
    return inner(flag)
"""
        result = analysis.analyze_python_complexity("nested.py", source)

        self.assertEqual(
            [(item.name, item.cognitive_complexity) for item in result.functions],
            [("outer", 0), ("outer.inner", 1)],
        )

    def test_parse_errors_and_unsupported_languages_are_explicit(self):
        invalid = analysis.analyze_python_complexity("broken.py", "def nope(:\n")
        unsupported = analysis.analyze_source_complexity(
            "main.go", "package main", language="go"
        )

        self.assertEqual(invalid.status, "parse_error")
        self.assertIn("line 1", invalid.message)
        self.assertEqual(invalid.functions, ())
        self.assertEqual(unsupported.status, "unsupported")
        self.assertIn("go", unsupported.message)
        self.assertEqual(unsupported.functions, ())

    def test_python_ast_covers_supported_control_flow_constructs(self):
        source = """\
class Worker:
    async def run(self, values):
        selected = [x for x in values if x and x > 0]
        unique = {x for x in values}
        mapping = {x: x for x in values if x}
        stream = (x for x in values)
        value = selected[0] if selected else 0
        for item in stream:
            if item < 0:
                break
            continue
        try:
            match value:
                case 0:
                    return mapping
                case other if other > 1:
                    return unique
        except ValueError:
            return None
        except:
            return None
        else:
            return value
        finally:
            value = 0

def recursive(value):
    return recursive(value - 1) if value else 0
"""

        result = analysis.analyze_python_complexity("constructs.py", source)

        self.assertEqual(result.status, "supported")
        self.assertEqual(
            [item.name for item in result.functions],
            ["Worker.run", "recursive"],
        )

    def test_syntax_error_without_column_is_explicit(self):
        error = SyntaxError("broken")
        error.lineno = 9
        error.offset = None
        with mock.patch.object(analysis.ast, "parse", side_effect=error):
            result = analysis.analyze_python_complexity("broken.py", "ignored")
        self.assertEqual(result.message, "line 9")


TYPESCRIPT_SAMPLE = """export function sumOfPrimes(max: number): number {
  let total = 0;
  outer: for (let i = 1; i <= max; ++i) {
    for (let j = 2; j < i; ++j) {
      if (i % j === 0) {
        continue outer;
      }
    }
    total += i;
  }
  return total;
}
export const pick = (a?: string, b?: string) => (a && b) || (a ? a : b);
class Store {
  load(x: number) {
    try { if (x > 1) { return 1; } else if (x < 0) { return 2; } else { return 3; } }
    catch (e) { return x > 0 && x < 5 && x !== 3 ? 1 : 0; }
  }
}
"""
VUE_SAMPLE = """<template><div>{{ a && b }}</div></template>
<script setup lang="ts">
const label = computed(() => {
  switch (mode.value) { case 1: return 'a'; default: return 'b'; }
});
</script>
"""


def typescript_available(root):
    node = shutil.which("node")
    if node is None:
        return False
    probe = subprocess.run(
        [node, "-e", "require('typescript')"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


class ScriptComplexityTests(unittest.TestCase):
    def write_project(self, root):
        (root / "package.json").write_text("{}", encoding="utf-8")
        (root / "a.ts").write_text(TYPESCRIPT_SAMPLE, encoding="utf-8")
        (root / "C.vue").write_text(VUE_SAMPLE, encoding="utf-8")
        (root / "bad.ts").write_text("const = ;\n", encoding="utf-8")

    def test_typescript_and_vue_scores_follow_cognitive_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_project(root)
            if not typescript_available(root):
                self.skipTest("node with a resolvable typescript package is required")
            result = analysis.analyze_script_complexity(
                root, ["a.ts", "C.vue", "bad.ts", "README.md"]
            )

        scores = {
            (item.path, function.name): (
                function.cognitive_complexity,
                function.max_nesting,
            )
            for item in result.values()
            for function in item.functions
        }
        self.assertEqual(scores[("a.ts", "sumOfPrimes")], (7, 3))
        self.assertEqual(scores[("a.ts", "pick")], (3, 1))
        self.assertEqual(scores[("a.ts", "Store.load")], (6, 2))
        self.assertEqual(scores[("C.vue", "computed(callback)")], (1, 1))
        self.assertEqual(result["bad.ts"].status, "parse_error")
        self.assertNotIn("README.md", result)

    def test_missing_helper_or_node_keeps_scripts_unsupported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = analysis.analyze_script_complexity(
                root, ["a.ts"], helper=root / "absent.mjs"
            )
            with mock.patch.object(analysis.shutil, "which", return_value=None):
                no_node = analysis.analyze_script_complexity(root, ["b.vue"])

        self.assertEqual(missing["a.ts"].status, "unsupported")
        self.assertIn("needs absent.mjs", missing["a.ts"].message)
        self.assertEqual(no_node["b.vue"].status, "unsupported")
        self.assertIn("Node.js", no_node["b.vue"].message)
        self.assertEqual(analysis.analyze_script_complexity(Path("."), ["x.py"]), {})

    def test_helper_failures_and_timeouts_keep_scripts_unsupported(self):
        helper = Path(__file__)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom\nlast line")
        silent = subprocess.CompletedProcess([], 2, stdout="", stderr="")
        timeout = subprocess.TimeoutExpired(["node"], 600)
        outcomes = {}
        with mock.patch.object(analysis.shutil, "which", return_value="/bin/node"):
            for label, effect in (
                ("failed", [failed]),
                ("silent", [silent]),
                ("timeout", timeout),
            ):
                with mock.patch.object(analysis.subprocess, "run", side_effect=effect):
                    result = analysis.analyze_script_complexity(
                        Path("."), ["a.ts"], helper
                    )
                outcomes[label] = result["a.ts"]

        self.assertTrue(all(item.status == "unsupported" for item in outcomes.values()))
        self.assertIn("helper failed: last line", outcomes["failed"].message)
        self.assertIn("helper failed: no output", outcomes["silent"].message)
        self.assertIn("helper timed out after 600 seconds", outcomes["timeout"].message)

    def test_portable_sources_use_script_results_only_with_a_root(self):
        sources = {"a.ts": "export const x = 1;\n", "m.py": "def f():\n    return 1\n"}
        measured = analysis.SourceComplexity("a.ts", "typescript", "supported", ())
        with mock.patch.object(
            analysis, "analyze_script_complexity", return_value={"a.ts": measured}
        ) as adapter:
            with_root = analysis.analyze_portable_sources(
                sources, script_root=Path(".")
            )
            without_root = analysis.analyze_portable_sources(sources)

        by_path = {item.path: item.status for item in with_root.complexity}
        self.assertEqual(by_path, {"a.ts": "supported", "m.py": "supported"})
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(
            {item.path: item.status for item in without_root.complexity}["a.ts"],
            "unsupported",
        )


class DuplicationTests(unittest.TestCase):
    def test_reports_actionable_pairs_and_union_percentage(self):
        sources = {
            "one.py": """\
def total(values):
    result = 0
    for value in values:
        result += value
    return result
""",
            "two.py": """\
def total(values):
        result = 0
        for value in values:
            result += value
        return result
print('unique')
""",
        }

        report = analysis.find_duplicate_blocks(sources, minimum_lines=4)

        self.assertEqual(len(report.pairs), 1)
        pair = report.pairs[0]
        self.assertEqual(pair.line_count, 5)
        self.assertEqual(
            (pair.first.path, pair.first.start_line, pair.first.end_line),
            ("one.py", 1, 5),
        )
        by_path = {item.path: item for item in report.files}
        self.assertEqual(by_path["one.py"].duplicated_lines, 5)
        self.assertEqual(by_path["one.py"].percentage, 100.0)
        self.assertEqual(by_path["two.py"].duplicated_lines, 5)
        self.assertAlmostEqual(by_path["two.py"].percentage, 5 / 6 * 100)

    def test_comments_and_blank_runs_do_not_create_duplicate_code(self):
        sources = {
            "a.py": "# repeated\n# repeated\n# repeated\n\n",
            "b.py": "# repeated\n# repeated\n# repeated\n\n",
        }

        report = analysis.find_duplicate_blocks(sources, minimum_lines=3)

        self.assertEqual(report.pairs, ())
        self.assertEqual(report.total_code_lines, 0)
        self.assertEqual(report.percentage, 0.0)

    def test_same_file_overlap_is_ignored_and_pair_limit_is_explicit(self):
        repeated = "\n".join(["alpha", "beta", "gamma"] * 3)
        report = analysis.find_duplicate_blocks(
            {"same.py": repeated, "other.py": repeated},
            minimum_lines=3,
            maximum_pairs=1,
        )

        self.assertEqual(len(report.pairs), 1)
        self.assertTrue(report.truncated)
        self.assertGreater(report.total_pair_count, 1)

    def test_rejects_invalid_duplicate_limits(self):
        with self.assertRaisesRegex(ValueError, "at least 2"):
            analysis.find_duplicate_blocks({}, minimum_lines=1)
        with self.assertRaisesRegex(ValueError, "positive"):
            analysis.find_duplicate_blocks({}, maximum_pairs=0)

    def test_containment_helper_distinguishes_maximal_ranges(self):
        kept = ("a.py", 0, 10, "b.py", 2, 12)
        inside = ("a.py", 1, 9, "b.py", 3, 11)
        other = ("a.py", 1, 9, "c.py", 3, 11)

        self.assertTrue(analysis._contains_duplicate(kept, inside))
        self.assertFalse(analysis._contains_duplicate(kept, other))
        self.assertEqual(analysis._maximal_candidates({kept, inside}), [kept])


class DependencyTests(unittest.TestCase):
    def test_reports_cycles_fan_metrics_and_ignored_external_edges(self):
        report = analysis.analyze_dependency_graph(
            modules=("a.py", "b.py", "c.py", "leaf.py"),
            edges=(
                analysis.ResolvedDependency("a.py", "b.py"),
                analysis.ResolvedDependency("b.py", "c.py"),
                analysis.ResolvedDependency("c.py", "a.py"),
                analysis.ResolvedDependency("c.py", "leaf.py"),
                analysis.ResolvedDependency("c.py", "third_party"),
            ),
        )

        self.assertEqual(report.ignored_external_edges, 1)
        self.assertEqual(len(report.cycles), 1)
        self.assertEqual(report.cycles[0].members, ("a.py", "b.py", "c.py"))
        cycle_path = report.cycles[0].example_path
        self.assertEqual(cycle_path[0], cycle_path[-1])
        self.assertEqual(set(cycle_path[:-1]), {"a.py", "b.py", "c.py"})
        metrics = {item.module: item for item in report.modules}
        self.assertEqual((metrics["c.py"].fan_in, metrics["c.py"].fan_out), (1, 2))
        self.assertEqual(
            (metrics["leaf.py"].fan_in, metrics["leaf.py"].fan_out), (1, 0)
        )

    def test_large_graph_does_not_depend_on_python_recursion_depth(self):
        modules = tuple(f"module_{index}" for index in range(1_500))
        edges = tuple(
            analysis.ResolvedDependency(
                modules[index], modules[(index + 1) % len(modules)]
            )
            for index in range(len(modules))
        )

        report = analysis.analyze_dependency_graph(modules, edges)

        self.assertEqual(len(report.cycles), 1)
        self.assertEqual(len(report.cycles[0].members), len(modules))
        self.assertEqual(len(report.cycles[0].example_path), len(modules) + 1)

    def test_self_cycle_and_diamond_graph_cover_iterative_edges(self):
        report = analysis.analyze_dependency_graph(
            ("a", "b", "c", "d"),
            (
                analysis.ResolvedDependency("a", "a"),
                analysis.ResolvedDependency("a", "b"),
                analysis.ResolvedDependency("a", "c"),
                analysis.ResolvedDependency("b", "d"),
                analysis.ResolvedDependency("c", "d"),
            ),
        )
        self.assertEqual(report.cycles[0].example_path, ("a", "a"))

    def test_impossible_cycle_request_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "did not contain"):
            graph._example_cycle({"a": {"b"}, "b": set()}, ("a", "b"))

    def test_graph_walk_helpers_ignore_already_seen_nodes(self):
        order = []
        graph._finish_walk("seen", {"seen": set()}, {"seen"}, order)
        self.assertEqual(order, [])

        parents = {"target": None}
        pending = graph.deque()
        graph._queue_cycle_targets(
            {"node": {"target"}},
            "node",
            "start",
            {"node", "target"},
            parents,
            pending,
        )
        self.assertEqual(list(pending), [])


class SecretTests(unittest.TestCase):
    def test_secret_values_are_never_retained_or_emitted(self):
        aws_secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        github_secret = "ghp_" + "a" * 36
        source = f'aws = "{aws_secret}"\ngithub = "{github_secret}"\n'

        findings = analysis.scan_secrets({"settings.py": source})

        self.assertEqual(
            [item.kind for item in findings], ["aws_access_key", "github_token"]
        )
        serialized = repr([dataclasses.asdict(item) for item in findings])
        self.assertNotIn(aws_secret, serialized)
        self.assertNotIn(github_secret, serialized)
        self.assertTrue(all(not hasattr(item, "value") for item in findings))
        self.assertEqual(
            [(item.line, item.column) for item in findings], [(1, 8), (2, 11)]
        )

    def test_common_non_secret_identifiers_do_not_trigger(self):
        findings = analysis.scan_secrets(
            {"example.py": "token = 'test-token'\nkey = 'ABCDEFGHIJKLMNOP'\n"}
        )

        self.assertEqual(findings, ())


class HotspotTests(unittest.TestCase):
    def test_calculates_hotspots_without_reading_git(self):
        history = (
            analysis.HistoryInput("busy.py", 10, 40, 15),
            analysis.HistoryInput("quiet.py", 2, 3, 1),
            analysis.HistoryInput("unknown.go", 8, 12, 7),
        )

        hotspots = analysis.calculate_hotspots(history, {"busy.py": 7, "quiet.py": 1})

        self.assertEqual(
            [item.path for item in hotspots], ["busy.py", "quiet.py", "unknown.go"]
        )
        self.assertEqual(hotspots[0].churn, 55)
        self.assertEqual(hotspots[0].score, 80.0)
        self.assertEqual(hotspots[0].status, "measured")
        self.assertIsNone(hotspots[-1].score)
        self.assertEqual(hotspots[-1].status, "missing_complexity")

    def test_rejects_impossible_history_counts(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            analysis.calculate_hotspots(
                (analysis.HistoryInput("bad.py", -1, 0, 0),), {"bad.py": 1}
            )
        with self.assertRaisesRegex(ValueError, "complexity"):
            analysis.calculate_hotspots(
                (analysis.HistoryInput("bad.py", 1, 0, 0),), {"bad.py": -1}
            )
        duplicate = analysis.HistoryInput("same.py", 1, 0, 0)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            analysis.calculate_hotspots((duplicate, duplicate), {})


class AggregateTests(unittest.TestCase):
    def test_aggregate_keeps_all_missing_evidence_explicit(self):
        report = analysis.analyze_portable_sources(
            sources={
                "valid.py": "def ok():\n    return True\n",
                "main.go": "package main",
            },
            languages={"valid.py": "python", "main.go": "go"},
            resolved_edges=(analysis.ResolvedDependency("valid.py", "main.go"),),
            history=(analysis.HistoryInput("main.go", 3, 4, 2),),
        )

        statuses = {item.path: item.status for item in report.complexity}
        self.assertEqual(statuses, {"main.go": "unsupported", "valid.py": "supported"})
        self.assertEqual(report.dependencies.ignored_external_edges, 0)
        self.assertEqual(report.hotspots[0].status, "missing_complexity")


if __name__ == "__main__":
    unittest.main()
