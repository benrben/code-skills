from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "skills" / "code-discipline" / "scripts" / "quality"
LOOP = LAUNCHER.with_name("quality_loop.py")


class PortableLauncherTests(unittest.TestCase):
    def write_executable(self, path: Path, source: str) -> Path:
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_launcher(
        self, arguments: list[str], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_uses_existing_compatible_python_and_forwards_every_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            log = temp / "python-arguments"
            python = self.write_executable(
                temp / "python3",
                """
                #!/bin/sh
                if [ "$1" = "-c" ]; then
                    exit 0
                fi
                printf '%s\\n' "$@" > "$QUALITY_TEST_LOG"
                exit 17
                """,
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "QUALITY_PYTHON": str(python),
                    "QUALITY_TEST_LOG": str(log),
                }
            )

            result = self.run_launcher(
                ["--root", "path with spaces", "--fast", "--no-install"], environment
            )

            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [
                    str(LOOP),
                    "--root",
                    "path with spaces",
                    "--fast",
                    "--no-install",
                ],
            )

    def test_rejects_unsupported_operating_system_and_architecture(self) -> None:
        cases = (
            ("Plan9", "x86_64", "unsupported operating system 'Plan9'"),
            ("Linux", "sparc64", "unsupported architecture 'sparc64' on Linux"),
        )
        for operating_system, architecture, expected in cases:
            with self.subTest(
                operating_system=operating_system, architecture=architecture
            ):
                with tempfile.TemporaryDirectory() as temporary:
                    temp = Path(temporary)
                    uname = self.write_executable(
                        temp / "uname",
                        f"""
                        #!/bin/sh
                        case "$1" in
                            -s) printf '%s\\n' '{operating_system}' ;;
                            -m) printf '%s\\n' '{architecture}' ;;
                        esac
                        """,
                    )
                    self.assertTrue(uname.exists())
                    environment = os.environ.copy()
                    environment["PATH"] = f"{temp}:{environment['PATH']}"

                    result = self.run_launcher([], environment)

                    self.assertEqual(result.returncode, 2)
                    self.assertIn(expected, result.stderr)

    def test_uv_override_uses_managed_python_without_network_or_project_sync(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            python_log = temp / "managed-python-arguments"
            uv_log = temp / "uv-arguments"
            uv_ready = temp / "managed-python-installed"
            network_log = temp / "network-was-used"
            incompatible_python = self.write_executable(
                temp / "old-python",
                """
                #!/bin/sh
                exit 1
                """,
            )
            managed_python = self.write_executable(
                temp / "managed-python",
                """
                #!/bin/sh
                if [ "$1" = "-c" ]; then
                    exit 0
                fi
                printf '%s\\n' "$@" > "$QUALITY_TEST_LOG"
                exit 23
                """,
            )
            uv = self.write_executable(
                temp / "uv",
                f"""
                #!/bin/sh
                printf '%s\\n' "$*" >> "$QUALITY_UV_LOG"
                printf 'python-bin=%s\\n' "$UV_PYTHON_BIN_DIR" >> "$QUALITY_UV_LOG"
                if [ "$1 $2" = "python find" ]; then
                    [ -f "$QUALITY_UV_READY" ] || exit 1
                    printf '%s\\n' '{managed_python}'
                    exit 0
                fi
                if [ "$1 $2" = "python install" ]; then
                    : > "$QUALITY_UV_READY"
                    exit 0
                fi
                exit 91
                """,
            )
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            for name in ("curl", "wget"):
                self.write_executable(
                    fake_bin / name,
                    """
                    #!/bin/sh
                    printf '%s\\n' "$0" >> "$QUALITY_NETWORK_LOG"
                    exit 99
                    """,
                )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "QUALITY_NETWORK_LOG": str(network_log),
                    "QUALITY_PYTHON": str(incompatible_python),
                    "QUALITY_TEST_LOG": str(python_log),
                    "QUALITY_UV_BINARY": str(uv),
                    "QUALITY_UV_LOG": str(uv_log),
                    "QUALITY_UV_READY": str(uv_ready),
                    "XDG_CACHE_HOME": str(temp / "cache"),
                }
            )

            result = self.run_launcher(
                ["--root", ".", "--coverage", "--no-install"], environment
            )

            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertFalse(network_log.exists())
            self.assertEqual(
                python_log.read_text(encoding="utf-8").splitlines(),
                [str(LOOP), "--root", ".", "--coverage", "--no-install"],
            )
            uv_command = uv_log.read_text(encoding="utf-8")
            self.assertIn("python find", uv_command)
            self.assertIn("python install", uv_command)
            self.assertIn("--no-project", uv_command)
            self.assertIn("--no-config", uv_command)
            self.assertIn("--no-python-downloads", uv_command)
            self.assertNotIn(" sync", uv_command)
            self.assertIn(
                f"python-bin={temp / 'cache' / 'code-discipline' / 'python-bin'}",
                uv_command,
            )


if __name__ == "__main__":
    unittest.main()
