"""The installer packages exactly the skill files and updates installs safely."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills" / "code-discipline"
SCRIPTS = SKILL_SOURCE / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install as installer  # noqa: E402

COMMIT = "a" * 40

# Comment-only files kept at paths that 3.0.0 installers still download
# before they hand off to the current installer. They are never installed.
LEGACY_PLACEHOLDERS = tuple(
    f"scripts/{module}.py"
    for module in (
        "gherkin_check gherkin_yaml portable_analysis portable_graph "
        "project_profile project_quality quality_charts smoke_check"
    ).split()
)
# The files a 3.0.0 installer downloads before it hands off to the current one.
INSTALLER_3_0_0_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "quality-thresholds.json",
    "references/project-setup-chat.md",
    "references/quality-loop.md",
    "references/repository-setup.md",
    "scripts/quality",
    *(
        f"scripts/{name}.py"
        for name in (
            "install install_local quality_loop quality_report quality_charts "
            "quality_update quality_items portable_analysis portable_graph "
            "portable_vulnerabilities project_profile project_setup "
            "project_quality repo_quality_gate smoke_check gherkin_check "
            "gherkin_yaml"
        ).split()
    ),
}


def result_link(target: Path) -> Path:
    return target.parent.parent.parent / ".claude" / "skills" / "code-discipline"


def skill_payloads() -> dict[str, bytes]:
    return {
        relative: (SKILL_SOURCE / relative).read_bytes()
        for relative in installer.SKILL_FILES
    }


def packaged_directory_files() -> set[str]:
    files = set()
    for path in SKILL_SOURCE.rglob("*"):
        relative = path.relative_to(SKILL_SOURCE)
        hidden = any(
            part.startswith(".") or part == "__pycache__" for part in relative.parts
        )
        if path.is_file() and not hidden and path.suffix != ".pyc":
            files.add(relative.as_posix())
    return files


class SkillPackageTests(unittest.TestCase):
    def test_skill_files_list_exactly_the_files_in_the_skill_directory(self):
        self.assertEqual(len(installer.SKILL_FILES), len(set(installer.SKILL_FILES)))
        self.assertEqual(
            set(installer.SKILL_FILES) | set(LEGACY_PLACEHOLDERS),
            packaged_directory_files(),
        )
        self.assertIn("scripts/quality", installer.EXECUTABLE_FILES)
        self.assertTrue(
            all(path in installer.EXECUTABLE_FILES for path in installer.PYTHON_FILES)
        )

    def test_every_file_a_3_0_0_installer_downloads_still_exists(self):
        served = set(installer.SKILL_FILES) | set(LEGACY_PLACEHOLDERS)
        self.assertEqual(sorted(INSTALLER_3_0_0_FILES - served), [])
        self.assertFalse(set(LEGACY_PLACEHOLDERS) & set(installer.SKILL_FILES))
        for relative in LEGACY_PLACEHOLDERS:
            lines = (SKILL_SOURCE / relative).read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines and all(line.startswith("#") for line in lines))

    def test_bundled_thresholds_use_the_schema_the_installer_accepts(self):
        thresholds = json.loads(
            (SKILL_SOURCE / "quality-thresholds.json").read_text(encoding="utf-8")
        )
        self.assertEqual(thresholds["schema_version"], installer.THRESHOLDS_SCHEMA)
        self.assertEqual(installer.THRESHOLDS_SCHEMA, 3)

    def test_packaged_files_alone_run_and_initialize_a_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_copy = root / "code-discipline"
            for relative, payload in skill_payloads().items():
                destination = skill_copy / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            repository = root / "repository"
            repository.mkdir()
            for script in (
                "repo_quality_gate.py",
                "quality_loop.py",
                "install.py",
                "install_local.py",
            ):
                completed = subprocess.run(
                    [sys.executable, str(skill_copy / "scripts" / script), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, script + completed.stderr)
            initialized = subprocess.run(
                [
                    sys.executable,
                    str(skill_copy / "scripts" / "quality_loop.py"),
                    "--root",
                    str(repository),
                    "--init",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            quality = repository / ".quality"
            self.assertTrue((quality / "quality-gate.json").is_file())
            thresholds = json.loads(
                (quality / "quality-thresholds.json").read_text(encoding="utf-8")
            )
            self.assertEqual(thresholds["schema_version"], 3)
            self.assertEqual(thresholds["file_loc"]["max_lines"], 600)


class DownloadTests(unittest.TestCase):
    def test_falls_back_to_curl_when_python_https_fails(self):
        completed = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=b"skill payload", stderr=b""
        )
        with (
            mock.patch.object(
                installer, "urlopen", side_effect=installer.URLError("certificate")
            ),
            mock.patch.object(installer.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as run,
        ):
            payload = installer.download_file("https://example.test/SKILL.md", 100)
        self.assertEqual(payload, b"skill payload")
        self.assertIn("--max-filesize", run.call_args.args[0])

    def test_resolves_mutable_refs_before_downloading_the_skill(self):
        with mock.patch.object(
            installer, "download_file", return_value=COMMIT.encode()
        ) as download:
            self.assertEqual(installer.resolve_reference("refs/heads/main"), COMMIT)
        self.assertIn("refs%2Fheads%2Fmain", download.call_args.args[0])
        self.assertEqual(
            download.call_args.args[2], {"Accept": "application/vnd.github.sha"}
        )

    def test_validates_refs_urls_and_download_failures(self):
        self.assertEqual(installer.validate_ref("refs/tags/v1.0.0"), "refs/tags/v1.0.0")
        self.assertIn(
            "refs/tags/v1.0.0", installer.raw_url("refs/tags/v1.0.0", "SKILL.md")
        )
        for invalid in ("", "/main", "refs/../main", "main branch"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(installer.argparse.ArgumentTypeError):
                    installer.validate_ref(invalid)

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"ok"
        with mock.patch.object(installer, "urlopen", return_value=response):
            self.assertEqual(installer.download_file("https://example.test", 2), b"ok")
        response.read.assert_called_once_with(3)

        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not download"):
                installer.download_file("https://example.test")

        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value="curl"),
            mock.patch.object(installer.subprocess, "run", side_effect=OSError("curl")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python HTTPS or curl"):
                installer.download_file("https://example.test")

        failed = subprocess.CompletedProcess(["curl"], 22, b"", b"denied")
        with (
            mock.patch.object(installer, "urlopen", side_effect=OSError("https")),
            mock.patch.object(installer.shutil, "which", return_value="curl"),
            mock.patch.object(installer.subprocess, "run", return_value=failed),
        ):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                installer.download_file("https://example.test")

        response.read.return_value = b"too large"
        with mock.patch.object(installer, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                installer.download_file("https://example.test", 2)

    def test_rejects_invalid_metadata_and_payload_sets(self):
        with mock.patch.object(installer, "download_file", return_value=b"\xff"):
            with self.assertRaisesRegex(RuntimeError, "invalid ref metadata"):
                installer.resolve_reference("main")
        with mock.patch.object(installer, "download_file", return_value=b"short"):
            with self.assertRaisesRegex(RuntimeError, "did not resolve"):
                installer.resolve_reference("main")

        with mock.patch.object(
            installer, "download_file", return_value=b"payload"
        ) as download:
            payloads = installer.download_skill(COMMIT)
        self.assertEqual(set(payloads), set(installer.SKILL_FILES))
        self.assertEqual(download.call_count, len(installer.SKILL_FILES))

        with self.assertRaisesRegex(RuntimeError, "missing"):
            installer.validate_skill_payloads({})
        unknown_payloads = {relative: b"" for relative in installer.SKILL_FILES}
        unknown_payloads["unknown"] = b""
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            installer.validate_skill_payloads(unknown_payloads)
        installer.validate_skill_payloads(
            {relative: b"" for relative in installer.SKILL_FILES}
        )


class StagedInstallTests(unittest.TestCase):
    def test_validates_each_staged_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text("invalid", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not code-discipline"):
                installer.validate_skill_marker(root)
            (root / "SKILL.md").write_text(
                "---\nname: code-discipline\n---\n", encoding="utf-8"
            )
            installer.validate_skill_marker(root)

            thresholds = root / "quality-thresholds.json"
            for invalid in ("[]", '{"schema_version":2}'):
                with self.subTest(thresholds=invalid):
                    thresholds.write_text(invalid, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "thresholds"):
                        installer.validate_staged_thresholds(root)
            thresholds.write_text('{"schema_version":3}', encoding="utf-8")
            installer.validate_staged_thresholds(root)

            for relative in installer.PYTHON_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pass\n", encoding="utf-8")
            (root / installer.PYTHON_FILES[0]).write_text("if", encoding="utf-8")
            with self.assertRaises(SyntaxError):
                installer.validate_staged_python(root)
            (root / installer.PYTHON_FILES[0]).write_text("pass\n", encoding="utf-8")
            installer.validate_staged_python(root)

            failed = subprocess.CompletedProcess([], 1, "", "broken")
            with mock.patch.object(installer.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "failed validation"):
                    installer.staged_versions(root)
            passed = subprocess.CompletedProcess([], 0, "1.0\n", "")
            with mock.patch.object(installer.subprocess, "run", return_value=passed):
                self.assertEqual(installer.staged_versions(root), ("1.0", "1.0"))

    def test_atomically_installs_the_complete_skill(self):
        payloads = skill_payloads()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, scope_root = installer.repo_target(root)
            repository_config = root / ".quality" / "quality-gate.json"
            repository_config.parent.mkdir()
            repository_config.write_text('{"owned": true}\n', encoding="utf-8")

            versions = installer.install_skill(target, payloads, update=False)
            link = installer.claude_link_for(target, scope_root)
            installer.ensure_claude_link(link, target)

            self.assertEqual(len(versions), 2)
            self.assertTrue(installer.managed_skill(target))
            self.assertEqual(link.resolve(), target.resolve())
            self.assertEqual(
                repository_config.read_text(encoding="utf-8"), '{"owned": true}\n'
            )
            for relative in installer.SKILL_FILES:
                with self.subTest(relative=relative):
                    self.assertEqual(
                        (target / relative).read_bytes(), payloads[relative]
                    )
                    executable = os.access(target / relative, os.X_OK)
                    self.assertEqual(executable, relative in installer.EXECUTABLE_FILES)

    def test_replaces_a_managed_symlink_not_its_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared-skill"
            target = root / ".agents" / "skills" / "code-discipline"
            shutil.copytree(SKILL_SOURCE, shared)
            marker = shared / "source-marker"
            marker.write_text("preserve\n", encoding="utf-8")
            target.parent.mkdir(parents=True)
            target.symlink_to(shared, target_is_directory=True)

            installer.install_skill(target, skill_payloads(), update=True)

            self.assertFalse(target.is_symlink())
            self.assertTrue(installer.managed_skill(target))
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_refuses_to_replace_an_unmanaged_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "code-discipline"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                installer.install_skill(target, {}, update=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_restores_backup_after_partial_swap_failure(self):
        payloads = skill_payloads()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "code-discipline"
            shutil.copytree(SKILL_SOURCE, target)
            marker = target / "marker"
            marker.write_text("original", encoding="utf-8")
            real_replace = installer.os.replace
            calls = 0

            def fail_second_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("staged replace failed")
                return real_replace(source, destination)

            with mock.patch.object(
                installer.os, "replace", side_effect=fail_second_replace
            ):
                with self.assertRaisesRegex(OSError, "staged replace failed"):
                    installer.install_skill(target, payloads, update=True)

            self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_restore_is_a_no_op_without_a_moved_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            backup = root / "backup"
            installer.restore_install_backup(target, backup, False)
            target.mkdir()
            backup.mkdir()
            (backup / "marker").write_text("restored", encoding="utf-8")
            installer.restore_install_backup(target, backup, True)
            self.assertEqual(
                (target / "marker").read_text(encoding="utf-8"), "restored"
            )


class DestinationTests(unittest.TestCase):
    def test_path_link_destination_and_output_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            self.assertFalse(installer.managed_skill(missing))
            installer.remove_path(missing)
            installer.validate_claude_link(missing, root)
            directory = root / "directory"
            directory.mkdir()
            installer.remove_path(directory)
            self.assertFalse(directory.exists())

            managed = root / "managed"
            managed.mkdir()
            (managed / "SKILL.md").write_text(
                "---\nname: code-discipline\n---\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                installer.validate_install_target(managed, update=False)

            link = root / "link"
            installer.ensure_claude_link(link, managed)
            installer.ensure_claude_link(link, managed)
            installer.validate_claude_link(link, managed)
            conflict = root / "conflict"
            conflict.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "refusing"):
                installer.validate_claude_link(conflict, managed)

            update_args = SimpleNamespace(update_current=True, repo=False, root=".")
            self.assertEqual(
                installer.install_destination(update_args)[1:], (True, None)
            )
            agents_target = root / ".agents" / "skills" / "code-discipline"
            self.assertEqual(
                installer.sibling_claude_link(agents_target),
                root / ".claude" / "skills" / "code-discipline",
            )
            self.assertIsNone(installer.sibling_claude_link(root / "skills" / "x"))
            self.assertIsNone(installer.sibling_claude_link(root / "other" / "x"))
            with mock.patch.dict(
                os.environ, {installer.UPDATE_TARGET_ENV: str(agents_target)}
            ):
                self.assertEqual(
                    installer.install_destination(update_args)[2],
                    root / ".claude" / "skills" / "code-discipline",
                )
            repo_args = SimpleNamespace(update_current=False, repo=True, root=str(root))
            self.assertEqual(
                installer.install_destination(repo_args)[0],
                installer.repo_target(root)[0],
            )
            global_args = SimpleNamespace(update_current=False, repo=False, root=".")
            with mock.patch.object(installer.Path, "home", return_value=root):
                self.assertEqual(
                    installer.install_destination(global_args)[0],
                    installer.global_target()[0],
                )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                installer.print_install_result(managed, False, link, "4", "3")
                installer.print_install_result(managed, True, None, "4", "3")
            self.assertIn("Installed", output.getvalue())
            self.assertIn("Updated", output.getvalue())
            self.assertIn("Claude link", output.getvalue())

    def test_install_from_args_links_and_records_each_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            link = root / "link"
            registry = root / "installs.json"
            repo_args = SimpleNamespace(update_current=False, repo=True, root=str(root))
            with (
                mock.patch.dict(os.environ, {installer.REGISTRY_ENV: str(registry)}),
                mock.patch.object(
                    installer, "install_destination", return_value=(target, False, link)
                ),
                mock.patch.object(installer, "validate_claude_link") as validate,
                mock.patch.object(
                    installer, "install_skill", return_value=("4", "3")
                ) as install,
                mock.patch.object(installer, "ensure_claude_link") as ensure,
            ):
                result = installer.install_from_args(repo_args, {})
            self.assertEqual(result, (target, False, link, "4", "3"))
            validate.assert_called_once_with(link, target)
            install.assert_called_once_with(target, {}, False)
            ensure.assert_called_once_with(link, target)
            self.assertEqual(json.loads(registry.read_text()), [str(target)])

            update_args = SimpleNamespace(update_current=True, repo=False, root=".")
            with (
                mock.patch.dict(os.environ, {installer.REGISTRY_ENV: str(registry)}),
                mock.patch.object(
                    installer, "install_destination", return_value=(target, True, None)
                ),
                mock.patch.object(installer, "install_skill", return_value=("4", "3")),
            ):
                self.assertEqual(
                    installer.install_from_args(update_args, {}),
                    (target, True, None, "4", "3"),
                )


if __name__ == "__main__":
    unittest.main()
