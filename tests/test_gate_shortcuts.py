from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import gate_shortcuts  # noqa: E402
import gate_shortcuts_handlers as handlers  # noqa: E402
import gate_shortcuts_text  # noqa: E402
import source_files  # noqa: E402

# Built from parts so coverage tools never read these fixtures as real pragmas.
NO_COVER = "# pragma" + ": no cover"
NO_BRANCH = "# pragma" + ": no branch"
NO_COVER_UPPER = "# PRAGMA" + ": NO COVER"
NO_BRANCH_MIXED = "#Pragma" + " No Branch"


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def make_repository(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def local(
    files: dict[str, diff_scope.FileChange], deleted: frozenset[str] = frozenset()
) -> diff_scope.ChangeSet:
    return diff_scope.ChangeSet(diff_scope.LOCAL_CHANGES, "base", files, deleted)


class MarkerTableTests(unittest.TestCase):
    FLAGGED = (
        ("app.py", "x = f()  # type: ignore[attr-defined]", "# type: ignore"),
        ("app.ts", "// @ts-ignore", "@ts-ignore"),
        ("app.ts", "// @ts-expect-error wrong", "@ts-expect-error"),
        ("app.ts", "// @ts-nocheck", "@ts-nocheck"),
        ("app.js", "// eslint-disable-next-line no-console", "eslint-disable"),
        ("app.js", "/* eslint-disable */", "eslint-disable"),
        ("app.ts", "// biome-ignore lint: reason", "biome-ignore"),
        ("app.ts", "// deno-lint-ignore no-explicit-any", "deno-lint-ignore"),
        ("app.py", "import os  # noqa: F401", "# noqa"),
        ("app.py", "# ruff: noqa", "# noqa"),
        ("app.py", "# flake8: noqa", "# noqa"),
        ("app.py", "import os  # noqa: E402, F401", "# noqa"),
        ("app.py", "import os  # noqa:E4", "# noqa"),
        ("app.py", "x = 1  # pylint: disable=invalid-name", "# pylint: disable"),
        ("app.py", "import os  # NOQA", "# noqa"),
        ("app.py", "import os  # NoQA: F401", "# noqa"),
        ("app.py", "# pylint: skip-file", "# pylint: disable"),
        ("app.py", "x = y  # pyright: ignore", "# pyright:"),
        ("app.py", "# pyright: basic", "# pyright:"),
        ("app.py", "# pyright: reportGeneralTypeIssues=false", "# pyright:"),
        ("app.py", "# mypy: ignore-errors", "# mypy:"),
        ("app.py", "# mypy: disable-error-code=misc", "# mypy:"),
        ("app.py", "# mypy: allow-untyped-defs", "# mypy:"),
        ("app.py", "# mypy: no-strict-optional", "# mypy:"),
        ("app.py", "# mypy: disallow-untyped-defs=False", "# mypy:"),
        ("app.js", "/* eslint no-console: off */", "eslint rule off"),
        ("app.js", '/* eslint quotes: ["off"], semi: 2 */', "eslint rule off"),
        ("app.js", "/* eslint semi: 2, no-alert: 0 */", "eslint rule off"),
        ("main.go", "//lint:ignore SA1019 reason", "//lint:ignore"),
        ("main.go", "x := y //nolint:errcheck", "//nolint"),
        ("main.go", "x := y // nolint", "//nolint"),
        ("app.cpp", "int x; // NOLINTNEXTLINE", "NOLINT"),
        ("lib.rs", "#[allow(dead_code)]", "#[allow("),
        ("lib.rs", "#![allow(clippy::all)]", "#[allow("),
        ("lib.rs", "#[expect(dead_code)]", "#[allow("),
        ("lib.rs", "#[cfg_attr(test, allow(dead_code))]", "#[cfg_attr(..., allow("),
        ("App.java", '@SuppressWarnings("unchecked")', "@SuppressWarnings"),
        ("App.java", '@SuppressLint("NewApi")', "@SuppressLint"),
        ("App.kt", '@Suppress("UNUSED")', "@Suppress("),
        ("App.cs", '[SuppressMessage("x", "y")]', "[SuppressMessage("),
        ("App.cs", "#pragma warning disable CS0168", "#pragma warning disable"),
        ("app.c", "#pragma GCC diagnostic ignored", "#pragma diagnostic ignored"),
        ("App.java", "int x; // NOSONAR", "NOSONAR"),
        ("app.rb", "# rubocop:disable Metrics/AbcSize", "rubocop:disable"),
        ("App.swift", "// swiftlint:disable force_cast", "swiftlint:disable"),
        ("app.php", "/** @phpstan-ignore-next-line */", "@phpstan-ignore"),
        ("app.php", "/** @psalm-suppress MixedAssignment */", "@psalm-suppress"),
        ("app.py", "if x:  " + NO_COVER, "pragma: no cover"),
        ("app.py", "if x:  " + NO_BRANCH, "pragma: no branch"),
        ("app.py", "if x:  " + NO_COVER_UPPER, "pragma: no cover"),
        ("app.py", "if x:  " + NO_BRANCH_MIXED, "pragma: no branch"),
        ("App.java", "@Generated", "@Generated"),
        ("App.kt", '@javax.annotation.processing.Generated("x")', "@Generated"),
        ("App.cs", "[ExcludeFromCodeCoverage]", "ExcludeFromCodeCoverage"),
        ("app.js", "/* istanbul ignore next */", "istanbul ignore"),
        ("app.js", "/* c8 ignore next */", "c8 ignore"),
        ("app.js", "/* v8 ignore start */", "v8 ignore"),
        ("app.py", "def f():  # lizard forgives", "lizard forgive"),
        ("app.dart", "// coverage:ignore-line", "coverage:ignore"),
        ("app.c", "int x; // LCOV_EXCL_LINE", "LCOV_EXCL"),
        ("test_app.py", "@pytest.mark.skip(reason='later')", "pytest.mark.skip"),
        ("test_app.py", "@pytest.mark.skipif(True, reason='x')", "pytest.mark.skip"),
        ("test_app.py", "@pytest.mark.xfail", "pytest.mark.xfail"),
        (
            "test_app.py",
            "pytestmark = pytest.mark.skip(reason='x')",
            "pytest.mark.skip",
        ),
        ("test_app.py", "pytestmark = pytest.mark.xfail", "pytest.mark.xfail"),
        ("test_app.py", "@(pytest.mark.skip)", "pytest.mark.skip"),
        ("test_app.py", "@mark.xfail", "pytest.mark.skip"),
        ("test_app.py", "    pytest.skip('later')", "pytest.skip("),
        ("test_app.py", "np = pytest.importorskip('numpy')", "pytest.skip("),
        ("test_app.py", "@unittest.skip('later')", "@unittest.skip"),
        ("test_app.py", "@unittest.skipIf(True, 'x')", "@unittest.skip"),
        ("test_app.py", "@skipUnless(False, 'x')", "@unittest.skip"),
        ("test_app.py", "@ut.skip('later')", "@unittest.skip"),
        ("test_app.py", "    @(unittest.skipIf)(True, 'x')", "@unittest.skip"),
        ("test_app.py", "        raise unittest.SkipTest('later')", "SkipTest"),
        ("test_app.py", "@unittest.expectedFailure", "@unittest.expectedFailure"),
        ("test_app.py", "        self.skipTest('later')", "self.skipTest("),
        ("app.test.js", "it.skip('works', () => {})", ".skip("),
        ("app.test.js", "describe.skip('suite', () => {})", ".skip("),
        ("app.test.js", "test.skip.each([1])('x', () => {})", ".skip("),
        ("app.test.js", "it.todo('later')", ".skip("),
        ("app.test.js", "test.todo('later')", ".skip("),
        ("app.test.ts", "test.concurrent.skip('x', () => {})", ".skip("),
        ("app.test.ts", "it.fails('x', () => {})", ".skip("),
        ("app.test.ts", "test.failing('x', () => {})", ".skip("),
        ("app.test.ts", "it.skipIf(isWindows)('x', () => {})", ".skip("),
        ("app.test.ts", "test.concurrent.only('x', () => {})", ".only("),
        ("app.test.js", "    this.skip();", "this.skip("),
        ("app.test.js", "it.only('works', () => {})", ".only("),
        ("app.test.js", "describe.only('suite', () => {})", ".only("),
        ("app.test.js", "xit('works', () => {})", "xit("),
        ("app.test.js", "xdescribe('suite', () => {})", "xit("),
        ("app.test.js", "xtest(`works`, () => {})", "xit("),
        ("app_spec.rb", "  xit 'works' do", "xit("),
        ("spec/app_spec.rb", "  xit do", "xit("),
        ("spec/app_spec.rb", "  fit 'works' do", "fit("),
        ("app.test.js", "fit('works', () => {})", "fit("),
        ("app.test.js", "fdescribe('suite', () => {})", "fit("),
        ("app_test.go", '\tt.Skip("later")', "t.Skip("),
        ("app_test.go", "\tt.SkipNow()", "t.Skip("),
        ("app_test.go", '\tt.Skipf("later %d", 1)', "t.Skip("),
        ("lib.rs", "#[ignore]", "#[ignore]"),
        ("lib.rs", '#[ignore = "slow"]', "#[ignore]"),
        ("AppTest.java", "@Ignore", "@Ignore"),
        ("AppTest.java", "@Disabled", "@Disabled"),
        ("AppTest.java", "@DisabledOnOs(OS.WINDOWS)", "@Disabled"),
        ("AppTests.cs", '[Ignore("later")]', "[Ignore]"),
        ("AppTests.cs", '[Fact(Skip = "later")]', "[Fact(Skip ="),
        ("AppTests.cs", '[Theory(DisplayName = "x", Skip = "later")]', "[Fact(Skip ="),
        ("app_spec.rb", "    skip 'later'", "skip/pending"),
        ("app_spec.rb", "    pending('later')", "skip/pending"),
        ("test/app_test.rb", "    skip if windows?", "skip/pending"),
        ("test/app_test.rb", "    skip", "skip/pending"),
        ("AppTests.swift", 'throw XCTSkip("later")', "XCTSkip"),
    )

    CLEAN = (
        ("app.py", "x = 1  # type annotations are fine"),
        ("app.py", "model.fit(x, y)"),
        ("app.py", "fit(x, y)"),
        ("app.py", "unittest.main()"),
        ("lib.rs", "let rest = items.iter().skip(2);"),
        ("App.cs", "var page = rows.Skip(10).Take(5);"),
        ("app.js", "const it = iterator(); it.next();"),
        ("app.js", "exit('done')"),
        ("lib.rs", "#[derive(Debug)]"),
        ("app.py", "def skip(self):"),
        ("app.py", "    skip('not a ruby file')"),
        ("app.rb", "  skip_this = true"),
        ("app.rb", "  obj.skip('x')"),
        ("App.java", "@Override"),
        ("app.py", "ignore = True  # nothing to see"),
        ("tests/test_app.py", "import app  # noqa: E402"),
        ("tests/test_app.py", "import app  # noqa:E402\r"),
        ("scripts/run.py", "import sibling  # NOQA: E402"),
        ("app.py", "# pyright: strict"),
        ("app.py", "# mypy: disallow-untyped-defs, warn-unreachable"),
        ("app.py", "# mypy: no-implicit-reexport"),
        ("app.js", "/* eslint-env node */"),
        ("app.js", '/* eslint no-console: "error" */'),
        ("app.py", '@app.route("/skip")'),
        ("Entity.java", "@GeneratedValue(strategy = GenerationType.IDENTITY)"),
        ("app.py", "@Generated"),
        ("order.rb", 'Order.where(status: "pending")'),
        ("order.rb", "return if state == 'skip'"),
        ("pager.rb", "  def skip(count)"),
        ("pager.rb", "    @skip = options.fetch(:skip, 0)"),
        ("lib/pager.rb", "    skip 'not a test file'"),
        ("spec/pager_spec.rb", "    skip = 2"),
        ("spec/pager_spec.rb", '    expect(order.status).to eq("pending")'),
        ("lex.rs", "let rest: String = it.skip(1).collect();"),
        ("Q.cs", "var rows = table.Select(t => t.Skip(1));"),
        ("README.md", 'This library is a good fit "out of the box".'),
        ("app.py", "@cache.skip_when_empty"),
    )

    def test_each_marker_is_recognized(self):
        for relative, line, label in self.FLAGGED:
            with self.subTest(line=line):
                hit = gate_shortcuts.marker_in_line(line, relative)
                self.assertIsNotNone(hit)
                assert hit is not None
                self.assertEqual(hit.marker.label, label)

    def test_ordinary_code_is_not_a_marker(self):
        for relative, line in self.CLEAN:
            with self.subTest(line=line):
                self.assertIsNone(gate_shortcuts.marker_in_line(line, relative))

    def test_the_matched_text_is_reported_not_the_table_label(self):
        cases = (
            (
                "app.py",
                "x = 1  # mypy: disable-error-code=misc",
                "# mypy: disable-error-code=misc",
            ),
            ("app.test.js", "xdescribe('suite', () => {})", "xdescribe"),
            ("lib.rs", "#![allow(clippy::all)]", "#![allow("),
            ("test_app.py", "@skipUnless(False, 'x')", "@skipUnless"),
            ("app.py", "# ruff: noqa", "# ruff: noqa"),
            ("app.py", "x = 1  " + NO_COVER_UPPER, NO_COVER_UPPER),
        )
        for relative, line, text in cases:
            with self.subTest(line=line):
                hit = gate_shortcuts.marker_in_line(line, relative)
                assert hit is not None
                self.assertEqual(hit.text, text)

    def test_only_markers_tools_read_in_any_case_ignore_case(self):
        self.assertIsNone(gate_shortcuts.marker_in_line("// NOQA", "a.py"))
        self.assertIsNone(gate_shortcuts.marker_in_line("# Type: Ignore", "a.py"))
        self.assertIsNone(gate_shortcuts.marker_in_line("// TS-IGNORE", "a.ts"))
        self.assertIsNotNone(gate_shortcuts.marker_in_line("x  # NOQA", "a.py"))

    def test_this_gate_and_its_test_are_exempt(self):
        self.assertTrue(
            gate_shortcuts.is_exempt_from_markers(
                "skills/code-discipline/scripts/gate_shortcuts.py"
            )
        )
        self.assertTrue(
            gate_shortcuts.is_exempt_from_markers("tests/test_gate_shortcuts.py")
        )
        self.assertFalse(gate_shortcuts.is_exempt_from_markers("src/shortcuts.py"))
        self.assertFalse(
            gate_shortcuts.is_exempt_from_markers("src/billing_gate_shortcuts.py")
        )


class MarkerScanTests(unittest.TestCase):
    def test_only_added_lines_of_changed_files_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/app.py", "import os  # noqa\nx = 1  # noqa\n")
            write(root, "tests/test_app.py", "@unittest.skip('x')\n")
            write(root, "README.md", "Use eslint-disable sparingly.\n")
            changes = local(
                {
                    "src/app.py": diff_scope.FileChange("src/app.py", frozenset({2})),
                    "tests/test_app.py": diff_scope.FileChange(
                        "tests/test_app.py", frozenset({1})
                    ),
                }
            )
            items = gate_shortcuts.marker_items(root, changes)
        self.assertEqual(
            [(item.path, item.line, item.value) for item in items],
            [
                ("src/app.py", 2, "'# noqa'"),
                ("tests/test_app.py", 1, "'@unittest.skip'"),
            ],
        )
        self.assertEqual(items[0].metric, "suppression marker")
        self.assertEqual(items[1].metric, "test skip marker")

    def test_repository_scope_scans_every_line_of_every_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/app.py", "x = 1\ny = 2  # noqa\n")
            write(root, "docs/notes.md", "Never use @ts-ignore.\n")
            write(root, "tools/gate_shortcuts.py", "MARKER = '# noqa'\n")
            write(root, ".quality/state.json", '{"note": "# noqa"}\n')
            (root / "image.bin").write_bytes(b"\0# noqa")
            items = gate_shortcuts.marker_items(root, diff_scope.repository_changes())
        self.assertEqual(
            sorted((item.path, item.line) for item in items),
            [("src/app.py", 2)],
        )

    def test_documentation_and_string_literals_do_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "README.md", "Avoid `# type: ignore` and it.skip().\n")
            write(
                root,
                "tests/test_parser.py",
                'SAMPLE = """\n'
                "class T(unittest.TestCase):\n"
                "    @unittest.expectedFailure\n"
                "    def test_x(self):\n"
                "        raise unittest.SkipTest('later')\n"
                '"""\n'
                "LABEL = '# noqa'\n"
                "value = run()  # type: ignore\n",
            )
            write(root, "web/app.ts", "const hint = '@ts-ignore';\n// @ts-ignore\n")
            items = gate_shortcuts.marker_items(root, diff_scope.repository_changes())
        self.assertEqual(
            sorted((item.path, item.line) for item in items),
            [("tests/test_parser.py", 8), ("web/app.ts", 2)],
        )

    def test_quote_tracking_handles_escapes_and_closed_strings(self):
        self.assertTrue(gate_shortcuts_text.inside_quotes('x = "a # noqa', 6))
        self.assertFalse(gate_shortcuts_text.inside_quotes('x = "a" # noqa', 8))
        self.assertTrue(gate_shortcuts_text.inside_quotes('x = "a\\" # noqa', 9))
        self.assertTrue(gate_shortcuts_text.inside_quotes("x = `a ${b} # noqa", 12))

    def test_unfinished_python_tokens_keep_the_spans_found_so_far(self):
        spans = gate_shortcuts_text.python_string_spans('a = "x"\nb = """open')
        self.assertEqual(spans, [((1, 4), (1, 7))])

    def test_a_nul_byte_does_not_hide_a_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "b.ts").write_bytes(b"// \0\n// @ts-ignore\nrun();\n")
            (root / "data.bin").write_bytes(b"\0 @ts-ignore\n")
            items = gate_shortcuts.marker_items(root, diff_scope.repository_changes())
        self.assertEqual(
            [(item.path, item.line, item.value) for item in items],
            [("src/b.ts", 2, "'@ts-ignore'")],
        )

    def test_added_line_numbers_past_the_end_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "app.py", "x = 1  # noqa")
            changes = local(
                {"app.py": diff_scope.FileChange("app.py", frozenset({1, 7}))}
            )
            items = gate_shortcuts.marker_items(root, changes)
        self.assertEqual([item.line for item in items], [1])


