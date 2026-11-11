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
import gate_file_size  # noqa: E402

LIMIT = 5


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


def write_lines(root: Path, relative: str, count: int, prefix: str = "x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{prefix} = {n}\n" for n in range(count)))
    return path


def commit_all(root: Path, message: str = "base") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        make_repository(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def evaluate(self, files, changes):
        return gate_file_size.evaluate_file_size(self.root, files, changes, LIMIT)


class RepositoryScopeTests(RepositoryTestCase):
    def test_every_production_file_over_the_limit_fails(self):
        big = write_lines(self.root, "app/big.py", LIMIT + 2)
        exact = write_lines(self.root, "app/exact.py", LIMIT)
        outcome = self.evaluate([big, exact], diff_scope.repository_changes())
        self.assertEqual("fail", outcome.status)
        self.assertEqual("file_loc", outcome.key)
        self.assertEqual("File size", outcome.title)
        self.assertEqual(["app/big.py"], [item.path for item in outcome.items])
        item = outcome.items[0]
        self.assertEqual(("lines", "7", "5"), (item.metric, item.value, item.limit))
        self.assertIsNone(item.line)
        self.assertIn("Production files over 5 lines: 1 of 2.", outcome.summary)
        self.assertEqual(2, outcome.measurements["files_checked"])
        self.assertEqual("app/big.py", outcome.measurements["files"][0]["path"])

    def test_files_at_or_below_the_limit_pass(self):
        small = write_lines(self.root, "small.py", 1)
        outcome = self.evaluate([small], diff_scope.repository_changes())
        self.assertEqual("pass", outcome.status)
        self.assertEqual(
            "Production files at or below 5 lines: 1 of 1.", outcome.summary
        )
        self.assertEqual([], outcome.details)

    def test_relative_paths_and_missing_files_are_handled(self):
        write_lines(self.root, "rel.py", LIMIT + 1)
        outcome = self.evaluate(
            [Path("rel.py"), Path("gone.py")], diff_scope.repository_changes()
        )
        self.assertEqual(["rel.py"], [item.path for item in outcome.items])
        self.assertEqual(1, outcome.measurements["files_checked"])


class LocalChangesTests(RepositoryTestCase):
    def test_a_change_that_crosses_the_limit_fails(self):
        path = write_lines(self.root, "app.py", LIMIT)
        commit_all(self.root)
        write_lines(self.root, "app.py", LIMIT + 1)
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("6 (was 5)", outcome.items[0].value)
        self.assertIn("1 of 1 changed production file crossed", outcome.summary)

    def test_a_file_already_over_the_limit_that_grew_fails(self):
        path = write_lines(self.root, "app.py", LIMIT + 2)
        commit_all(self.root)
        write_lines(self.root, "app.py", LIMIT + 3)
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("8 (was 7)", outcome.items[0].value)

    def test_a_file_already_over_the_limit_that_did_not_grow_passes_with_a_note(
        self,
    ):
        path = write_lines(self.root, "app.py", LIMIT + 3)
        commit_all(self.root)
        write_lines(self.root, "app.py", LIMIT + 2, prefix="y")
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)
        self.assertIn("No changed production file crossed", outcome.summary)
        self.assertEqual(
            [
                "app.py is over the limit (7 (was 8) lines, limit 5) "
                "but the change did not make it longer."
            ],
            outcome.details,
        )

    def test_unchanged_oversized_files_are_out_of_scope(self):
        old = write_lines(self.root, "old.py", LIMIT + 10)
        small = write_lines(self.root, "small.py", 1)
        commit_all(self.root)
        write_lines(self.root, "small.py", 2)
        outcome = self.evaluate([old, small], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)
        self.assertEqual(
            ["small.py"], [f["path"] for f in outcome.measurements["files"]]
        )
        self.assertEqual(1, outcome.measurements["files"][0]["base_lines"])

    def test_a_new_untracked_file_over_the_limit_fails(self):
        write_lines(self.root, "keep.py", 1)
        commit_all(self.root)
        new = write_lines(self.root, "new.py", LIMIT + 1)
        outcome = self.evaluate([new], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("6 (new file)", outcome.items[0].value)
        self.assertTrue(outcome.measurements["files"][0]["is_new"])

    def test_a_new_file_at_the_limit_passes(self):
        write_lines(self.root, "keep.py", 1)
        commit_all(self.root)
        new = write_lines(self.root, "new.py", LIMIT)
        outcome = self.evaluate([new], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)

    def test_a_renamed_file_is_compared_with_its_old_path(self):
        write_lines(self.root, "old_name.py", LIMIT + 4)
        commit_all(self.root)
        git(self.root, "mv", "old_name.py", "new_name.py")
        path = write_lines(self.root, "new_name.py", LIMIT + 3)
        changes = diff_scope.local_changes(self.root)
        self.assertEqual("old_name.py", changes.base_path("new_name.py"))
        outcome = self.evaluate([path], changes)
        self.assertEqual("pass", outcome.status)
        self.assertEqual(LIMIT + 4, outcome.measurements["files"][0]["base_lines"])

    def test_deleted_files_are_ignored(self):
        gone = write_lines(self.root, "gone.py", LIMIT + 4)
        commit_all(self.root)
        gone.unlink()
        changes = diff_scope.local_changes(self.root)
        self.assertIn("gone.py", changes.deleted)
        gone.write_text("x\n" * (LIMIT + 9))
        outcome = self.evaluate([gone], changes)
        self.assertEqual("pass", outcome.status)
        self.assertEqual(0, outcome.measurements["files_checked"])

    def test_crlf_base_lines_are_counted_like_the_working_tree(self):
        path = self.root / "win.py"
        path.write_bytes(b"a\r\nb\r\nc\r\nd\r\ne\r\nf\r\n")
        commit_all(self.root)
        path.write_bytes(b"a\r\nb\r\nc\r\nd\r\ne\r\ng\r\n")
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)
        self.assertEqual(6, outcome.measurements["files"][0]["base_lines"])


