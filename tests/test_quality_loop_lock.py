"""One quality loop at a time per repository, on POSIX and Windows locks."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quality_loop  # noqa: E402


class RepositoryLockTests(unittest.TestCase):
    def test_second_run_for_the_same_repository_is_refused_with_its_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with quality_loop.repository_run_lock(root):
                with self.assertRaisesRegex(
                    quality_loop.ConcurrentRunError,
                    rf"already running for .* \(PID {os.getpid()}, started ",
                ):
                    with quality_loop.repository_run_lock(root):
                        self.fail("a second run acquired the lock")
            with quality_loop.repository_run_lock(root):
                lock = quality_loop.repository_lock_path(root)
                self.assertTrue(lock.is_file())

    def test_windows_lock_writes_a_byte_and_reports_contention(self):
        with tempfile.TemporaryFile(mode="w+") as handle:
            locking = mock.Mock()
            windows = SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2)
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows}),
            ):
                self.assertTrue(quality_loop.try_lock(handle))
                handle.seek(0, os.SEEK_END)
                self.assertEqual(handle.tell(), 1)
                quality_loop.unlock(handle)
            self.assertEqual(locking.call_count, 2)

            windows.locking = mock.Mock(side_effect=OSError(errno.EACCES, "locked"))
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows}),
            ):
                self.assertFalse(quality_loop.try_lock(handle))

            windows.locking = mock.Mock(side_effect=OSError(errno.EINVAL, "invalid"))
            with (
                mock.patch.object(quality_loop.os, "name", "nt"),
                mock.patch.dict(sys.modules, {"msvcrt": windows}),
            ):
                with self.assertRaises(OSError):
                    quality_loop.try_lock(handle)

    def test_posix_contention_returns_false(self):
        with tempfile.TemporaryFile(mode="w+") as handle:
            with mock.patch("fcntl.flock", side_effect=BlockingIOError):
                self.assertFalse(quality_loop.try_lock(handle))

    def test_lock_owner_reads_json_text_or_nothing(self):
        with tempfile.TemporaryFile(mode="w+") as handle:
            self.assertEqual(
                quality_loop.lock_owner(handle), "owner details unavailable"
            )
            for raw in ("not-json", "[1]"):
                handle.seek(0)
                handle.truncate()
                handle.write(raw)
                handle.flush()
                self.assertEqual(quality_loop.lock_owner(handle), raw)
            handle.seek(0)
            handle.truncate()
            json.dump({"pid": 7, "started_at": "now"}, handle)
            handle.flush()
            self.assertEqual(quality_loop.lock_owner(handle), "PID 7, started now")


if __name__ == "__main__":
    unittest.main()