class PythonHandlerTests(unittest.TestCase):
    def kinds(self, source: str) -> list[tuple[str, int, int]]:
        return [
            (handler.kind, handler.start_line, handler.end_line)
            for handler in handlers.silent_handlers(textwrap.dedent(source), ".py")
        ]

    def test_bodies_that_do_nothing_are_silent(self):
        source = """\
            try:
                run()
            except ValueError:
                pass
            try:
                run()
            except (KeyError, OSError) as error:
                ...
            for item in items:
                try:
                    run(item)
                except Exception:
                    continue
            def lookup():
                try:
                    return table[key]
                except KeyError:
                    return
            def lookup_none():
                try:
                    return table[key]
                except:
                    return None
            """
        self.assertEqual(
            self.kinds(source),
            [
                ("except ValueError", 3, 4),
                ("except (KeyError, OSError)", 7, 8),
                ("except Exception", 12, 13),
                ("except KeyError", 17, 18),
                ("except", 22, 23),
            ],
        )

    def test_handlers_that_act_are_not_silent(self):
        source = """\
            try:
                run()
            except ValueError as error:
                log(error)
            try:
                run()
            except KeyError:
                return default
            try:
                run()
            except OSError:
                raise
            with open(path) as handle:
                handle.read()
            with suppress_output():
                run()
            with errors.suppress(OSError):
                run()
            with lock:
                run()
            """
        self.assertEqual(self.kinds(source), [])

    def test_trivial_variants_of_doing_nothing_are_silent(self):
        source = """\
            for item in items:
                try:
                    run(item)
                except Exception:
                    break
            try:
                run()
            except Exception:
                None
            try:
                run()
            except TypeError:
                "ignored on purpose"
            """
        self.assertEqual(
            self.kinds(source),
            [
                ("except Exception", 4, 5),
                ("except Exception", 8, 9),
                ("except TypeError", 12, 13),
            ],
        )

    def test_suppress_blocks_are_silent_at_their_header(self):
        source = """\
            import contextlib
            from contextlib import suppress
            with contextlib.suppress(Exception):
                run()
                more()
            with open(path) as handle, suppress(
                KeyError,
            ):
                run()
            async def main():
                async with suppress(OSError): await run()
            """
        self.assertEqual(
            self.kinds(source),
            [("with contextlib.suppress(Exception)", 3, 3)],
        )

    def test_broad_suppress_forms_are_silent(self):
        source = """\
            from contextlib import suppress
            import builtins
            with suppress():
                run()
            with suppress(builtins.BaseException):
                run()
            with suppress(KeyError, Exception):
                run()
            with factory():
                run()
            """
        self.assertEqual(
            [kind for kind, _, _ in self.kinds(source)],
            [
                "with suppress()",
                "with suppress(builtins.BaseException)",
                "with suppress(KeyError, Exception)",
            ],
        )

    def test_a_handler_with_a_reason_comment_is_documented(self):
        source = """\
            try:
                import yaml
            except ImportError:
                # YAML support is optional for plain JSON projects.
                yaml = None
                pass
            try:
                run()
            except ValueError:
                pass  # todo
            """
        self.assertEqual(self.kinds(source), [("except ValueError", 9, 10)])
        brace = "try { a(); } catch (e) { /* the file may not exist yet */ }\n"
        self.assertEqual(handlers.silent_handlers(brace, ".ts"), [])

    def test_source_too_deep_for_the_parser_yields_nothing(self):
        long_sum = "x = " + "1 + " * 20000 + "1\n"
        long_chain = "if a:\n    pass\n" + "elif a:\n    pass\n" * 20000
        self.assertEqual(handlers.silent_handlers(long_sum, ".py"), [])
        self.assertEqual(handlers.silent_handlers(long_chain, ".py"), [])

    def test_unparsable_python_yields_nothing(self):
        self.assertEqual(self.kinds("def broken(:\n"), [])
        self.assertEqual(handlers.silent_handlers("x = 1\0", ".py"), [])

    def test_invalid_escape_warnings_are_not_printed(self):
        source = 'pattern = "\\d"\ntry:\n    run()\nexcept E:\n    pass\n'
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("error")
            found = self.kinds(source)
        self.assertEqual(found, [("except E", 4, 5)])
        self.assertEqual(caught, [])


