import contextlib
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import gate_secrets  # noqa: E402
import gate_types  # noqa: E402

# Every fake credential is assembled at runtime so this file never contains a
# literal token that a push-protection scanner, or this gate, would flag.
AWS_KEY = "AK" + "IA" + "Q" * 16
GITHUB_TOKEN = "gh" + "p_" + "a1" * 18
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
KEY_MATERIAL = "MII" + "Ev" * 30 + "Q="
FAKE_TOKENS = {
    "private key": PEM_HEADER + "\\n" + KEY_MATERIAL + "\\n",
    "AWS access key": AWS_KEY,
    "GitHub token": GITHUB_TOKEN,
    "GitHub fine-grained token": "github" + "_pat_" + "B" * 22,
    "GitLab token": "gl" + "pat-" + "x_y-" * 5,
    "Slack token": "xo" + "xb-" + "1234-abcdef",
    "Slack app token": "xa" + "pp-1-" + "A1B2C3D4E5",
    "Slack webhook": "https://hooks.slack" + ".com/services/T0ABC/B0DEF/" + "x" * 12,
    "Stripe live key": "rk" + "_live_" + "c" * 16,
    "Anthropic API key": "sk" + "-ant-" + "api03_" + "d" * 20,
    "OpenAI project key": "sk" + "-proj-" + "e" * 20,
    "OpenAI API key": "sk" + "-" + "f" * 20 + "T3Blbk" + "FJ" + "g" * 20,
    "Google API key": "AI" + "za" + "h" * 35,
    "npm token": "np" + "m_" + "i" * 36,
    "PyPI token": "py" + "pi-AgEIcHlwaS5vcmc" + "j" * 50,
    "Hugging Face token": "h" + "f_" + "k" * 34,
    "SendGrid API key": "S" + "G." + "l" * 22 + "." + "m" * 43,
}
TWILIO_SID = "S" + "K" + "0123456789abcdef" * 2


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


def outcome_text(outcome: gate_types.GateOutcome) -> str:
    parts = [outcome.summary, *outcome.details, repr(outcome.items)]
    parts.append(repr(outcome.measurements))
    return "\n".join(parts)


def located(outcome: gate_types.GateOutcome) -> list[tuple[str, int | None, str]]:
    return sorted((item.path, item.line, item.name) for item in outcome.items)