class CommitScopeTests(RepositoryTestCase):
    def test_a_commit_that_grows_an_oversized_file_fails(self):
        path = write_lines(self.root, "app.py", LIMIT + 1)
        commit_all(self.root, "first")
        write_lines(self.root, "app.py", LIMIT + 2)
        commit_all(self.root, "second")
        outcome = self.evaluate([path], diff_scope.commit_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("7 (was 6)", outcome.items[0].value)

    def test_a_root_commit_treats_every_file_as_new(self):
        path = write_lines(self.root, "app.py", LIMIT + 1)
        commit_all(self.root, "first")
        outcome = self.evaluate([path], diff_scope.commit_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("6 (new file)", outcome.items[0].value)


class QuotedPathTests(RepositoryTestCase):
    """Git C-quotes these names in diff headers; they must stay in scope."""

    def test_a_tracked_non_ascii_file_that_crosses_the_limit_fails(self):
        path = write_lines(self.root, "src/café.py", LIMIT)
        commit_all(self.root)
        write_lines(self.root, "src/café.py", LIMIT + 4)
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual(["src/café.py"], [item.path for item in outcome.items])
        self.assertEqual("9 (was 5)", outcome.items[0].value)

    def test_a_non_ascii_debt_file_that_did_not_grow_keeps_its_base(self):
        path = write_lines(self.root, "src/naïve.py", LIMIT + 3)
        commit_all(self.root)
        write_lines(self.root, "src/naïve.py", LIMIT + 3, prefix="y")
        outcome = self.evaluate([path], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)
        self.assertEqual(LIMIT + 3, outcome.measurements["files"][0]["base_lines"])

    def test_staged_new_files_with_quoted_names_fail(self):
        write_lines(self.root, "keep.py", 1)
        commit_all(self.root)
        names = ["src/café.py", 'src/a"b.py', "src/back\\slash.py"]
        paths = [write_lines(self.root, name, LIMIT + 1) for name in names]
        git(self.root, "add", "-A")
        outcome = self.evaluate(paths, diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual(sorted(names), sorted(item.path for item in outcome.items))

    def test_a_commit_adding_a_non_ascii_file_fails(self):
        write_lines(self.root, "keep.py", 1)
        commit_all(self.root, "first")
        path = write_lines(self.root, "src/café.py", LIMIT + 1)
        commit_all(self.root, "second")
        outcome = self.evaluate([path], diff_scope.commit_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("6 (new file)", outcome.items[0].value)

    def test_a_deleted_non_ascii_file_is_ignored(self):
        gone = write_lines(self.root, "src/café.py", LIMIT + 4)
        commit_all(self.root)
        gone.unlink()
        changes = diff_scope.local_changes(self.root)
        gone.write_text("x\n" * (LIMIT + 9))
        outcome = self.evaluate([gone], changes)
        self.assertEqual(0, outcome.measurements["files_checked"])


class UnreadableFileTests(RepositoryTestCase):
    def test_an_unreadable_file_blocks_instead_of_crashing(self):
        path = write_lines(self.root, "a.py", 1)
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(
            gate_file_size.source_files, "physical_line_count", side_effect=denied
        ):
            outcome = self.evaluate([path], diff_scope.repository_changes())
        self.assertEqual("blocked", outcome.status)
        self.assertEqual("Could not read 1 production file: a.py.", outcome.summary)
        self.assertEqual(["Could not read a.py: Permission denied"], outcome.details)
        self.assertEqual(["a.py"], outcome.measurements["unreadable"])

    def test_real_failures_stay_failures_next_to_an_unreadable_file(self):
        big = write_lines(self.root, "big.py", LIMIT + 1)
        locked = write_lines(self.root, "locked.py", 1)
        real_count = gate_file_size.source_files.physical_line_count

        def count(path):
            if path.name == "locked.py":
                raise OSError("disk error")
            return real_count(path)

        with mock.patch.object(
            gate_file_size.source_files, "physical_line_count", side_effect=count
        ):
            outcome = self.evaluate([big, locked], diff_scope.repository_changes())
        self.assertEqual("fail", outcome.status)
        self.assertIn("Could not read locked.py: disk error", outcome.details)


class MovedFileTests(RepositoryTestCase):
    def test_an_unstaged_move_of_an_oversized_file_is_not_a_new_file(self):
        old = write_lines(self.root, "src/legacy.py", LIMIT + 5)
        commit_all(self.root)
        moved = self.root / "src" / "moved.py"
        old.rename(moved)
        outcome = self.evaluate([moved], diff_scope.local_changes(self.root))
        self.assertEqual("pass", outcome.status)
        row = outcome.measurements["files"][0]
        self.assertEqual(
            ("src/legacy.py", LIMIT + 5), (row["moved_from"], row["base_lines"])
        )
        self.assertIn("10 (was 10 as src/legacy.py)", outcome.details[0])

    def test_a_moved_file_that_grew_still_fails(self):
        old = write_lines(self.root, "legacy.py", LIMIT + 5)
        commit_all(self.root)
        old.unlink()
        moved = write_lines(self.root, "moved.py", LIMIT + 6)
        outcome = self.evaluate([moved], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("11 (was 10 as legacy.py)", outcome.items[0].value)

    def test_an_unrelated_new_file_next_to_a_deletion_is_still_new(self):
        old = write_lines(self.root, "legacy.py", LIMIT + 5)
        commit_all(self.root)
        old.unlink()
        new = write_lines(self.root, "new.py", LIMIT + 1, prefix="z")
        outcome = self.evaluate([new], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("6 (new file)", outcome.items[0].value)

    def test_an_unreadable_move_candidate_stays_new(self):
        old = write_lines(self.root, "legacy.py", LIMIT + 5)
        commit_all(self.root)
        moved = self.root / "moved.py"
        old.rename(moved)
        with mock.patch.object(
            gate_file_size.source_files, "read_text", side_effect=OSError("gone")
        ):
            outcome = self.evaluate([moved], diff_scope.local_changes(self.root))
        self.assertEqual("fail", outcome.status)
        self.assertEqual("10 (new file)", outcome.items[0].value)

    def test_without_a_base_revision_nothing_is_a_move(self):
        size = gate_file_size.FileSize("new.py", LIMIT + 1, None, True)
        changes = diff_scope.ChangeSet(
            diff_scope.LOCAL_CHANGES, None, {}, frozenset({"old.py"})
        )
        result = gate_file_size.recognize_moves(self.root, [size], changes, LIMIT)
        self.assertEqual([size], result)


class HelperTests(unittest.TestCase):
    def test_text_line_count_matches_physical_lines(self):
        count = gate_file_size.text_line_count
        self.assertEqual(0, count(""))
        self.assertEqual(1, count("a"))
        self.assertEqual(2, count("a\nb\n"))
        self.assertEqual(3, count("a\r\nb\rc"))

    def test_violation_rules(self):
        size = gate_file_size.FileSize
        violates = gate_file_size.violates
        self.assertFalse(violates(size("a", 5, 9), 5, incremental=True))
        self.assertTrue(violates(size("a", 6, 5), 5, incremental=True))
        self.assertTrue(violates(size("a", 7, 6), 5, incremental=True))
        self.assertFalse(violates(size("a", 6, 6), 5, incremental=True))
        self.assertTrue(violates(size("a", 6, 6), 5, incremental=False))
        self.assertTrue(violates(size("a", 6, None, True), 5, incremental=True))

    def test_describe_value(self):
        size = gate_file_size.FileSize
        self.assertEqual("6", size("a", 6, None).describe_value())
        self.assertEqual("6 (was 4)", size("a", 6, 4).describe_value())
        self.assertEqual("6 (new file)", size("a", 6, None, True).describe_value())

    def test_unquote_git_path(self):
        unquote = gate_file_size.unquote_git_path
        self.assertEqual("plain.py", unquote("plain.py"))
        self.assertEqual("src/café.py", unquote("src/caf\\303\\251.py"))
        self.assertEqual('a"b.py', unquote('a\\"b.py'))
        self.assertEqual("back\\slash.py", unquote("back\\\\slash.py"))
        self.assertEqual("tab\there", unquote("tab\\there"))
        self.assertEqual("odd\\q.py", unquote("odd\\q.py"))

    def test_decode_change_paths(self):
        changes = diff_scope.ChangeSet(
            diff_scope.COMMIT,
            "base",
            {
                "caf\\303\\251.py": diff_scope.FileChange(
                    "caf\\303\\251.py", old_path="old\\303\\251.py"
                ),
                "plain.py": diff_scope.FileChange("plain.py"),
            },
            frozenset({"gone\\303\\251.py"}),
        )
        decoded = gate_file_size.decode_change_paths(changes)
        self.assertEqual(["café.py", "plain.py"], sorted(decoded.files))
        self.assertEqual("oldé.py", decoded.base_path("café.py"))
        self.assertEqual("café.py", decoded.files["café.py"].path)
        self.assertEqual(frozenset({"goneé.py"}), decoded.deleted)
        repository = diff_scope.repository_changes()
        self.assertIs(repository, gate_file_size.decode_change_paths(repository))

    def test_similarity(self):
        similarity = gate_file_size.similarity
        self.assertEqual(1.0, similarity("", ""))
        self.assertEqual(1.0, similarity("a\nb\n", "b\na\n"))
        self.assertEqual(0.5, similarity("a\nb\n", "a\nc\n"))
        self.assertEqual(0.0, similarity("a\n", ""))


if __name__ == "__main__":
    unittest.main()