class BraceHandlerTests(unittest.TestCase):
    def kinds(self, source: str, extension: str) -> list[tuple[str, int, int]]:
        return [
            (handler.kind, handler.start_line, handler.end_line)
            for handler in handlers.silent_handlers(textwrap.dedent(source), extension)
        ]

    def test_empty_catch_blocks_in_each_language(self):
        cases = {
            ".java": "try { run(); } catch (IOException e) { }\n",
            ".kt": "try { run() } catch (e: Exception) {}\n",
            ".cs": "try { Run(); } catch { }\n",
            ".php": "try { run(); } catch (\\Exception $e) {}\n",
            ".swift": "do { try run() } catch let error as AppError { }\n",
            ".ts": "try { run(); } catch { }\n",
            ".dart": "try { run(); } catch (e) { }\n",
            ".scala": "try { run() } catch { }\n",
        }
        for extension, source in cases.items():
            with self.subTest(extension=extension):
                self.assertEqual(self.kinds(source, extension), [("catch", 1, 1)])

    def test_catch_that_only_returns_nothing_is_silent(self):
        source = """\
            try {
              run();
            } catch (e) {
              // ignore it
              return null;
            }
            try { run(); } catch (e) { return undefined; }
            try { run(); } catch (e) { return; }
            try { run(); } catch (e) { ; }
            try { run(); } catch (e) { void e; }
            """
        self.assertEqual(
            self.kinds(source, ".js"),
            [
                ("catch", 3, 6),
                ("catch", 7, 7),
                ("catch", 8, 8),
                ("catch", 9, 9),
                ("catch", 10, 10),
            ],
        )

    def test_catch_that_handles_the_error_is_not_silent(self):
        source = """\
            try { run(); } catch (e) { log(e); }
            try { run(); } catch (e) { return fallback; }
            try { run(); } catch (e) { throw new Wrapped(e); }
            // try { run(); } catch (e) { }
            const text = "catch (e) { }";
            /* catch { } */
            try { run(); } catch (e) { returnValue = 1; }
            """
        self.assertEqual(self.kinds(source, ".js"), [])

    def test_unbalanced_catch_is_ignored(self):
        self.assertEqual(self.kinds("try { run(); } catch (e) {\n", ".js"), [])

    def test_promise_catch_callbacks_that_drop_the_error(self):
        source = """\
            load().catch(() => {});
            load().catch(() => null);
            load().catch(err => undefined);
            load().catch(function () {});
            load().catch(function onError(error) { return; });
            load()
              .catch(() => {
              });
            load().catch((e) => { return null; });
            load().catch((error: unknown) => {});
            load().catch(async () => {});
            load().catch(async (error: Error) => { void error; });
            load().catch(async function (error: unknown) { return null; });
            """
        self.assertEqual(
            self.kinds(source, ".ts"),
            [
                (".catch()", 1, 1),
                (".catch()", 2, 2),
                (".catch()", 3, 3),
                (".catch()", 4, 4),
                (".catch()", 5, 5),
                (".catch()", 7, 8),
                (".catch()", 9, 9),
                (".catch()", 10, 10),
                (".catch()", 11, 11),
                (".catch()", 12, 12),
                (".catch()", 13, 13),
            ],
        )

    def test_promise_catch_that_reports_is_not_silent(self):
        source = """\
            load().catch((error) => report(error));
            load().catch(function (error) { console.error(error); });
            load().catch(handleError);
            """
        self.assertEqual(self.kinds(source, ".js"), [])

    def test_go_error_checks_and_discarded_errors(self):
        source = """\
            func run() error {
            \tif err != nil {
            \t}
            \tif err := save(); err != nil {}
            \tif err != nil { return nil }
            \tif err != nil { return err }
            \t_ = err
            \t// _ = err
            \tx := "_ = err"
            \tif value != nil {}
            \tif e != nil {}
            \tif nil != err {}
            \tif nil != parseError {}
            \tif node != nil {}
            }
            """
        self.assertEqual(
            self.kinds(source, ".go"),
            [
                ("error check", 2, 3),
                ("error check", 4, 4),
                ("error check", 5, 5),
                ("_ = err", 7, 7),
                ("error check", 11, 11),
                ("error check", 12, 12),
                ("error check", 13, 13),
            ],
        )

    def test_discarded_error_only_counts_in_go(self):
        self.assertEqual(self.kinds("_ = err\n", ".js"), [])