class ScanTextTests(unittest.TestCase):
    def test_every_pattern_reports_its_kind_and_line(self):
        for kind, token in FAKE_TOKENS.items():
            with self.subTest(kind=kind):
                text = f"first line\nvalue = '{token}'\n"
                self.assertEqual(gate_secrets.scan_text(text), [(kind, 2)])

    def test_private_key_variants_with_key_material_match(self):
        for label in ("", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ", "PGP "):
            with self.subTest(label=label):
                header = "-----BEGIN " + label + "PRIVATE" + " KEY-----"
                text = f"{header}\n{KEY_MATERIAL}\n"
                self.assertEqual(gate_secrets.scan_text(text), [("private key", 1)])
        block = "-----BEGIN PGP " + "PRIVATE" + " KEY BLOCK-----"
        text = f"{block}\nVersion: GnuPG v1\nComment: demo\n\n{KEY_MATERIAL}\n"
        self.assertEqual(gate_secrets.scan_text(text), [("private key", 1)])

    def test_private_key_layouts_seen_in_config_files(self):
        layouts = {
            "yaml block": f"tls:\n  key: |\n    {PEM_HEADER}\n    {KEY_MATERIAL}\n",
            "legacy encrypted": (
                f"x\n\n{PEM_HEADER}\nProc-Type: 4,ENCRYPTED\n"
                f"DEK-Info: AES-128-CBC,AB12\n\n{KEY_MATERIAL}\n"
            ),
            "concatenated strings": f'k = (\n"{PEM_HEADER}\\n"\n"{KEY_MATERIAL}\\n" +\n',
            "env value": f'KEY="{PEM_HEADER}\\r\\n{KEY_MATERIAL}"',
        }
        expected_lines = {
            "yaml block": 3,
            "legacy encrypted": 3,
            "concatenated strings": 2,
            "env value": 1,
        }
        for name, text in layouts.items():
            with self.subTest(layout=name):
                self.assertEqual(
                    gate_secrets.scan_text(text),
                    [("private key", expected_lines[name])],
                )

    def test_pem_header_without_key_material_is_not_a_secret(self):
        harmless = [
            f'PKCS8_HEADER = "{PEM_HEADER}"\nOTHER = 1\n',
            f'if strings.HasPrefix(s, "{PEM_HEADER}") {{\n\treturn parse(s)\n}}\n',
            f"The file must start with `{PEM_HEADER}`.\n",
            f'"{PEM_HEADER} in /home/user/projects/someproject/keys/production/x"',
            f"{PEM_HEADER}\n1\n2\n3\n4\n5\n{KEY_MATERIAL}\n",
            f"{PEM_HEADER}\n{'M' * 39}\n",
        ]
        for text in harmless:
            with self.subTest(text=text[:30]):
                self.assertEqual(gate_secrets.scan_text(text), [])

    def test_two_headers_before_one_body_report_both(self):
        text = f"{PEM_HEADER}\n{PEM_HEADER}\n{KEY_MATERIAL}\n"
        self.assertEqual(
            gate_secrets.scan_text(text), [("private key", 1), ("private key", 2)]
        )

    def test_tokens_inside_longer_identifiers_do_not_match(self):
        embedded = [
            "x" + AWS_KEY,
            AWS_KEY + "Z",
            "Z" + GITHUB_TOKEN,
            "AI" + "za" + "h" * 36,
        ]
        for text in embedded:
            with self.subTest(text=text[:6]):
                self.assertEqual(gate_secrets.scan_text(text), [])

    def test_too_short_and_test_mode_values_do_not_match(self):
        harmless = [
            "AK" + "IA" + "Q" * 15,
            "gh" + "p_" + "a" * 35,
            "sk" + "_test_" + "c" * 20,
            "xo" + "xb-" + "short",
            "placeholder = 'changeme'",
        ]
        for text in harmless:
            with self.subTest(text=text[:6]):
                self.assertEqual(gate_secrets.scan_text(text), [])

    def test_documentation_placeholders_do_not_match(self):
        examples = [
            "aws_access_key_id = " + "AK" + "IA" + "IOSFODNN7" + "EXAMPLE",
            "AWS_ACCESS_KEY_ID=" + "AS" + "IA" + "I44QH8DHB" + "EXAMPLE",
        ]
        for text in examples:
            with self.subTest(text=text[:12]):
                self.assertEqual(gate_secrets.scan_text(text), [])
        mixed = examples[0] + " real=" + AWS_KEY
        self.assertEqual(gate_secrets.scan_text(mixed), [("AWS access key", 1)])

    def test_xoxo_in_prose_and_urls_is_not_a_slack_token(self):
        slug = 'url = "https://example.com/blog/' + "xo" + 'xo-love-letters-2024"'
        self.assertEqual(gate_secrets.scan_text(slug), [])

    def test_twilio_key_needs_twilio_on_the_same_line(self):
        self.assertEqual(gate_secrets.scan_text(f"key = '{TWILIO_SID}'"), [])
        self.assertEqual(
            gate_secrets.scan_text(f"TWILIO_KEY = '{TWILIO_SID}'"),
            [(gate_secrets.TWILIO_KIND, 1)],
        )
        self.assertEqual(gate_secrets.scan_text(f"twilio\nkey = '{TWILIO_SID}'"), [])

    def test_one_line_with_two_kinds_reports_both(self):
        line = f"{GITHUB_TOKEN} {AWS_KEY}"
        self.assertEqual(
            gate_secrets.scan_text(line),
            [("AWS access key", 1), ("GitHub token", 1)],
        )

    def test_results_hold_kinds_and_lines_only(self):
        findings = gate_secrets.scan_text(f"a\nb\nkey={AWS_KEY}\n")
        self.assertEqual(findings, [("AWS access key", 3)])
        self.assertNotIn(AWS_KEY, repr(findings))

    def test_line_numbers_count_newlines_only(self):
        for separator in ("\f", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", " "):
            with self.subTest(separator=repr(separator)):
                text = f"a = 1\n# section{separator}\nkey = '{AWS_KEY}'\n"
                self.assertEqual(gate_secrets.scan_text(text), [("AWS access key", 3)])
        crlf = f"a = 1\r\n\r\nkey = '{AWS_KEY}'\r\n"
        self.assertEqual(gate_secrets.scan_text(crlf), [("AWS access key", 3)])


class ScanFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "data"

    def tearDown(self):
        self.directory.cleanup()

    def scan(self, data: bytes) -> list[tuple[str, int]]:
        self.path.write_bytes(data)
        return gate_secrets.scan_file(self.path)

    def test_encodings_and_binary_content_are_scanned(self):
        text = f"a = 1\nkey = '{AWS_KEY}'\n"
        samples = {
            "utf-8": text.encode("utf-8"),
            "utf-16 with BOM": text.encode("utf-16"),
            "utf-16-le": text.encode("utf-16-le"),
            "utf-16-be": text.encode("utf-16-be"),
            "utf-16 crlf": text.replace("\n", "\r\n").encode("utf-16"),
            "utf-32": text.encode("utf-32"),
            "leading NUL": b"\0" + text.encode(),
            "binary noise": b"\x89PNG\x1a\xff\xfe\0\0" + text.encode(),
        }
        for name, data in samples.items():
            with self.subTest(encoding=name):
                self.assertEqual(self.scan(data), [("AWS access key", 2)])

    def test_files_larger_than_the_text_limit_are_scanned(self):
        padding = b"-- pad\n" * 400_000
        data = padding + f"key = '{AWS_KEY}'\n".encode()
        self.assertGreater(len(data), 2_000_000)
        self.assertEqual(self.scan(data), [("AWS access key", 400_001)])

    def test_form_feed_does_not_shift_file_line_numbers(self):
        data = f"a = 1\n\f\nb = 2\nkey = '{AWS_KEY}'\n".encode()
        self.assertEqual(self.scan(data), [("AWS access key", 4)])

    def test_this_gate_and_its_tests_contain_no_secrets(self):
        module = SCRIPTS / "gate_secrets.py"
        self.assertEqual(gate_secrets.scan_file(module), [])
        self.assertEqual(gate_secrets.scan_file(Path(__file__)), [])

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            gate_secrets.scan_file(self.path)


class EvaluateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def evaluate(self) -> gate_types.GateOutcome:
        return gate_secrets.evaluate_secrets(self.root, diff_scope.repository_changes())

    def test_clean_repository_passes(self):
        self.write("app.py", "print('hello')\n")
        self.write("README.md", "# Demo\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(outcome.key, "secrets")
        self.assertEqual(outcome.title, "Secrets")
        self.assertEqual(outcome.items, [])
        self.assertEqual(outcome.measurements, {"files_scanned": 2, "findings": 0})
        self.assertIn("No high-confidence secrets in 2 files", outcome.summary)

    def test_secrets_in_any_file_fail_without_exposing_values(self):
        self.write(".env", f"AWS_ACCESS_KEY_ID={AWS_KEY}\n")
        self.write("config/ci.yaml", f"env:\n  token: {GITHUB_TOKEN}\n")
        self.write("tests/test_api.py", f"KEY = '{AWS_KEY}'\n")
        self.write("docs/setup.md", "Nothing secret here.\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            located(outcome),
            [
                (".env", 1, "AWS access key"),
                ("config/ci.yaml", 2, "GitHub token"),
                ("tests/test_api.py", 1, "AWS access key"),
            ],
        )
        self.assertEqual(outcome.measurements["files_scanned"], 4)
        self.assertEqual(outcome.measurements["findings"], 3)
        self.assertEqual(outcome.measurements["kind:AWS access key"], 2)
        self.assertEqual(outcome.measurements["kind:GitHub token"], 1)
        self.assertIn("High-confidence secrets found: 3", outcome.summary)
        self.assertIn(
            ".env:1 AWS access key: secret found (limit none)", outcome.details
        )
        rendered = outcome_text(outcome)
        self.assertNotIn(AWS_KEY, rendered)
        self.assertNotIn(GITHUB_TOKEN, rendered)

    def test_files_named_like_this_gate_are_scanned(self):
        self.write("app/gate_secrets.py", f"PATTERN = '{AWS_KEY}'\n")
        self.write("src/test_gate_secrets.py", f"TOKEN = '{AWS_KEY}'\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items],
            ["app/gate_secrets.py", "src/test_gate_secrets.py"],
        )

    def test_binary_utf16_and_large_files_are_scanned(self):
        line = f"KEY = '{AWS_KEY}'\n"
        (self.root / "deploy.ps1").write_bytes(line.encode("utf-16"))
        (self.root / "creds.ini").write_bytes(b"\0" + line.encode())
        (self.root / "seed.sql").write_text(line + "-- pad\n" * 300_000)
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items],
            ["creds.ini", "deploy.ps1", "seed.sql"],
        )

    def test_unreadable_file_blocks_a_clean_result(self):
        self.write("app.py", "print('hello')\n")
        self.write("locked.txt", "anything\n")
        with refuse_to_open(self.root / "locked.txt"):
            outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.BLOCKED)
        self.assertIn("locked.txt: could not be read", outcome.details)
        self.assertIn("1 file could not be read", outcome.summary)
        self.assertEqual(outcome.measurements["files_unreadable"], 1)

    def test_unreadable_file_keeps_a_failure_failing(self):
        self.write(".env", f"KEY={AWS_KEY}\n")
        self.write("locked.txt", "anything\n")
        with refuse_to_open(self.root / "locked.txt"):
            outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertIn("locked.txt: could not be read", outcome.details)
        self.assertIn("High-confidence secrets found: 1", outcome.summary)

    def test_without_git_third_party_folders_are_skipped(self):
        self.write("node_modules/pkg/fixture.js", f"k = '{AWS_KEY}'\n")
        self.write("app.py", "VALUE = 1\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(outcome.measurements["files_scanned"], 1)

    def test_broken_git_directory_falls_back_to_a_directory_walk(self):
        (self.root / ".git").mkdir()
        self.write("app.py", f"KEY = '{AWS_KEY}'\n")
        outcome = self.evaluate()
        self.assertEqual(located(outcome), [("app.py", 1, "AWS access key")])


@contextlib.contextmanager
def refuse_to_open(locked: Path) -> Iterator[None]:
    """Make ``Path.open`` raise PermissionError for one path, as a file with
    mode 000 does for a non-root user."""
    locked = locked.resolve()
    original = Path.open

    def guarded_open(path: Path, *arguments: Any, **keywords: Any) -> Any:
        if path.resolve() == locked:
            raise PermissionError(13, "Permission denied", str(path))
        return original(path, *arguments, **keywords)

    with mock.patch.object(Path, "open", guarded_open):
        yield


class GitRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        make_repository(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit_all(self) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")

    def evaluate(self, changes: diff_scope.ChangeSet) -> gate_types.GateOutcome:
        return gate_secrets.evaluate_secrets(self.root, changes)

    def test_tracked_files_in_build_and_editor_folders_are_scanned(self):
        line = f"TOKEN = '{AWS_KEY}'\n"
        folders = [".vscode", "build", "out", "dist", "vendor", "target", "obj"]
        folders += ["coverage", ".quality", "node_modules"]
        for folder in folders:
            self.write(f"{folder}/config.txt", line)
        self.commit_all()
        outcome = self.evaluate(diff_scope.repository_changes())
        self.assertEqual(
            sorted(item.path for item in outcome.items),
            sorted(f"{folder}/config.txt" for folder in folders),
        )

    def test_untracked_files_follow_gitignore_and_skip_dependency_installs(self):
        line = f"TOKEN = '{AWS_KEY}'\n"
        self.write(".gitignore", "secrets.local\n")
        self.write("secrets.local", line)
        self.write(".vscode/launch.json", line)
        self.write("build/deploy.sh", line)
        self.write("node_modules/pkg/fixture.js", line)
        self.write(".venv/lib/site.py", line)
        outcome = self.evaluate(diff_scope.repository_changes())
        self.assertEqual(
            sorted(item.path for item in outcome.items),
            [".vscode/launch.json", "build/deploy.sh"],
        )
        self.assertEqual(outcome.measurements["files_scanned"], 3)

    def test_deleted_tracked_file_is_not_listed(self):
        self.write("gone.txt", "x\n")
        self.write("kept.txt", "y\n")
        self.commit_all()
        (self.root / "gone.txt").unlink()
        outcome = self.evaluate(diff_scope.repository_changes())
        self.assertEqual(outcome.measurements["files_scanned"], 1)

    def test_git_without_executable_falls_back_to_a_directory_walk(self):
        self.write("build/deploy.sh", f"TOKEN = '{AWS_KEY}'\n")
        self.write("app.py", "VALUE = 1\n")
        with mock.patch.object(gate_secrets.shutil, "which", return_value=None):
            paths = gate_secrets.publishable_files(
                self.root, diff_scope.repository_changes()
            )
        self.assertEqual([path.name for path in paths], ["app.py"])

    def test_touched_file_with_old_secret_fails_and_untouched_file_is_skipped(self):
        self.write("touched.py", f"KEY = '{AWS_KEY}'\nVALUE = 1\n")
        self.write("untouched.py", f"TOKEN = '{GITHUB_TOKEN}'\n")
        self.commit_all()
        self.write("touched.py", f"KEY = '{AWS_KEY}'\nVALUE = 2\n")
        outcome = self.evaluate(diff_scope.local_changes(self.root))
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [(item.path, item.line) for item in outcome.items], [("touched.py", 1)]
        )
        self.assertEqual(outcome.measurements["files_scanned"], 1)

    def test_change_without_secrets_passes(self):
        self.write("app.py", "VALUE = 1\n")
        self.write("old.py", f"KEY = '{AWS_KEY}'\n")
        self.commit_all()
        self.write("app.py", "VALUE = 2\n")
        self.write("new.yaml", "name: demo\n")
        outcome = self.evaluate(diff_scope.local_changes(self.root))
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(outcome.measurements["files_scanned"], 2)


class ScanFilesTests(unittest.TestCase):
    def test_reports_relative_paths_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text(f"x\n{GITHUB_TOKEN}\n", encoding="utf-8")
            items, unreadable = gate_secrets.scan_files(
                root, [root / "a.txt", root / "missing.txt"]
            )
        self.assertEqual(unreadable, ["missing.txt"])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(
            (item.path, item.line, item.name), ("a.txt", 2, "GitHub token")
        )
        self.assertEqual(
            item.describe(), "a.txt:2 GitHub token: secret found (limit none)"
        )


if __name__ == "__main__":
    unittest.main()
