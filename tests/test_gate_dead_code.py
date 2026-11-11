import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_dead_code  # noqa: E402
import gate_dead_code_references  # noqa: E402
from diff_scope import (  # noqa: E402
    LOCAL_CHANGES,
    ChangeSet,
    FileChange,
    local_changes,
    repository_changes,
)
from gate_types import BLOCKED, FAIL, PASS, FunctionInfo  # noqa: E402


def function(path, name, start, end=None, **extra):
    return FunctionInfo(path, name, name + "()", start, end or start + 1, **extra)


def changes(*file_changes, deleted=frozenset()):
    files = {change.path: change for change in file_changes}
    return ChangeSet(LOCAL_CHANGES, "base", files, frozenset(deleted))


def git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class RepositoryFixture(unittest.TestCase):
    """A temporary repository whose files the tests write by hand."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()

    def tearDown(self):
        self._directory.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def lines_for(self, path):
        target = self.root / path
        return target.read_text().splitlines() if target.exists() else []

    def evaluate(self, functions, change_set, files=None):
        if files is None:
            files = gate_dead_code_references.discover_reference_files(self.root)
        return gate_dead_code.evaluate_dead_code(
            self.root, functions, change_set, files
        )

    def commit_all(self):
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")


class NameTests(unittest.TestCase):
    def test_identifiers_keep_dollar_signs_as_part_of_the_name(self):
        self.assertEqual(
            gate_dead_code_references.identifiers("x = $total + total(1)"),
            ["x", "$total", "total", "1"],
        )

    def test_search_name_drops_ruby_suffixes_and_qualifiers(self):
        self.assertEqual(
            gate_dead_code.search_name(function("a.rb", "Cart::valid?", 1)), "valid"
        )
        self.assertEqual(
            gate_dead_code.search_name(function("a.rb", "save!", 1)), "save"
        )

    def test_ruby_setter_is_searched_without_its_suffix_and_still_checked(self):
        setter = function("a.rb", "total=", 1)
        self.assertEqual(gate_dead_code.search_name(setter), "total")
        self.assertIsNone(gate_dead_code.exclusion_reason(setter))

    def test_name_position_matches_whole_identifiers_only(self):
        self.assertEqual(
            gate_dead_code_references.name_position("x = $total + total", "total"), 13
        )
        self.assertIsNone(
            gate_dead_code_references.name_position("total_cost", "total")
        )


class ReferencePathTests(unittest.TestCase):
    def test_source_templates_scripts_and_configuration_are_references(self):
        for path in (
            "src/app.py",
            "index.html",
            "app/views/show.html.erb",
            "pyproject.toml",
            ".github/workflows/ci.yml",
            "bin/deploy",
            "Makefile",
            "notebook.ipynb",
            "CHANGES.py",
        ):
            self.assertTrue(gate_dead_code_references.is_reference_path(path), path)

    def test_prose_lockfiles_and_data_are_not_references(self):
        for path in (
            "README.md",
            "docs/guide.rst",
            "notes.txt",
            "yarn.lock",
            "go.sum",
            "package-lock.json",
            "LICENSE",
            "CHANGELOG",
            "data/rows.csv",
            "dist/app.js.map",
        ):
            self.assertFalse(gate_dead_code_references.is_reference_path(path), path)


class DefinitionShapeTests(unittest.TestCase):
    def test_same_prefix_declares_the_function(self):
        shape = gate_dead_code.DefinitionShape.of("def render(a, b):", "render")
        self.assertTrue(shape.matches("def render(a):", "render"))
        self.assertFalse(shape.matches("    return render(a)", "render"))
        self.assertFalse(shape.matches("unrelated()", "render"))

    def test_bare_name_lines_must_also_end_like_the_definition(self):
        shape = gate_dead_code.DefinitionShape.of("  helper(a, b) {", "helper")
        self.assertTrue(shape.matches("  helper(a) {", "helper"))
        self.assertFalse(shape.matches("  helper(a);", "helper"))

    def test_definition_without_the_name_has_no_shape(self):
        self.assertIsNone(gate_dead_code.DefinitionShape.of("", "helper"))


class RemovedLinesTests(RepositoryFixture):
    def test_only_lines_from_reference_files_are_collected(self):
        change_set = changes(
            FileChange("a.py", removed=("    compute(x)",)),
            FileChange("README.md", removed=("Call serve() to start.",)),
        )
        removed = gate_dead_code.removed_lines(self.root, change_set)
        self.assertEqual(removed.mentioning("compute"), ("    compute(x)",))
        self.assertEqual(removed.mentioning("serve"), ())

    def test_deleted_files_contribute_their_whole_base_text(self):
        self.write("src/lib.py", "def _compute():\n    return 1\n")
        self.write("src/app.py", "from lib import _compute\nvalue = _compute()\n")
        self.write("docs/notes.md", "_compute is internal\n")
        self.commit_all()
        git(self.root, "rm", "-q", "src/app.py", "docs/notes.md")
        change_set = local_changes(self.root)
        self.assertEqual(change_set.deleted, {"src/app.py", "docs/notes.md"})
        removed = gate_dead_code.removed_lines(self.root, change_set)
        self.assertEqual(len(removed.mentioning("_compute")), 2)


class CandidateTests(RepositoryFixture):
    def select(self, functions, change_set):
        removed = gate_dead_code.removed_lines(self.root, change_set)
        return gate_dead_code.select_candidates(
            functions, change_set, removed, self.lines_for
        )

    def test_repository_scope_checks_every_function(self):
        functions = [function("a.py", "one", 1), function("b.py", "two", 4)]
        candidates = self.select(functions, repository_changes())
        self.assertEqual([c.reason for c in candidates], ["repository"] * 2)

    def test_incremental_scope_checks_new_and_orphaned_functions_only(self):
        self.write("a.py", "\n\ndef fresh():\n    pass\n")
        self.write("b.py", "def compute():\n    pass\n")
        functions = [
            function("a.py", "fresh", 3),
            function("b.py", "compute", 1),
            function("b.py", "untouched", 20),
        ]
        change_set = changes(
            FileChange("a.py", added=frozenset({3, 4})),
            FileChange("c.py", removed=("    return compute(1)",)),
        )
        candidates = self.select(functions, change_set)
        self.assertEqual(
            [(c.function.name, c.reason) for c in candidates],
            [("fresh", "new"), ("compute", "orphan")],
        )

    def test_changed_body_without_new_definition_is_not_new(self):
        functions = [function("a.py", "steady", 1, 5)]
        change_set = changes(FileChange("a.py", added=frozenset({3})))
        self.assertEqual(self.select(functions, change_set), [])

    def test_edited_signature_is_not_a_new_function(self):
        self.write("lib.py", "def render(template, values):\n    pass\n")
        functions = [function("lib.py", "render", 1)]
        change_set = changes(
            FileChange(
                "lib.py", added=frozenset({1}), removed=("def render(template):",)
            )
        )
        self.assertEqual(self.select(functions, change_set), [])

    def test_edited_signature_with_a_removed_caller_is_an_orphan(self):
        self.write("lib.py", "def render(template, values):\n    pass\n")
        functions = [function("lib.py", "render", 1)]
        change_set = changes(
            FileChange(
                "lib.py",
                added=frozenset({1}),
                removed=("def render(template):", "page = render(t)"),
            )
        )
        self.assertEqual(
            [c.reason for c in self.select(functions, change_set)], ["orphan"]
        )

    def test_removing_only_another_declaration_is_not_an_orphan(self):
        self.write("lib.py", "def render():\n    pass\n")
        functions = [function("lib.py", "render", 1)]
        change_set = changes(FileChange("old.py", removed=("def render():",)))
        self.assertEqual(self.select(functions, change_set), [])

    def test_documentation_edits_never_make_orphans(self):
        self.write("src/cli.py", "def serve():\n    pass\n")
        functions = [function("src/cli.py", "serve", 1)]
        change_set = changes(
            FileChange("README.md", removed=("The serve command starts it.",))
        )
        self.assertEqual(self.select(functions, change_set), [])


class ExclusionTests(unittest.TestCase):
    def reason(self, name, path="src/a.py"):
        return gate_dead_code.exclusion_reason(function(path, name, 1))

    def test_names_that_are_not_plain_identifiers_are_skipped(self):
        for name in ("~Cart", "operator==", "(anonymous)"):
            self.assertEqual(self.reason(name), "not a plain identifier", name)

    def test_short_names_are_skipped(self):
        self.assertEqual(self.reason("go"), "name too short to search")

    def test_entry_points_dunders_and_sunders_are_skipped(self):
        for name in ("main", "init", "new", "constructor", "__str__", "_missing_"):
            self.assertEqual(self.reason(name), "entry point or language hook", name)

    def test_methods_named_after_their_class_are_constructors(self):
        self.assertEqual(self.reason("Cart::Cart"), "constructor")
        self.assertEqual(self.reason("shop.Cart.Cart"), "constructor")
        self.assertFalse(gate_dead_code.is_constructor(function("a", "Cart.total", 1)))

    def test_functions_in_test_files_are_skipped(self):
        self.assertEqual(self.reason("helper", "tests/helpers.py"), "in a test file")
        self.assertEqual(self.reason("helper", "src/cart.test.ts"), "in a test file")

    def test_test_like_names_in_production_files_are_checked(self):
        for name in ("test_total", "testConnection", "ExampleConfig", "setUp"):
            self.assertIsNone(self.reason(name, "src/app.go"), name)

    def test_ordinary_names_are_checked(self):
        for name in ("testify", "_helper", "compute_total", "Cart::total"):
            self.assertIsNone(self.reason(name), name)


class DecoratorTests(unittest.TestCase):
    def decorated(self, text, start_line, language="Python"):
        return gate_dead_code.has_decorator(text.splitlines(), start_line, language)

    def test_decorator_directly_above_counts(self):
        self.assertTrue(self.decorated('@app.route("/x")\ndef handler():\n', 2))

    def test_multi_line_decorator_counts(self):
        text = '@app.route(\n    "/x",\n    methods=["GET"],\n)\ndef handler():\n'
        self.assertTrue(self.decorated(text, 5))

    def test_rust_and_csharp_attributes_count(self):
        self.assertTrue(self.decorated("#[no_mangle]\nfn exported() {}\n", 2, "Rust"))
        self.assertTrue(self.decorated("[HttpGet]\nprivate void Get() {}\n", 2, "C#"))

    def test_annotation_on_the_definition_line_counts(self):
        self.assertTrue(
            self.decorated("@Override public String name() {}\n", 1, "Java")
        )

    def test_array_literal_is_not_an_attribute_outside_csharp(self):
        text = "const pairs = [\n  [1, 2],\n]\nfunction helper () {}\n"
        self.assertFalse(self.decorated(text, 4, "JavaScript"))

    def test_ruby_instance_variable_is_not_a_decorator(self):
        text = "class Cart\n  @count = 0\n  def helper\n"
        self.assertFalse(self.decorated(text, 3, "Ruby"))
        self.assertFalse(self.decorated(text, 3, None))

    def test_blank_line_or_previous_block_stops_the_search(self):
        self.assertFalse(self.decorated("@first\ndef a():\n    pass\n\ndef b():\n", 5))
        self.assertFalse(self.decorated("@x\ndef a():\ndef b():\n", 3))
        self.assertFalse(self.decorated("@Bean\nint a() {}\nint b() {}\n", 3, "Java"))

    def test_top_of_file_and_out_of_range_lines_have_no_decorator(self):
        self.assertFalse(self.decorated("def a():\n", 1))
        self.assertFalse(self.decorated("x = 1\n", 30))

    def test_search_gives_up_after_the_lookback_window(self):
        text = "@far\n" + "value,\n" * 11 + "def late():\n"
        self.assertFalse(self.decorated(text, 13))


class VisibilityTests(unittest.TestCase):
    def kind(self, path, name, definition):
        return gate_dead_code.visibility(function(path, name, 1), definition)

    def test_leading_underscore_is_private_everywhere(self):
        self.assertEqual(self.kind("a.py", "_helper", "def _helper():"), "private")
        self.assertEqual(self.kind("a.dart", "_build", "void _build() {"), "private")

    def test_go_uses_capitalization(self):
        self.assertEqual(self.kind("a.go", "Total", "func Total() int {"), "exported")
        self.assertEqual(self.kind("a.go", "total", "func total() int {"), "private")

    def test_rust_uses_unrestricted_pub(self):
        self.assertEqual(self.kind("a.rs", "total", "pub fn total()"), "exported")
        self.assertEqual(self.kind("a.rs", "total", "fn total() {"), "private")
        for restricted in ("pub(crate)", "pub(super)", "pub(in crate::a)"):
            definition = f"{restricted} fn total()"
            self.assertEqual(self.kind("a.rs", "total", definition), "private")

    def test_scripts_use_export(self):
        self.assertEqual(
            self.kind("a.ts", "total", "export function total() {"), "exported"
        )
        self.assertEqual(
            self.kind("a.js", "total", "module.exports.total = function total() {"),
            "exported",
        )
        self.assertEqual(self.kind("a.vue", "total", "function total() {"), "private")

    def test_modifier_languages_use_keywords_before_the_name(self):
        self.assertEqual(
            self.kind("A.java", "total", "public int total() {"), "exported"
        )
        self.assertEqual(self.kind("A.kt", "total", "internal fun total()"), "exported")
        self.assertEqual(
            self.kind("A.swift", "total", "fileprivate func total()"), "private"
        )
        self.assertEqual(self.kind("a.rb", "total", "private def total"), "private")
        self.assertEqual(self.kind("A.java", "total", "int total() {"), "default")
        self.assertEqual(
            self.kind("A.java", "total", "int total(boolean private_) {"), "default"
        )

    def test_c_static_is_private(self):
        self.assertEqual(self.kind("a.c", "total", "static int total(void)"), "private")
        self.assertEqual(self.kind("a.cpp", "total", "int total()"), "default")

    def test_python_and_unknown_languages_default_to_public(self):
        self.assertEqual(
            self.kind("a.py", "total", "def total(private=True):"), "default"
        )
        self.assertEqual(self.kind("a.xyz", "total", "total"), "default")

    def test_private_members_are_marked_by_name_or_keyword(self):
        def private(path, name, definition):
            return gate_dead_code.is_private_member(
                function(path, name, 1, is_method=True), definition
            )

        self.assertTrue(private("a.py", "_helper", "    def _helper(self):"))
        self.assertTrue(private("A.java", "Cart::help", "  private int help() {"))
        self.assertTrue(private("a.ts", "help", "  private help() {"))
        self.assertFalse(private("A.java", "Cart::help", "  int help(int private_) {"))
        self.assertFalse(private("a.go", "help", "func (c *Cart) help() {"))


class DefinitionTextTests(unittest.TestCase):
    def text(self, path, source, start):
        return gate_dead_code.definition_text(
            function(path, "helper", start), source.splitlines()
        )

    def test_gnu_style_c_joins_the_return_type_line(self):
        source = "#include <x.h>\nstatic int\nhelper(void)\n{\n"
        self.assertEqual(self.text("a.c", source, 3), "static int helper(void)")

    def test_c_head_stops_at_statements_comments_blank_lines_and_the_limit(self):
        self.assertEqual(self.text("a.c", "int x;\nhelper(void)\n", 2), "helper(void)")
        self.assertEqual(self.text("a.c", "/* doc */\nhelper()\n", 2), "helper()")
        self.assertEqual(self.text("a.c", "\nhelper()\n", 2), "helper()")
        source = "a\nb\nc\nd\nhelper()\n"
        self.assertEqual(self.text("a.c", source, 5), "b c d helper()")

    def test_other_languages_use_only_the_definition_line(self):
        self.assertEqual(self.text("a.py", "x\ndef helper():\n", 2), "def helper():")


class ReferenceIndexTests(RepositoryFixture):
    def test_index_counts_wanted_names_and_lists_unreadable_files(self):
        self.write("a.py", "def total():\n    return total_cost + total\n")
        absolute = self.write("b.py", "total()\nother()\n")
        index = gate_dead_code_references.build_reference_index(
            self.root,
            [Path("a.py"), absolute, Path("missing.py")],
            ["total"],
        )
        self.assertEqual(index.counts["total"], 3)
        self.assertNotIn("other", index.counts)
        self.assertEqual(index.indexed_paths, frozenset({"a.py", "b.py"}))
        self.assertEqual(index.unreadable, ("missing.py",))

    def count(self, path, text, name, start, end):
        self.write(path, text)
        index = gate_dead_code_references.build_reference_index(
            self.root, [Path(path)], [name]
        )
        target = function(path, name, start, end)
        return gate_dead_code.reference_count(index, target, text.splitlines())

    def test_own_body_recursion_and_docstrings_are_not_references(self):
        recursive = "def _walk(n):\n    return 0 if n <= 0 else _walk(n - 1)\n"
        self.assertEqual(self.count("a.py", recursive, "_walk", 1, 2), 0)
        documented = 'def _double(x):\n    """_double doubles x."""\n    return x\n'
        self.assertEqual(self.count("b.py", documented, "_double", 1, 3), 0)

    def test_header_prototypes_are_not_references_but_calls_are(self):
        self.write("calc.h", "int calc_abs(int value);\n")
        self.write("calc.c", "int calc_abs(int value)\n{\n  return value;\n}\n")
        self.write("main.c", "int main(void) { return calc_abs(-1); }\n")
        target = function("calc.c", "calc_abs", 1, 4)
        lines = (self.root / "calc.c").read_text().splitlines()

        def count(files):
            index = gate_dead_code_references.build_reference_index(
                self.root, [Path(name) for name in files], ["calc_abs"]
            )
            return gate_dead_code.reference_count(index, target, lines)

        self.assertEqual(count(["calc.h", "calc.c"]), 0)
        self.assertEqual(count(["calc.h", "calc.c", "main.c"]), 1)

    def test_prototypes_and_overloads_in_the_same_file_are_not_references(self):
        source = "static int helper(int);\n\nstatic int helper(int x)\n{\n}\n"
        self.assertEqual(self.count("a.c", source, "helper", 3, 5), 0)
        overload = (
            "function pick(a: string): string;\n"
            "function pick(a: any) {\n  return a;\n}\n"
        )
        self.assertEqual(self.count("a.ts", overload, "pick", 2, 4), 0)

    def test_real_callers_in_the_same_file_still_count(self):
        source = "def _helper():\n    pass\n\nvalue = _helper()\n"
        self.assertEqual(self.count("a.py", source, "_helper", 1, 2), 1)

    def test_files_outside_the_index_keep_the_full_count(self):
        self.write("a.py", "total()\n")
        index = gate_dead_code_references.build_reference_index(
            self.root, [Path("a.py")], ["total"]
        )
        outside = function("other.py", "total", 1)
        self.assertEqual(gate_dead_code.reference_count(index, outside, []), 1)

    def test_discover_reference_files_keeps_code_bearing_files_including_tests(self):
        self.write("src/app.py", "x = 1\n")
        self.write("tests/test_app.py", "x = 2\n")
        self.write("index.html", "<p></p>\n")
        self.write("README.md", "docs\n")
        found = gate_dead_code_references.discover_reference_files(self.root)
        names = sorted(path.relative_to(self.root).as_posix() for path in found)
        self.assertEqual(names, ["index.html", "src/app.py", "tests/test_app.py"])

    def test_relative_root_finds_the_same_files(self):
        self.write("src/app.py", "x = 1\n")
        previous = Path.cwd()
        os.chdir(self.root.parent)
        try:
            relative = Path(self.root.name)
            found = gate_dead_code_references.discover_reference_files(relative)
            index = gate_dead_code_references.build_reference_index(
                relative, found, ["x"]
            )
        finally:
            os.chdir(previous)
        self.assertEqual(index.indexed_paths, frozenset({"src/app.py"}))
        self.assertEqual(index.unreadable, ())


class IncrementalEvaluationTests(RepositoryFixture):
    def test_new_unreferenced_python_function_fails(self):
        self.write(
            "src/app.py", "def run():\n    return 1\n\n\ndef helper():\n    pass\n"
        )
        functions = [
            function("src/app.py", "run", 1),
            function("src/app.py", "helper", 5),
        ]
        outcome = self.evaluate(
            functions, changes(FileChange("src/app.py", added=frozenset({5, 6})))
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual([item.name for item in outcome.items], ["helper"])
        item = outcome.items[0]
        self.assertEqual((item.metric, item.value, item.line), ("references", "0", 5))
        self.assertEqual(outcome.summary, "Functions that no production code calls: 1.")
        self.assertEqual(outcome.measurements["new_functions"], 1)
        self.assertEqual(outcome.measurements["unused"], 1)

    def test_new_function_called_only_from_a_test_fails(self):
        self.write("src/app.py", "def compute_total():\n    return 1\n")
        self.write("tests/test_app.py", "from app import compute_total\n")
        functions = [function("src/app.py", "compute_total", 1)]
        outcome = self.evaluate(
            functions, changes(FileChange("src/app.py", is_new=True, all_lines=True))
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(
            [(item.name, item.value) for item in outcome.items],
            [("compute_total", "0 (only tests call it)")],
        )

    def test_function_called_by_production_and_tests_passes(self):
        self.write("src/app.py", "def compute_total():\n    return 1\n")
        self.write("src/main.py", "from app import compute_total\ncompute_total()\n")
        self.write("tests/test_app.py", "from app import compute_total\n")
        functions = [function("src/app.py", "compute_total", 1)]
        outcome = self.evaluate(
            functions, changes(FileChange("src/app.py", is_new=True, all_lines=True))
        )
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(
            outcome.summary, "Checked functions with a production caller: 1 of 1."
        )

    def test_exported_function_called_only_from_a_test_is_noted(self):
        self.write("src/api.ts", "export function total() {\n  return 1;\n}\n")
        self.write("src/api.test.ts", "import { total } from './api';\n")
        functions = [function("src/api.ts", "total", 1)]
        outcome = self.evaluate(
            functions, changes(FileChange("src/api.ts", is_new=True, all_lines=True))
        )
        self.assertEqual(outcome.status, PASS)
        self.assertIn("total: only tests call it, not failed", outcome.details[0])

    def test_function_called_only_from_a_template_passes(self):
        self.write("static/app.js", "function handleSubmit(event) {\n}\n")
        self.write("index.html", '<form onsubmit="return handleSubmit(event)">\n')
        functions = [function("static/app.js", "handleSubmit", 1)]
        change_set = changes(FileChange("static/app.js", is_new=True, all_lines=True))
        self.assertEqual(self.evaluate(functions, change_set).status, PASS)

    def test_orphaned_function_fails_when_its_last_caller_was_removed(self):
        self.write("src/lib.py", "def compute():\n    return 2\n")
        self.write("src/app.py", "def run():\n    return 1\n")
        functions = [
            function("src/lib.py", "compute", 1),
            function("src/app.py", "run", 1),
        ]
        change_set = changes(
            FileChange(
                "src/app.py", added=frozenset({2}), removed=("    return compute()",)
            )
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].name, "compute")
        self.assertIn("removed its last reference", outcome.items[0].value)
        self.assertEqual(outcome.measurements["orphan_candidates"], 1)

    def test_deleting_the_file_with_the_last_caller_fails_the_orphan(self):
        self.write("src/report.py", "def _format_row(row):\n    return row\n")
        self.write("src/legacy.py", "from report import _format_row\n")
        self.commit_all()
        git(self.root, "rm", "-q", "src/legacy.py")
        functions = [function("src/report.py", "_format_row", 1)]
        outcome = self.evaluate(functions, local_changes(self.root))
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].name, "_format_row")
        self.assertIn("removed its last reference", outcome.items[0].value)

    def test_documentation_only_change_passes(self):
        self.write("src/cli.py", "def serve():\n    pass\n")
        functions = [function("src/cli.py", "serve", 1)]
        change_set = changes(
            FileChange("README.md", removed=("The serve command starts it.",))
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.measurements["functions_checked"], 0)

    def test_orphan_that_still_has_another_caller_passes(self):
        self.write("src/lib.py", "def compute():\n    return 2\n")
        self.write("src/other.py", "value = compute()\n")
        functions = [function("src/lib.py", "compute", 1)]
        change_set = changes(FileChange("src/app.py", removed=("compute()",)))
        self.assertEqual(self.evaluate(functions, change_set).status, PASS)

    def test_unreferenced_private_methods_fail(self):
        self.write(
            "src/cart.py",
            "class Cart:\n    def _never_called(self):\n        pass\n",
        )
        self.write(
            "src/Cart.java",
            "class Cart {\n    private int javaOnlyHelper() {\n        return 1;\n"
            "    }\n}\n",
        )
        functions = [
            function("src/cart.py", "_never_called", 2, 3, is_method=True),
            function("src/Cart.java", "Cart::javaOnlyHelper", 2, 4, is_method=True),
        ]
        change_set = changes(
            FileChange("src/cart.py", is_new=True, all_lines=True),
            FileChange("src/Cart.java", is_new=True, all_lines=True),
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(
            [item.name for item in outcome.items],
            ["Cart::javaOnlyHelper", "_never_called"],
        )

    def test_unreferenced_but_possibly_external_functions_are_only_noted(self):
        self.write(
            "src/app.py",
            '@app.route("/")\ndef index_page():\n    pass\n\n'
            "class Cart:\n    def total(self):\n        pass\n",
        )
        self.write("pkg/api.go", "package api\n\nfunc Exported() int { return 1 }\n")
        functions = [
            function("src/app.py", "index_page", 2),
            function("src/app.py", "total", 6, is_method=True),
            function("pkg/api.go", "Exported", 3),
        ]
        change_set = changes(
            FileChange("src/app.py", is_new=True, all_lines=True),
            FileChange("pkg/api.go", is_new=True, all_lines=True),
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(outcome.status, PASS)
        notes = "\n".join(outcome.details)
        self.assertIn("index_page: no references found", notes)
        self.assertIn("may be registered dynamically", notes)
        self.assertIn("method; may be called through an interface", notes)
        self.assertIn("exported; may be used by other projects", notes)
        self.assertEqual(outcome.measurements["unreferenced_not_failed"], 3)

    def test_excluded_functions_are_never_checked_but_counted(self):
        self.write("tests/test_app.py", "def helper():\n    pass\n")
        self.write("src/app.py", "def main():\n    pass\n")
        functions = [
            function("tests/test_app.py", "helper", 1),
            function("src/app.py", "main", 1),
        ]
        change_set = changes(
            FileChange("tests/test_app.py", is_new=True, all_lines=True),
            FileChange("src/app.py", is_new=True, all_lines=True),
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.measurements["functions_checked"], 0)
        self.assertEqual(outcome.measurements["excluded_functions"], 2)
        self.assertEqual(
            outcome.summary, "The change adds no functions and removes no callers."
        )

    def test_function_in_a_missing_file_is_judged_without_its_text(self):
        functions = [function("src/gone.py", "vanished", 3)]
        change_set = changes(FileChange("src/gone.py", is_new=True, all_lines=True))
        outcome = self.evaluate(functions, change_set, files=[])
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].path, "src/gone.py")

    def test_failures_are_sorted_and_counted(self):
        self.write("src/b.py", "def _second():\n    pass\n")
        self.write("src/a.py", "def _first():\n    pass\n")
        functions = [
            function("src/b.py", "_second", 1),
            function("src/a.py", "_first", 1),
        ]
        change_set = changes(
            FileChange("src/a.py", is_new=True, all_lines=True),
            FileChange("src/b.py", is_new=True, all_lines=True),
        )
        outcome = self.evaluate(functions, change_set)
        self.assertEqual(
            [item.path for item in outcome.items], ["src/a.py", "src/b.py"]
        )
        self.assertTrue(
            outcome.summary.startswith("Functions that no production code calls: 2")
        )

    def test_new_c_function_only_declared_in_a_header_fails(self):
        self.write("src/calc.h", "int calc_abs(int value);\n")
        self.write("src/calc.c", "int calc_abs(int value)\n{\n  return value;\n}\n")
        outcome = self.evaluate(
            [function("src/calc.c", "calc_abs", 1, 4)],
            changes(
                FileChange("src/calc.c", added=frozenset({1, 2, 3, 4})),
                FileChange("src/calc.h", added=frozenset({1})),
            ),
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].name, "calc_abs")


class CDeclarationTests(unittest.TestCase):
    def test_declarations_are_recognised(self):
        declares = gate_dead_code_references.is_c_declaration
        for line in (
            "int calc_abs(int value);",
            "static inline unsigned long calc_abs(void);",
            "char *calc_abs(const char *s)",
            "extern int calc_abs (int);",
            "std::vector<int> calc_abs(int n);",
            "const Shape &Shape::calc_abs() const;",
        ):
            with self.subTest(line=line):
                self.assertTrue(declares(line, "calc_abs"))

    def test_calls_are_not_declarations(self):
        declares = gate_dead_code_references.is_c_declaration
        for line in (
            "calc_abs(x);",
            "  y = calc_abs(x);",
            "return calc_abs(x);",
            "else calc_abs(x);",
            "Widget w(calc_abs(3));",
            "std::cout << calc_abs(x);",
            "if (ok) calc_abs(x);",
            "obj->calc_abs(x);",
            "int (*fn)(int) = calc_abs;",
            "no mention here",
        ):
            with self.subTest(line=line):
                self.assertFalse(declares(line, "calc_abs"))


class RepositoryEvaluationTests(RepositoryFixture):
    def test_public_python_function_is_only_noted_in_repository_scope(self):
        self.write("src/app.py", "def public_api():\n    pass\n")
        outcome = self.evaluate(
            [function("src/app.py", "public_api", 1)], repository_changes()
        )
        self.assertEqual(outcome.status, PASS)
        self.assertIn("public; may be used by other projects", outcome.details[0])
        self.assertEqual(
            outcome.summary,
            "Checked functions with a production caller: 0 of 1. "
            "Public, so not failed: 1.",
        )

    def test_private_function_fails_in_repository_scope(self):
        self.write("src/app.py", "def _unused():\n    pass\n")
        outcome = self.evaluate(
            [function("src/app.py", "_unused", 1)], repository_changes()
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].name, "_unused")

    def test_gnu_style_static_c_function_fails(self):
        self.write("src/a.c", "static int\nunused_helper(void)\n{\n  return 1;\n}\n")
        outcome = self.evaluate(
            [function("src/a.c", "unused_helper", 2, 5)], repository_changes()
        )
        self.assertEqual(outcome.status, FAIL)

    def test_unreadable_reference_file_blocks_failures(self):
        self.write("src/app.py", "def _unused():\n    pass\n")
        files = [self.root / "src/app.py", self.root / "src/missing.py"]
        outcome = self.evaluate(
            [function("src/app.py", "_unused", 1)], repository_changes(), files
        )
        self.assertEqual(outcome.status, BLOCKED)
        self.assertIn("1 reference file could not be read", outcome.summary)
        self.assertIn("Could not read reference file src/missing.py.", outcome.details)

    def test_unreadable_reference_file_is_listed_when_everything_passes(self):
        self.write("src/app.py", "def compute():\n    pass\n\ncompute()\n")
        files = [self.root / "src/app.py", self.root / "src/missing.py"]
        outcome = self.evaluate(
            [function("src/app.py", "compute", 1)], repository_changes(), files
        )
        self.assertEqual(outcome.status, PASS)
        self.assertIn("Could not read reference file src/missing.py.", outcome.details)

    def test_repository_without_functions_passes(self):
        outcome = self.evaluate([], repository_changes())
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.summary, "No functions to check.")
        self.assertEqual(outcome.key, "dead_code")
        self.assertEqual(outcome.title, "Dead code")


class JudgeTests(unittest.TestCase):
    def test_private_function_fails_with_reason(self):
        candidate = gate_dead_code.Candidate(function("a.ts", "helper", 1), "new")
        verdict = gate_dead_code.judge(candidate, ["function helper() {"], True)
        self.assertTrue(verdict.fails)
        self.assertEqual(verdict.note, "private")

    def test_default_visibility_fails_only_in_incremental_scope(self):
        candidate = gate_dead_code.Candidate(function("a.rb", "helper", 1), "new")
        self.assertTrue(gate_dead_code.judge(candidate, ["def helper"], True).fails)
        self.assertFalse(gate_dead_code.judge(candidate, ["def helper"], False).fails)

    def test_decorated_private_method_is_only_noted(self):
        candidate = gate_dead_code.Candidate(
            function("A.java", "Cart::onEvent", 2, is_method=True), "new"
        )
        lines = ["@Subscribe", "private void onEvent() {"]
        verdict = gate_dead_code.judge(candidate, lines, True)
        self.assertFalse(verdict.fails)
        self.assertEqual(verdict.note, "may be registered dynamically")


if __name__ == "__main__":
    unittest.main()