class RubyHandlerTests(unittest.TestCase):
    def kinds(self, source: str) -> list[tuple[str, int, int]]:
        return [
            (handler.kind, handler.start_line, handler.end_line)
            for handler in handlers.silent_handlers(textwrap.dedent(source), ".rb")
        ]

    def test_rescues_followed_by_end_or_nil_are_silent(self):
        source = """\
            begin
              run
            rescue StandardError => e
            end
            begin
              run
            rescue

              nil
            end
            begin; run; rescue => e; end
            value = parse(text) rescue nil
            def call
              run
            rescue IOError
              # just ignore
            end
            """
        self.assertEqual(
            self.kinds(source),
            [
                ("rescue", 3, 4),
                ("rescue", 7, 9),
                ("rescue", 11, 11),
                ("rescue nil", 12, 12),
                ("rescue", 15, 17),
            ],
        )

    def test_rescues_that_act_are_not_silent(self):
        source = """\
            begin
              run
            rescue IOError => e
              logger.warn(e)
            end
            begin; run; rescue => e; log(e); end
            value = parse(text) rescue default
            message = "rescue nil"
            begin
              run
            rescue IOError
            """
        self.assertEqual(self.kinds(source), [])


class HelperTests(unittest.TestCase):
    def test_mask_keeps_offsets_and_newlines(self):
        text = 'a = "x // y"; // note\n/* one\ntwo */ b'
        masked = handlers.mask(text, handlers._BRACE_NOISE)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count("\n"), text.count("\n"))
        self.assertNotIn("note", masked)
        self.assertNotIn("two", masked)
        self.assertTrue(masked.endswith(" b"))

    def test_matching_brace(self):
        self.assertEqual(handlers.matching_brace("{a{b}c}", 0), 6)
        self.assertEqual(handlers.matching_brace("{a{b}c", 0), -1)


