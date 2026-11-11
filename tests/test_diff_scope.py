import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402


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


class ParseUnifiedDiffTests(unittest.TestCase):
    def test_added_lines_use_new_file_numbers_and_removed_text_is_kept(self):
        diff = "\n".join(
            [
                "diff --git a/app.py b/app.py",
                "index 1..2 100644",
                "--- a/app.py",
                "+++ b/app.py",
                "@@ -2 +2,2 @@ def run():",
                "-    return old()",
                "+    value = new()",
                "+    return value",
                "@@ -9,0 +11 @@",
                "+extra = 1",
            ]
        )
        changes = dict(diff_scope.parse_unified_diff(diff))
        change = changes["app.py"]
        self.assertEqual(change.added, frozenset({2, 3, 11}))
        self.assertEqual(change.removed, ("    return old()",))
        self.assertFalse(change.is_new)
        self.assertIsNone(change.old_path)

    def test_removed_line_that_looks_like_a_header_stays_removed_text(self):
        diff = "\n".join(
            [
                "diff --git a/notes.md b/notes.md",
                "--- a/notes.md",
                "+++ b/notes.md",
                "@@ -1 +1 @@",
                "--- old heading",
                "+++ new heading",
            ]
        )
        change = dict(diff_scope.parse_unified_diff(diff))["notes.md"]
        self.assertEqual(change.removed, ("-- old heading",))
        self.assertEqual(change.added, frozenset({1}))

    def test_new_deleted_renamed_and_binary_files(self):
        diff = "\n".join(
            [
                "diff --git a/new.py b/new.py",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.py",
                "@@ -0,0 +1,2 @@",
                "+a = 1",
                "+b = 2",
                "diff --git a/gone.py b/gone.py",
                "deleted file mode 100644",
                "--- a/gone.py",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-x = 1",
                "diff --git a/old.py b/moved.py",
                "similarity index 90%",
                "rename from old.py",
                "rename to moved.py",
                "--- a/old.py",
                "+++ b/moved.py",
                "@@ -1 +1 @@",
                "-x = 1",
                "+x = 2",
                "diff --git a/logo.png b/logo.png",
                "Binary files a/logo.png and b/logo.png differ",
            ]
        )
        changes = dict(diff_scope.parse_unified_diff(diff))
        self.assertTrue(changes["new.py"].is_new)
        self.assertTrue(changes["new.py"].all_lines)
        self.assertEqual(changes["moved.py"].old_path, "old.py")
        self.assertEqual(changes["moved.py"].added, frozenset({1}))
        self.assertTrue(changes["logo.png"].all_lines)
        self.assertTrue(diff_scope._deleted(changes["gone.py"]))


class ChangeSetTests(unittest.TestCase):
    def test_repository_scope_includes_every_line(self):
        changes = diff_scope.repository_changes()
        self.assertFalse(changes.incremental)
        self.assertTrue(changes.includes("any.py"))
        self.assertTrue(changes.is_added("any.py", 40))
        self.assertTrue(changes.touches("any.py", 1, 2))
        self.assertEqual(changes.added_lines("any.py", 3), frozenset({1, 2, 3}))
        self.assertIsNone(changes.base_text(Path("."), "any.py"))

    def test_incremental_scope_limits_lines_and_files(self):
        change = diff_scope.FileChange("a.py", frozenset({5, 6}), ("old",))
        changes = diff_scope.ChangeSet(
            diff_scope.LOCAL_CHANGES, "abc", {"a.py": change}
        )
        self.assertTrue(changes.incremental)
        self.assertEqual(changes.paths, ("a.py",))
        self.assertFalse(changes.includes("b.py"))
        self.assertTrue(changes.is_added("a.py", 5))
        self.assertFalse(changes.is_added("a.py", 4))
        self.assertFalse(changes.is_added("b.py", 5))
        self.assertTrue(changes.touches("a.py", 1, 5))
        self.assertFalse(changes.touches("a.py", 7, 9))
        self.assertFalse(changes.touches("b.py", 1, 9))
        self.assertEqual(changes.added_lines("a.py", 10), frozenset({5, 6}))
        self.assertEqual(changes.added_lines("b.py", 10), frozenset())
        self.assertEqual(list(changes.removed_text()), [("a.py", "old")])

    def test_whole_file_change_touches_every_line(self):
        change = diff_scope.FileChange("n.py", is_new=True, all_lines=True)
        changes = diff_scope.ChangeSet(diff_scope.COMMIT, None, {"n.py": change})
        self.assertTrue(changes.is_new_file("n.py"))
        self.assertTrue(changes.is_added("n.py", 99))
        self.assertTrue(changes.touches("n.py", 3, 4))
        self.assertEqual(changes.added_lines("n.py", 2), frozenset({1, 2}))


class GitChangeSetTests(unittest.TestCase):
    def test_local_changes_collect_lines_untracked_files_and_base_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            (root / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
            (root / "old.py").write_text("gone = True\n")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "base")
            (root / "app.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")
            (root / "old.py").unlink()
            (root / "fresh.py").write_text("x = 1\ny = 2\n")

            changes = diff_scope.local_changes(root)

            self.assertEqual(changes.kind, diff_scope.LOCAL_CHANGES)
            self.assertIsNotNone(changes.base)
            self.assertEqual(changes.paths, ("app.py", "fresh.py"))
            self.assertEqual(changes.files["app.py"].added, frozenset({2, 4}))
            self.assertEqual(changes.files["app.py"].removed, ("b = 2",))
            self.assertTrue(changes.is_new_file("fresh.py"))
            self.assertEqual(changes.deleted, frozenset({"old.py"}))
            self.assertEqual(changes.base_text(root, "app.py"), "a = 1\nb = 2\nc = 3\n")
            self.assertIsNone(changes.base_text(root, "fresh.py"))

    def test_local_changes_without_any_commit_treat_files_as_new(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            (root / "a.py").write_text("x = 1\n")
            git(root, "add", "a.py")
            (root / "b.py").write_text("y = 1\n")

            changes = diff_scope.local_changes(root)

            self.assertIsNone(changes.base)
            self.assertEqual(changes.paths, ("a.py", "b.py"))
            self.assertTrue(changes.is_new_file("a.py"))
            self.assertTrue(changes.is_added("b.py", 1))

    def test_commit_changes_compare_with_first_parent_and_root_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            (root / "a.py").write_text("x = 1\n")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "first")
            first = diff_scope.commit_changes(root, "HEAD")
            self.assertIsNone(first.base)
            self.assertTrue(first.is_new_file("a.py"))

            (root / "a.py").write_text("x = 2\nz = 3\n")
            git(root, "commit", "-q", "-am", "second")
            second = diff_scope.commit_changes(root)
            self.assertEqual(second.kind, diff_scope.COMMIT)
            self.assertEqual(second.files["a.py"].added, frozenset({1, 2}))
            self.assertEqual(second.base_text(root, "a.py"), "x = 1\n")

    def test_unknown_commit_and_git_errors_raise_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            with self.assertRaises(ValueError):
                diff_scope.commit_changes(root, "missing-ref")
            with self.assertRaises(ValueError):
                diff_scope._git(root, ["diff", "not-a-revision"])

    def test_git_show_returns_none_for_missing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            (root / "a.py").write_text("x = 1\n")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "first")
            self.assertIsNone(diff_scope.git_show(root, "HEAD", "missing.py"))
            self.assertEqual(diff_scope.git_show(root, "HEAD", "a.py"), "x = 1\n")


if __name__ == "__main__":
    unittest.main()
