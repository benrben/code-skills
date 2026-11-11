from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_state_fixture as fx  # noqa: E402
from gate_types import FAIL  # noqa: E402
from quality_state_fixture import quality_update as updater  # noqa: E402

COMMIT = "a" * 40


def quality_file(root: Path, name: str) -> Path:
    path = root / ".quality" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def skill_target(root: Path) -> Path:
    target = root / ".agents" / "skills" / "code-discipline"
    target.mkdir(parents=True)
    return target


def save_state(root: Path, summary: str = "still failing") -> Path:
    broken = fx.gate("tests", FAIL, summary, items=[fx.item()])
    value = fx.analysis(fx.with_gate(broken), root=str(root))
    value.generated_at = "original measurement"
    path = quality_file(root, "quality-gate-state.json")
    path.write_text(json.dumps(fx.state(value)))
    return path


def toolchain_fixture(root: Path):
    source = root / "upstream"
    source.mkdir()
    updater.git(source, "init", "--quiet")
    updater.git(source, "config", "user.name", "Quality test")
    updater.git(source, "config", "user.email", "quality@example.test")
    script = source / updater.SKILL_PATH / "scripts/quality_loop.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('old engine')\n")
    updater.git(source, "add", ".")
    updater.git(source, "commit", "--quiet", "-m", "old engine")
    old = updater.git(source, "rev-parse", "HEAD")
    repo = root / "project"
    manifest = quality_file(repo, "toolchain.json")
    manifest.write_text(
        json.dumps(
            {
                "repository": str(source),
                "revision": old,
                "skillPath": updater.SKILL_PATH,
                "custom": "preserved",
            }
        )
    )
    cache = manifest.with_suffix("")
    cache.mkdir()
    with mock.patch.object(updater, "UPSTREAM", str(source)):
        updater.stage_toolchain(cache, old)
    script.write_text("print('new engine')\n")
    updater.git(source, "commit", "--quiet", "-am", "new engine")
    return source, repo, manifest, cache, updater.git(source, "rev-parse", "HEAD")