class SilentHandlerScanTests(unittest.TestCase):
    def test_only_production_handlers_touching_added_lines_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root,
                "src/app.py",
                """\
                def old():
                    try:
                        run()
                    except ValueError:
                        pass

                def new():
                    try:
                        run()
                    except KeyError:
                        pass
                """,
            )
            write(root, "tests/test_app.py", "try:\n    run()\nexcept E:\n    pass\n")
            write(root, "README.md", "except: pass\n")
            changes = local(
                {
                    "src/app.py": diff_scope.FileChange("src/app.py", frozenset({11})),
                    "tests/test_app.py": diff_scope.FileChange(
                        "tests/test_app.py", frozenset({4}), is_new=True, all_lines=True
                    ),
                }
            )
            items = gate_shortcuts.silent_handler_items(root, changes, {})
        self.assertEqual(
            [(item.path, item.line, item.name) for item in items],
            [("src/app.py", 10, "except KeyError")],
        )

    def test_repository_scope_reports_every_silent_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "web/app.js", "try { run(); } catch (e) {}\n")
            write(root, "cmd/main.go", "func f() {\n\t_ = err\n}\n")
            items = gate_shortcuts.silent_handler_items(
                root, diff_scope.repository_changes(), {}
            )
        self.assertEqual(
            sorted((item.path, item.line, item.name) for item in items),
            [("cmd/main.go", 2, "_ = err"), ("web/app.js", 1, "catch")],
        )


