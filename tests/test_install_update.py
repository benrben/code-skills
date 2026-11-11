"""Updates hand off to a newer installer and refresh every registered install."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_install import COMMIT, installer, result_link, skill_payloads


class UpdateTests(unittest.TestCase):
    def test_update_fetches_the_installer_first_and_hands_off_when_it_differs(self):
        current = installer.current_installer().read_bytes()
        update = SimpleNamespace(update_current=True, update_all=False)
        everything = SimpleNamespace(update_current=False, update_all=True)
        fresh = SimpleNamespace(update_current=False, update_all=False)
        self.assertEqual(installer.update_flag(update), "--update-current")
        self.assertEqual(installer.update_flag(everything), "--update-all")
        self.assertIsNone(installer.update_flag(fresh))

        with mock.patch.object(installer, "download_file") as download:
            self.assertIsNone(installer.newer_installer(fresh, COMMIT))
            with mock.patch.dict(os.environ, {installer.HANDOFF_ENV: "1"}):
                self.assertIsNone(installer.newer_installer(update, COMMIT))
        download.assert_not_called()

        with mock.patch.object(
            installer, "download_file", return_value=current
        ) as download:
            self.assertIsNone(installer.newer_installer(update, COMMIT))
        self.assertEqual(
            download.call_args.args[0],
            installer.raw_url(COMMIT, installer.INSTALLER_FILE),
        )
        newer = current + b"\n# newer\n"
        with mock.patch.object(installer, "download_file", return_value=newer):
            self.assertEqual(installer.newer_installer(everything, COMMIT), newer)

        with mock.patch.dict(os.environ, {installer.UPDATE_TARGET_ENV: "/handed"}):
            self.assertEqual(installer.update_target(), Path("/handed"))
        with mock.patch.dict(os.environ, {installer.UPDATE_TARGET_ENV: ""}):
            self.assertEqual(
                installer.update_target(), installer.current_installer().parent.parent
            )

    def test_main_hands_off_before_downloading_the_rest_of_the_skill(self):
        for flag, code in (("--update-current", 7), ("--update-all", 5)):
            with (
                self.subTest(flag=flag),
                mock.patch.object(installer, "resolve_reference", return_value=COMMIT),
                mock.patch.object(installer, "newer_installer", return_value=b"new"),
                mock.patch.object(installer, "download_skill") as download,
                mock.patch.object(
                    installer, "run_downloaded_installer", return_value=code
                ) as handoff,
            ):
                self.assertEqual(installer.main([flag]), code)
                download.assert_not_called()
                handoff.assert_called_once_with(
                    b"new", flag, installer.update_target(), COMMIT
                )

    def test_main_reports_success_and_failure(self):
        result = (Path("target"), False, None, "4", "3")
        payloads = {installer.INSTALLER_FILE: b"fresh"}
        with (
            mock.patch.object(installer, "resolve_reference", return_value=COMMIT),
            mock.patch.object(installer, "download_skill", return_value=payloads),
            mock.patch.object(
                installer, "install_from_args", return_value=result
            ) as install,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(installer.main(["--repo"]), 0)
        install.assert_called_once()
        self.assertIs(install.call_args.args[1], payloads)
        self.assertIn(f"Source commit: {COMMIT}", output.getvalue())
        with (
            mock.patch.object(installer, "resolve_reference", return_value=COMMIT),
            mock.patch.object(installer, "download_skill", return_value=payloads),
            mock.patch.object(
                installer, "install_from_args", side_effect=RuntimeError("broken")
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(installer.main(["--repo"]), 2)
        self.assertIn("broken", stderr.getvalue())

    def test_runs_the_downloaded_installer_with_the_update_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            script = (
                "import json, os, sys\n"
                f"json.dump({{'argv': sys.argv[1:], 'handoff': "
                f"os.environ.get({installer.HANDOFF_ENV!r}), 'target': "
                f"os.environ.get({installer.UPDATE_TARGET_ENV!r})}}, "
                f"open({str(report)!r}, 'w'))\n"
                "sys.exit(3)\n"
            ).encode()
            code = installer.run_downloaded_installer(
                script, "--update-current", Path(temporary) / "skill", "c" * 40
            )
            self.assertEqual(code, 3)
            recorded = json.loads(report.read_text())
        self.assertEqual(recorded["argv"], ["--update-current", "--ref", "c" * 40])
        self.assertEqual(recorded["handoff"], "1")
        self.assertEqual(recorded["target"], str(Path(temporary) / "skill"))
        with self.assertRaises(SyntaxError):
            installer.run_downloaded_installer(
                b"def (", "--update-all", Path(temporary), "c" * 40
            )

    def test_each_update_finishes_with_a_repository_refresh(self):
        result = (Path("installed"), True, None, "4", "3")
        with (
            mock.patch.object(
                installer.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            installer.finish_installations([result], COMMIT)
        self.assertEqual(
            run.call_args.args[0],
            [
                sys.executable,
                "-B",
                "installed/scripts/quality_update.py",
                "--skill",
                "installed",
                "--ref",
                COMMIT,
            ],
        )
        with (
            mock.patch.object(
                installer.subprocess, "run", return_value=SimpleNamespace(returncode=2)
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "repository refresh failed"):
                installer.finish_installations([result], COMMIT)

    def test_fresh_installs_skip_the_repository_refresh(self):
        result = (Path("installed"), False, None, "4", "3")
        with (
            mock.patch.object(installer.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            installer.finish_installations([result], COMMIT)
        run.assert_not_called()


class RegistryTests(unittest.TestCase):
    def test_registry_records_each_install_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry" / "installs.json"
            with mock.patch.dict(os.environ, {installer.REGISTRY_ENV: str(registry)}):
                self.assertEqual(installer.registry_path(), registry)
                self.assertEqual(installer.registered_installs(), [])
                installer.record_install(root / "one")
                installer.record_install(root / "two")
                installer.record_install(root / "one")
                self.assertEqual(
                    installer.registered_installs(), [root / "one", root / "two"]
                )
                self.assertTrue(registry.read_text(encoding="utf-8").endswith("]\n"))
                registry.write_text("{", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "unreadable"):
                    installer.registered_installs()
                for invalid in ('{"installs": []}', "[1]"):
                    registry.write_text(invalid, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "list of paths"):
                        installer.registered_installs()
            with (
                mock.patch.dict(os.environ, {installer.REGISTRY_ENV: ""}),
                mock.patch.object(installer.Path, "home", return_value=root),
            ):
                self.assertEqual(
                    installer.registry_path(),
                    root / ".agents" / "code-discipline-installs.json",
                )

    def test_update_all_refreshes_every_registered_install(self):
        payloads = skill_payloads()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "installs.json"
            with mock.patch.dict(os.environ, {installer.REGISTRY_ENV: str(registry)}):
                with self.assertRaisesRegex(RuntimeError, "no installs are registered"):
                    installer.update_registered_installs(payloads)

                first, _ = installer.repo_target(root / "first")
                second, _ = installer.repo_target(root / "second")
                for target in (first, second):
                    installer.install_skill(target, payloads, update=False)
                    (target / "SKILL.md").write_text(
                        "---\nname: code-discipline\n---\nold\n", encoding="utf-8"
                    )
                second_link = installer.claude_link_for(second, root / "second")
                second_link.parent.mkdir(parents=True)
                second_link.symlink_to(second)
                gone = root / "gone" / ".agents" / "skills" / "code-discipline"
                for target in (first, gone, second):
                    installer.record_install(target)

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    results = installer.update_registered_installs(payloads)

                self.assertEqual([result[0] for result in results], [first, second])
                self.assertEqual(
                    [result[2].resolve() for result in results],
                    [result_link(first).resolve(), second_link.resolve()],
                )
                self.assertIn(f"skipped {gone}", stderr.getvalue())
                for target in (first, second):
                    self.assertEqual(
                        (target / "SKILL.md").read_bytes(), payloads["SKILL.md"]
                    )
                    self.assertEqual(result_link(target).resolve(), target.resolve())
                self.assertEqual(installer.registered_installs(), [first, gone, second])

                blocked = result_link(first)
                blocked.unlink()
                blocked.write_text("occupied", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    installer.update_install(first, payloads)

                loose = root / "loose"
                installer.install_skill(loose, payloads, update=False)
                target_result = installer.update_install(loose, payloads)
                self.assertEqual(target_result[:3], (loose, True, None))
                self.assertIn(loose, installer.registered_installs())

            output = io.StringIO()
            with (
                mock.patch.object(installer, "resolve_reference", return_value=COMMIT),
                mock.patch.object(installer, "newer_installer", return_value=None),
                mock.patch.object(installer, "download_skill", return_value=payloads),
                mock.patch.object(
                    installer,
                    "update_registered_installs",
                    return_value=[
                        (first, True, None, "4", "3"),
                        (second, True, None, "4", "3"),
                    ],
                ),
                mock.patch.object(
                    installer.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(installer.main(["--update-all"]), 0)
            self.assertEqual(output.getvalue().count("Updated code-discipline"), 2)


if __name__ == "__main__":
    unittest.main()