class RefreshReportTests(unittest.TestCase):
    def test_refresh_renders_saved_measurements_without_changing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = save_state(root)
            original = state.read_bytes()
            html = quality_file(root, "quality-gate-report.html")
            html.write_text("old layout")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(updater.refresh_report(root), 0)
            rendered = html.read_text()
            self.assertIn("original measurement", rendered)
            self.assertIn("still failing", rendered)
            self.assertIn("Not passed. Please fix the following:", rendered)
            self.assertNotIn("hint never shown", rendered)
            self.assertEqual(state.read_bytes(), original)
            self.assertIn("measurements unchanged", output.getvalue())

    def test_missing_legacy_and_foreign_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertEqual(updater.refresh_report(root), 0)
            state = quality_file(root, "quality-gate-state.json")
            for legacy in ('{"report": {}}', "not json"):
                state.write_text(legacy)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(updater.refresh_report(root), 0)
                self.assertIn("run the loop once", output.getvalue())
            self.assertFalse((root / ".quality/quality-gate-report.html").exists())
            save_state(root / "other")
            shutil.copy(root / "other/.quality/quality-gate-state.json", state)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(updater.refresh_report(root), 2)
            self.assertIn("another repository", errors.getvalue())


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "project").mkdir()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_pinned_toolchain_updates_as_clean_checkout(self):
        source, repo, manifest, cache, commit = toolchain_fixture(self.root)
        with mock.patch.object(updater, "UPSTREAM", str(source)):
            updater.update_toolchain(repo, commit)
            self.assertEqual(updater.git(cache, "rev-parse", "HEAD"), commit)
            self.assertEqual(updater.git(cache, "status", "--porcelain"), "")
            saved = json.loads(manifest.read_text())
            self.assertEqual(
                (saved["revision"], saved["custom"]), (commit, "preserved")
            )
            with mock.patch.object(updater, "stage_toolchain") as stage:
                updater.update_toolchain(repo, commit)
                stage.assert_not_called()
            shutil.rmtree(cache)
            updater.update_toolchain(repo, commit)
            self.assertEqual(updater.git(cache, "rev-parse", "HEAD"), commit)

    def test_failed_download_or_dirty_cache_keeps_pin_and_engine(self):
        source, repo, manifest, cache, commit = toolchain_fixture(self.root)
        before = manifest.read_bytes()
        old = updater.git(cache, "rev-parse", "HEAD")
        with mock.patch.object(updater, "UPSTREAM", str(source)):
            with self.assertRaisesRegex(RuntimeError, "Git fetch failed"):
                updater.update_toolchain(repo, "f" * 40)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(updater.git(cache, "rev-parse", "HEAD"), old)
            (cache / "local-work.txt").write_text("keep me")
            with self.assertRaisesRegex(ValueError, "local changes"):
                updater.update_toolchain(repo, commit)
            self.assertEqual(manifest.read_bytes(), before)

    def test_failed_manifest_write_rolls_back_cache(self):
        source, repo, manifest, cache, commit = toolchain_fixture(self.root)
        before = manifest.read_bytes()
        failing = mock.patch.object(
            updater, "write_json_atomic", side_effect=OSError("disk full")
        )
        with mock.patch.object(updater, "UPSTREAM", str(source)):
            with failing, self.assertRaisesRegex(RuntimeError, "disk full"):
                updater.update_toolchain(repo, commit)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(
                updater.git(cache, "rev-parse", "HEAD"), json.loads(before)["revision"]
            )
            shutil.rmtree(cache)
            with failing, self.assertRaises(RuntimeError):
                updater.update_toolchain(repo, commit)
            self.assertFalse(cache.exists())

    def test_failed_cache_move_preserves_previous_checkout(self):
        source, repo, manifest, cache, commit = toolchain_fixture(self.root)
        old = updater.git(cache, "rev-parse", "HEAD")
        original_rename = Path.rename

        def fail_install(path, destination):
            if path.name == "next":
                raise OSError("cannot move staged checkout")
            return original_rename(path, destination)

        with (
            mock.patch.object(updater, "UPSTREAM", str(source)),
            mock.patch.object(Path, "rename", fail_install),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot move staged"):
                updater.update_toolchain(repo, commit)
        self.assertEqual(updater.git(cache, "rev-parse", "HEAD"), old)
        self.assertEqual(json.loads(manifest.read_text())["revision"], old)

    def test_manifests_and_checkout_identity_are_validated(self):
        source, repo, manifest, cache, commit = toolchain_fixture(self.root)
        original = manifest.read_text()
        for bad in [
            [],
            {},
            {"repository": "other", "skillPath": updater.SKILL_PATH},
            {"repository": updater.UPSTREAM, "skillPath": "other"},
            {
                "repository": updater.UPSTREAM,
                "skillPath": updater.SKILL_PATH,
                "revision": "main",
            },
        ]:
            manifest.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                updater.read_manifest(manifest)
        manifest.write_text(original)
        with self.assertRaisesRegex(ValueError, "origin"):
            updater.validate_cache(cache, commit)
        with mock.patch.object(updater, "UPSTREAM", str(source)):
            with self.assertRaisesRegex(ValueError, "revision"):
                updater.validate_cache(cache, commit)
        link = self.root / "linked-cache"
        link.symlink_to(cache, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "isolated"):
            updater.validate_cache(link, commit)
        with self.assertRaisesRegex(ValueError, "isolated"):
            updater.validate_cache(self.root, commit)
        with mock.patch.object(updater, "git", return_value=str(self.root)):
            with self.assertRaisesRegex(ValueError, "root does not match"):
                updater.validate_cache(cache, commit)
        script = cache / updater.SKILL_PATH / "scripts/quality_loop.py"
        script.unlink()
        with self.assertRaisesRegex(ValueError, "entry point"):
            updater.validate_entrypoint(cache)
        script.symlink_to(source / updater.SKILL_PATH / "scripts/quality_loop.py")
        with self.assertRaisesRegex(ValueError, "outside"):
            updater.validate_entrypoint(cache)


class RecordingLock:
    def __init__(self):
        self.roots: list[Path] = []

    @contextlib.contextmanager
    def __call__(self, root: Path):
        self.roots.append(root)
        yield


class UpdateRepositoryTests(unittest.TestCase):
    def test_repository_discovery_and_refresh_under_the_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = skill_target(root)
            lock = RecordingLock()
            self.assertIsNone(updater.repository_root(root))
            self.assertIsNone(updater.repository_root(target))
            self.assertEqual(
                updater.update_repository(target, COMMIT, run_lock=lock), 0
            )
            self.assertEqual(lock.roots, [])
            save_state(root)
            self.assertEqual(updater.repository_root(target), root)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    updater.update_repository(target, COMMIT, run_lock=lock), 0
                )
            self.assertEqual(lock.roots, [root])
            self.assertTrue((root / ".quality/quality-gate-report.html").is_file())

    def test_default_lock_comes_from_the_quality_loop(self):
        entered = []

        @contextlib.contextmanager
        def repository_run_lock(root):
            entered.append(root)
            yield

        fake = types.SimpleNamespace(repository_run_lock=repository_run_lock)
        with mock.patch.dict(sys.modules, {"quality_loop": fake}):
            with updater.loop_run_lock(Path(".")):
                self.assertEqual(entered, [Path(".")])

    def test_main_validates_the_reference_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = skill_target(root)
            self.assertEqual(updater.main(["--skill", str(target), "--ref", COMMIT]), 0)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(
                    updater.main(["--skill", str(target), "--ref", "main"]), 2
                )
            self.assertIn("full commit", errors.getvalue())
            failure = RuntimeError("quality loop already running")
            with (
                mock.patch.object(updater, "update_repository", side_effect=failure),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(
                    updater.main(["--skill", str(target), "--ref", COMMIT]), 2
                )
            self.assertIn("already running", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