class PolicyAndConfigTests(unittest.TestCase):
    def test_policy_edits_and_deletions_are_found_in_incremental_scope(self):
        changes = local(
            {
                ".quality/quality-gate.json": diff_scope.FileChange(
                    ".quality/quality-gate.json", frozenset({3})
                ),
                ".quality/state.json": diff_scope.FileChange(
                    ".quality/state.json", frozenset({1})
                ),
            },
            deleted=frozenset({".quality/quality-thresholds.json"}),
        )
        self.assertEqual(
            gate_shortcuts.changed_policy_files(changes),
            [".quality/quality-gate.json", ".quality/quality-thresholds.json"],
        )

    def test_renaming_a_policy_or_config_file_away_counts_as_changing_it(self):
        changes = local(
            {
                ".quality/old-thresholds.json": diff_scope.FileChange(
                    ".quality/old-thresholds.json",
                    old_path=".quality/quality-thresholds.json",
                ),
                "old-mypy.ini": diff_scope.FileChange(
                    "old-mypy.ini", old_path="mypy.ini"
                ),
            },
            deleted=frozenset({"web/tsconfig.json"}),
        )
        self.assertEqual(
            gate_shortcuts.changed_policy_files(changes),
            [".quality/quality-thresholds.json"],
        )
        self.assertEqual(
            gate_shortcuts.changed_check_configs(changes),
            ["mypy.ini", "web/tsconfig.json"],
        )

    def test_repository_scope_has_no_policy_edits_or_config_changes(self):
        changes = diff_scope.repository_changes()
        self.assertEqual(gate_shortcuts.changed_policy_files(changes), [])
        self.assertEqual(gate_shortcuts.changed_check_configs(changes), [])

    def test_check_configuration_files_are_recognized(self):
        names = [
            "tsconfig.json",
            "packages/web/tsconfig.build.json",
            ".eslintrc.cjs",
            "eslint.config.mjs",
            ".prettierrc",
            "prettier.config.js",
            "ruff.toml",
            ".ruff.toml",
            "mypy.ini",
            "setup.cfg",
            "pyproject.toml",
            "tox.ini",
            "pytest.ini",
            "jest.config.ts",
            "vitest.config.ts",
            ".golangci.yml",
            "clippy.toml",
            "rustfmt.toml",
            "phpunit.xml.dist",
            ".rubocop.yml",
            ".swiftlint.yml",
            "detekt.yml",
            "Directory.Build.props",
            ".editorconfig",
            "web/package.json",
        ]
        files = {name: diff_scope.FileChange(name) for name in names}
        files["src/app.py"] = diff_scope.FileChange("src/app.py")
        files["package-lock.json"] = diff_scope.FileChange("package-lock.json")
        self.assertEqual(
            gate_shortcuts.changed_check_configs(local(files)), sorted(names)
        )


