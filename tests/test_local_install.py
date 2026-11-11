"""A local install is complete, project-scoped, and hands off to chat setup."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/code-discipline"
LAUNCHER = SOURCE / "scripts/quality"
if str(SOURCE / "scripts") not in sys.path:
    sys.path.insert(0, str(SOURCE / "scripts"))

import install_local as local  # noqa: E402


class LocalInstallTests(unittest.TestCase):
    def test_launcher_installs_local_files_for_both_agents_and_preserves_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project with spaces"
            quality = root / ".quality"
            quality.mkdir(parents=True)
            config = quality / "quality-gate.json"
            config.write_text('{"gates":{"tests":false}}\n')
            git = root / ".git"
            git.mkdir()
            head = git / "HEAD"
            head.write_text("ref: refs/heads/main\n")
            registry = Path(temporary) / "installs.json"
            environment = {
                **os.environ,
                "QUALITY_PYTHON": sys.executable,
                "CODE_DISCIPLINE_INSTALL_REGISTRY": str(registry),
            }
            result = subprocess.run(
                [str(LAUNCHER), "--install", "--root", str(root)],
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target = (root / ".agents/skills/code-discipline").resolve()
            for relative in local.SKILL_FILES:
                self.assertEqual(
                    (target / relative).read_bytes(), (SOURCE / relative).read_bytes()
                )
            self.assertEqual(
                (root / ".claude/skills/code-discipline").resolve(), target
            )
            self.assertEqual(json.loads(registry.read_text()), [str(target)])
            self.assertEqual(config.read_text(), '{"gates":{"tests":false}}\n')
            self.assertEqual(head.read_text(), "ref: refs/heads/main\n")
            self.assertFalse((quality / "quality-thresholds.json").exists())
            self.assertIn("continue setup in this chat, before handoff", result.stdout)
            self.assertIn(
                str(target / "references/project-setup-chat.md"), result.stdout
            )

    def test_managed_local_update_preserves_policy_and_refuses_unmanaged_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            registry = Path(temporary) / "installs.json"
            with (
                mock.patch.dict(
                    os.environ, {"CODE_DISCIPLINE_INSTALL_REGISTRY": str(registry)}
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                target = local.install_local(SOURCE, root, False)
                self.assertFalse((root / ".git").exists())
                self.assertFalse((root / ".quality").exists())
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    local.install_local(SOURCE, root, False)
                self.assertEqual(local.install_local(SOURCE, root, True), target)
                (target / "SKILL.md").write_text("unmanaged content")
                with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                    local.install_local(SOURCE, root, True)
                self.assertEqual((target / "SKILL.md").read_text(), "unmanaged content")

    def test_main_reports_failure_without_installing_and_success_after_validation(self):
        with (
            mock.patch.object(
                local, "install_local", side_effect=OSError("missing source")
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(local.main(["--root", "example"]), 2)
        self.assertIn("missing source", output.getvalue())
        with mock.patch.object(local, "install_local") as install:
            self.assertEqual(local.main(["--root", "example", "--update"]), 0)
        install.assert_called_once_with(SOURCE, Path("example"), True)

    def test_missing_local_payload_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                local.local_payloads(Path(temporary))


if __name__ == "__main__":
    unittest.main()
