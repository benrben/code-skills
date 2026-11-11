from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import source_files  # noqa: E402


def write(root: Path, relative: str, text: str = "x = 1\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class GlobTests(unittest.TestCase):
    def test_patterns_are_root_anchored_and_star_stays_in_one_folder(self):
        self.assertTrue(source_files.glob_matches("tests/a/b.py", "tests/**"))
        self.assertFalse(source_files.glob_matches("src/tests_x.py", "tests/**"))
        self.assertTrue(source_files.glob_matches("a/b/tests/c.py", "**/tests/**"))
        self.assertTrue(source_files.glob_matches("tests/c.py", "**/tests/**"))
        self.assertTrue(source_files.glob_matches("a_test.go", "**/*_test.go"))
        self.assertTrue(source_files.glob_matches("pkg/a_test.go", "**/*_test.go"))
        self.assertFalse(source_files.glob_matches("src/a.py", "*.py"))
        self.assertTrue(source_files.glob_matches("x.py", "?.py"))
        self.assertFalse(source_files.glob_matches("x/y.py", "x?y.py"))
        self.assertTrue(source_files.glob_matches("Tests/A.swift", "tests/**"))
        self.assertTrue(source_files.glob_matches("a.py", "/a.py"))
        self.assertTrue(source_files.glob_matches("src/x/y.ts", "src/**"))

    def test_default_test_patterns_do_not_hide_production_code(self):
        for production in (
            "src/features/checkout.ts",
            "app/spec/openapi.py",
            "src/services/test.py",
            "bin/cli.js",
            "src/contest_entry.py",
        ):
            self.assertFalse(source_files.is_test_path(production), production)
        for test in (
            "tests/test_app.py",
            "pkg/a_test.go",
            "src/a.test.ts",
            "src/a.spec.ts",
            "spec/models/user_spec.rb",
            "lib/user_spec.rb",
            "src/__tests__/a.js",
            "src/test/java/AppTest.java",
            "Tests/AppTests/AppTests.swift",
        ):
            self.assertTrue(source_files.is_test_path(test), test)

    def test_first_match_and_tooling(self):
        self.assertEqual(
            source_files.first_match("a/b.gen.py", ["*.py", "**/*.gen.py"]),
            "**/*.gen.py",
        )
        self.assertIsNone(source_files.first_match("a.py", ["b.py"]))
        self.assertTrue(source_files.is_tooling_path("vite.config.ts"))
        self.assertFalse(source_files.is_tooling_path("src/app.ts"))


class DiscoveryTests(unittest.TestCase):
    def test_walk_without_git_skips_vendor_generated_and_build_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/a.py")
            write(root, "bin/cli.py")
            write(root, "node_modules/x/index.js")
            write(root, ".quality/state.json", "{}")
            write(root, "dist/bundle.js")
            write(root, ".agents/skills/code-discipline/scripts/gate_types.py")
            write(root, ".claude/skills/code-discipline/scripts/gate_types.py")
            with mock.patch.object(source_files.shutil, "which", return_value=None):
                found = sorted(
                    source_files.normalize_path(path, root)
                    for path in source_files.walk_files(root)
                )
            self.assertEqual(found, ["bin/cli.py", "src/a.py"])

    def test_an_installed_skill_is_not_part_of_the_repository_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            write(root, "app/calc.py")
            write(root, ".agents/skills/code-discipline/scripts/quality_loop.py")
            write(root, ".claude/settings.json", "{}")
            found = sorted(
                source_files.normalize_path(path, root)
                for path in source_files.walk_files(root)
            )
            self.assertEqual(found, ["app/calc.py"])

    def test_walk_with_git_honours_ignore_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            write(root, ".gitignore", "secret.txt\n")
            write(root, "secret.txt")
            write(root, "src/a.py")
            write(root, "vendor/lib.py")
            found = sorted(
                source_files.normalize_path(path, root)
                for path in source_files.walk_files(root)
            )
            self.assertEqual(found, [".gitignore", "src/a.py"])

    def test_walk_falls_back_when_git_listing_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            write(root, "a.py")
            failed = subprocess.CompletedProcess([], 128, b"", b"")
            with mock.patch.object(source_files.subprocess, "run", return_value=failed):
                found = [path.name for path in source_files.walk_files(root)]
            self.assertIn("a.py", found)

    def test_discover_source_files_applies_settings_scope_and_skip_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/a.py")
            write(root, "src/b.ts")
            write(root, "src/readme.md")
            write(root, "tests/test_a.py")
            write(root, "src/gate.py")
            with mock.patch.object(source_files.shutil, "which", return_value=None):
                every = source_files.discover_source_files(
                    root, {}, skip_names=frozenset({"gate.py"})
                )
                self.assertEqual(
                    [source_files.normalize_path(p, root) for p in every],
                    ["src/a.py", "src/b.ts"],
                )
                change = diff_scope.FileChange("src/b.ts", frozenset({1}))
                scope = diff_scope.ChangeSet(
                    diff_scope.LOCAL_CHANGES, None, {"src/b.ts": change}
                )
                scoped = source_files.discover_source_files(root, {}, scope)
                self.assertEqual([p.name for p in scoped], ["b.ts"])
                included = source_files.discover_source_files(
                    root, {"include": ["src/**"], "extensions": [".py"]}
                )
                self.assertEqual([p.name for p in included], ["a.py", "gate.py"])

    def test_text_files_skip_binary_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, ".env", "KEY=value\n")
            write(root, "config/app.yml", "a: 1\n")
            (root / "logo.png").write_bytes(b"\x89PNG\0\0")
            big = root / "big.txt"
            big.write_text("a")
            with mock.patch.object(source_files, "MAX_TEXT_BYTES", 0):
                self.assertFalse(source_files.is_text_file(big))
            self.assertFalse(source_files.is_text_file(root / "missing.txt"))
            with mock.patch.object(source_files.shutil, "which", return_value=None):
                found = [
                    source_files.normalize_path(path, root)
                    for path in source_files.text_files(root)
                ]
                change = diff_scope.FileChange(".env", frozenset({1}))
                scope = diff_scope.ChangeSet(
                    diff_scope.LOCAL_CHANGES, None, {".env": change}
                )
                scoped = source_files.text_files(root, scope)
            self.assertEqual(found, [".env", "big.txt", "config/app.yml"])
            self.assertEqual([path.name for path in scoped], [".env"])

    def test_line_counts_language_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write(root, "a.go", "a\nb\nc\n")
            self.assertEqual(source_files.physical_line_count(path), 3)
            self.assertEqual(source_files.read_text(path), "a\nb\nc\n")
            self.assertEqual(source_files.text_line_count("a\nb"), 2)
            self.assertEqual(source_files.language_for("x/a.go"), "Go")
            self.assertIsNone(source_files.language_for("x/a.unknown"))
            outside = Path(tempfile.gettempdir()).resolve()
            self.assertEqual(
                source_files.normalize_path(outside, root), outside.as_posix()
            )
            self.assertFalse(source_files.is_file_within(outside, root))
            self.assertFalse(source_files.is_file_within(root / "missing", root))


class HiddenProductionFileTests(unittest.TestCase):
    def hidden(self, config):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "src/app.py")
            write(root, "src/billing.py")
            write(root, "web/ui.ts")
            write(root, "tests/test_app.py")
            write(root, "vite.config.ts")
            with mock.patch.object(source_files.shutil, "which", return_value=None):
                return source_files.hidden_production_files(root, config)

    def test_default_settings_hide_nothing(self):
        self.assertEqual(self.hidden({}), [])

    def test_extra_exclude_include_and_extensions_are_reported(self):
        self.assertEqual(
            self.hidden({"exclude": ["tests/**", "src/billing.py", "*.config.*"]}),
            [("src/billing.py", "source.exclude pattern 'src/billing.py'")],
        )
        self.assertEqual(
            self.hidden({"include": ["src/**"]}),
            [("web/ui.ts", "not matched by source.include")],
        )
        self.assertEqual(
            self.hidden({"extensions": [".py"]}),
            [("web/ui.ts", "extension missing from source.extensions")],
        )


if __name__ == "__main__":
    unittest.main()