class EvaluateShortcutsTests(unittest.TestCase):
    def test_clean_change_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/app.py", "def run():\n    return 1\n")
            changes = local(
                {"src/app.py": diff_scope.FileChange("src/app.py", frozenset({2}))}
            )
            outcome = gate_shortcuts.evaluate_shortcuts(root, changes, {})
        self.assertEqual(outcome.key, "shortcuts")
        self.assertEqual(outcome.title, "No shortcuts")
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.items, [])
        self.assertIn("No hidden production files", outcome.summary)
        self.assertEqual(outcome.measurements["markers"], 0)

    def test_every_kind_of_shortcut_fails_with_a_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root,
                "src/app.py",
                "x = 1  # noqa\ntry:\n    run()\nexcept E:\n    pass\n",
            )
            write(root, "legacy/old.py", "x = 1\n")
            write(root, ".quality/quality-gate.json", "{}\n")
            write(root, "pyproject.toml", "[tool.ruff]\n")
            config = {"exclude": [*source_files.DEFAULT_TEST_PATTERNS, "legacy/**"]}
            changes = local(
                {
                    "src/app.py": diff_scope.FileChange(
                        "src/app.py", is_new=True, all_lines=True
                    ),
                    ".quality/quality-gate.json": diff_scope.FileChange(
                        ".quality/quality-gate.json", frozenset({1})
                    ),
                    "pyproject.toml": diff_scope.FileChange(
                        "pyproject.toml", frozenset({1})
                    ),
                }
            )
            outcome = gate_shortcuts.evaluate_shortcuts(root, changes, config)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            outcome.measurements,
            {
                "hidden_files": 1,
                "markers": 1,
                "silent_handlers": 1,
                "policy_changes": 1,
                "check_config_changes": 1,
            },
        )
        self.assertEqual(
            outcome.summary,
            "Shortcuts found: 1 hidden production file, 1 suppression or "
            "skip marker, 1 silent error handler, 1 unapproved policy edit.",
        )
        self.assertIn(
            "legacy/old.py: hidden from the gates by source.exclude pattern "
            "'legacy/**'",
            outcome.details[0],
        )
        self.assertTrue(
            any(item.value == "changed without user approval" for item in outcome.items)
        )
        self.assertIn(
            "Check configuration changed: pyproject.toml — confirm the change "
            "does not weaken checks.",
            outcome.details,
        )

    def test_approved_policy_change_is_informational(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, ".quality/quality-thresholds.json", "{}\n")
            changes = local(
                {
                    ".quality/quality-thresholds.json": diff_scope.FileChange(
                        ".quality/quality-thresholds.json", frozenset({1})
                    )
                }
            )
            blocked = gate_shortcuts.evaluate_shortcuts(root, changes, {})
            approved = gate_shortcuts.evaluate_shortcuts(
                root, changes, {}, allow_policy_change=True
            )
        self.assertEqual(blocked.status, "fail")
        self.assertIn("changed without user approval", blocked.details[0])
        self.assertEqual(approved.status, "pass")
        self.assertIn(
            "Project policy changed with user approval: "
            ".quality/quality-thresholds.json",
            approved.details,
        )

    def test_local_changes_from_git_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            write(root, "app.js", "export function run() {\n  return 1;\n}\n")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "base")
            write(
                root,
                "app.js",
                "export function run() {\n"
                "  // eslint-disable-next-line\n"
                "  try { go(); } catch (e) {}\n"
                "  return 1;\n"
                "}\n",
            )
            write(root, "app.test.js", "it.only('runs', () => {});\n")
            outcome = gate_shortcuts.evaluate_shortcuts(
                root, diff_scope.local_changes(root), {}
            )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            sorted((item.path, item.line, item.metric) for item in outcome.items),
            [
                ("app.js", 2, "suppression marker"),
                ("app.js", 3, "silent error handler"),
                ("app.test.js", 1, "test focus marker"),
            ],
        )


class GitChangeTests(unittest.TestCase):
    """End-to-end checks against real Git diffs, where renames, deletions,
    and pure line removals take shapes a hand-built ChangeSet can miss."""

    def commit_base(self, root: Path, files: dict[str, str]) -> None:
        make_repository(root)
        for relative, text in files.items():
            write(root, relative, text)
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "base")

    def evaluate(self, root: Path) -> gate_shortcuts.GateOutcome:
        return gate_shortcuts.evaluate_shortcuts(
            root, diff_scope.local_changes(root), {}
        )

    def test_staged_rename_of_a_policy_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.commit_base(
                root,
                {
                    ".quality/quality-thresholds.json": '{"coverage": 90}\n',
                    "mypy.ini": "[mypy]\nstrict = True\n",
                },
            )
            git(
                root,
                "mv",
                ".quality/quality-thresholds.json",
                ".quality/old-thresholds.json",
            )
            git(root, "rm", "-q", "mypy.ini")
            outcome = self.evaluate(root)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            [item.path for item in outcome.items], [".quality/quality-thresholds.json"]
        )
        self.assertEqual(outcome.measurements["check_config_changes"], 1)
        self.assertIn(
            "Check configuration changed: mypy.ini — confirm the change does not "
            "weaken checks.",
            outcome.details,
        )

    def test_handler_made_silent_by_deleting_lines_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.commit_base(
                root,
                {
                    "src/app.py": """\
                        import logging

                        def quiet():
                            try:
                                return run()
                            except KeyError:
                                return None

                        def load():
                            try:
                                return run()
                            except OSError:
                                logging.exception("load failed")
                                return None
                        """
                },
            )
            path = root / "src" / "app.py"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace('        logging.exception("load failed")\n', ""),
                encoding="utf-8",
            )
            outcome = self.evaluate(root)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(
            [(item.path, item.line, item.name) for item in outcome.items],
            [("src/app.py", 12, "except OSError")],
        )

    def test_unchanged_silent_handler_in_an_edited_file_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.commit_base(
                root,
                {
                    "src/app.py": """\
                        LIMIT = 1
                        EXTRA = 2

                        def quiet():
                            try:
                                return run()
                            except KeyError:
                                return None
                        """
                },
            )
            path = root / "src" / "app.py"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("EXTRA = 2\n", ""), encoding="utf-8")
            outcome = self.evaluate(root)
        self.assertEqual(outcome.status, "pass")

    def test_deleted_lines_without_a_readable_base_only_count_added_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/app.py", "try:\n    run()\nexcept E:\n    pass\n")
            changes = local(
                {
                    "src/app.py": diff_scope.FileChange(
                        "src/app.py", removed=("    log()",)
                    )
                }
            )
            items = gate_shortcuts.silent_handler_items(root, changes, {})
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
