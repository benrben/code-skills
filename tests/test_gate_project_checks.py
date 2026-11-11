import json
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_project_checks as checks  # noqa: E402
import gate_project_checks_inference as inference  # noqa: E402
import gate_project_checks_make as make  # noqa: E402
import gate_project_checks_safety as safety  # noqa: E402
import gate_project_checks_shell as shell  # noqa: E402
from gate_types import CommandRun, subprocess_runner  # noqa: E402


def write(root: Path, relative: str, text: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_executable(root: Path, relative: str) -> Path:
    path = write(root, relative, "#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def write_package(root: Path, **package: object) -> None:
    write(root, "package.json", json.dumps(package))


def which_from(*names: str):
    """A fake PATH lookup that knows only ``names``."""

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in names else None

    return which


def no_tools(name: str) -> str | None:
    return None


def commands(specs) -> list[tuple[str, ...]]:
    return [spec.command for spec in specs]


class FakeRunner:
    """Returns recorded results by command name and remembers every call."""

    def __init__(
        self,
        results: Mapping[str, tuple[int, str]] | None = None,
        *,
        timed_out: Sequence[str] = (),
    ) -> None:
        self.results = dict(results or {})
        self.timed_out = set(timed_out)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, timeout_seconds, env=None) -> CommandRun:
        command = tuple(command)
        self.calls.append(command)
        key = " ".join(command)
        if key in self.timed_out:
            return CommandRun(command, 124, "still running", 5.0, timed_out=True)
        returncode, output = self.results.get(key, (0, ""))
        return CommandRun(command, returncode, output, 0.25)


class TempRootTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class PackageScriptTests(TempRootTest):
    def test_check_scripts_run_in_fixed_order_through_npm_by_default(self):
        write_package(
            self.root,
            scripts={
                "build": "tsc",
                "test": "jest",
                "lint": "eslint .",
                "format:check": "prettier --check .",
            },
        )
        specs = inference.infer_project_checks(self.root, which=no_tools)
        self.assertEqual(
            commands(specs),
            [
                ("npm", "run", "lint"),
                ("npm", "run", "format:check"),
                ("npm", "run", "build"),
            ],
        )
        self.assertEqual({spec.source for spec in specs}, {"package.json"})
        self.assertEqual(specs[0].name, "npm run lint")

    def test_lockfile_picks_the_package_manager(self):
        for lockfile, manager in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("bun.lock", "bun"),
        ):
            with self.subTest(lockfile=lockfile):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    write_package(root, scripts={"lint": "eslint ."})
                    write(root, lockfile)
                    specs = inference.infer_project_checks(root, which=no_tools)
                    self.assertEqual(commands(specs), [(manager, "run", "lint")])

    def test_unreadable_or_odd_package_json_declares_nothing(self):
        for text in ("{not json", "[1, 2]", json.dumps({"scripts": ["lint"]})):
            with self.subTest(text=text):
                write(self.root, "package.json", text)
                self.assertEqual(inference.package_script_checks(self.root), [])


class TypeScriptTests(TempRootTest):
    def install_typescript(self, tsconfig: str) -> Path:
        write_package(self.root, devDependencies={"typescript": "5.6.0"})
        write(self.root, "tsconfig.json", tsconfig)
        return write_executable(self.root, "node_modules/.bin/tsc")

    def test_typescript_dependency_runs_local_tsc_without_emitting(self):
        tsc = self.install_typescript('{"compilerOptions": {"strict": true}}')
        specs = inference.infer_project_checks(self.root, which=no_tools)
        self.assertEqual(
            commands(specs), [(str(tsc), "--noEmit", "-p", "tsconfig.json")]
        )
        self.assertEqual(specs[0].source, "tsconfig.json")

    def test_solution_tsconfig_checks_each_reference(self):
        tsc = self.install_typescript(
            """{
              // solution-style config
              "files": [],
              "references": [
                {"path": "./packages/app"}, /* the app */
                {"path": "./packages/lib"},
                {"prepend": true},
              ],
            }"""
        )
        specs = inference.typescript_checks(self.root)
        self.assertEqual(
            commands(specs),
            [
                (str(tsc), "--noEmit", "-p", "./packages/app"),
                (str(tsc), "--noEmit", "-p", "./packages/lib"),
            ],
        )

    def test_a_declared_type_script_replaces_tsc(self):
        self.install_typescript("{}")
        write_package(
            self.root,
            devDependencies={"typescript": "5.6.0"},
            scripts={"typecheck": "tsc -b"},
        )
        specs = inference.infer_project_checks(self.root, which=no_tools)
        self.assertEqual(commands(specs), [("npm", "run", "typecheck")])

    def test_tsc_is_omitted_when_not_installed_or_not_configured(self):
        write_package(self.root, dependencies={"typescript": "5.6.0"})
        write(self.root, "tsconfig.json", "{}")
        self.assertEqual(inference.typescript_checks(self.root), [])
        (self.root / "tsconfig.json").unlink()
        write_executable(self.root, "node_modules/.bin/tsc")
        self.assertEqual(inference.typescript_checks(self.root), [])

    def test_tsc_needs_a_typescript_dependency(self):
        write_package(self.root, dependencies={"react": "18"})
        write(self.root, "tsconfig.json", "{}")
        write_executable(self.root, "node_modules/.bin/tsc")
        self.assertEqual(inference.typescript_checks(self.root), [])

    def test_jsonc_keeps_comment_like_text_inside_strings(self):
        value = inference.load_jsonc('{"url": "http://x/*y*/", "a": [1,],}')
        self.assertEqual(value, {"url": "http://x/*y*/", "a": [1]})
        self.assertEqual(inference.load_jsonc("[1]"), {})
        self.assertEqual(inference.load_jsonc("{broken"), {})


class PythonTests(TempRootTest):
    def test_configured_ruff_and_mypy_from_the_project_env(self):
        write(
            self.root,
            "pyproject.toml",
            "[tool.ruff]\nline-length = 88\n\n[tool.mypy]\nstrict = true\n",
        )
        ruff = write_executable(self.root, ".venv/bin/ruff")
        mypy = write_executable(self.root, ".venv/bin/mypy")
        specs = inference.infer_project_checks(self.root, which=no_tools)
        self.assertEqual(
            commands(specs),
            [
                (str(ruff), "check", "."),
                (str(ruff), "format", "--check", "."),
                (str(mypy), "."),
            ],
        )
        self.assertEqual({spec.source for spec in specs}, {"pyproject.toml"})

    def test_mypy_runs_without_arguments_when_config_lists_files(self):
        write(self.root, "mypy.ini", "[mypy]\nfiles = src\n\n[mypy-x.*]\n")
        mypy = write_executable(self.root, "venv/bin/mypy")
        specs = inference.python_checks(self.root, no_tools)
        self.assertEqual(commands(specs), [(str(mypy),)])
        self.assertEqual(specs[0].source, "mypy.ini")

    def test_files_in_a_later_section_does_not_count(self):
        write(self.root, "setup.cfg", "[mypy]\nstrict = True\n[other]\nfiles = x\n")
        mypy = write_executable(self.root, ".venv/bin/mypy")
        specs = inference.python_checks(self.root, no_tools)
        self.assertEqual(commands(specs), [(str(mypy), ".")])
        self.assertEqual(specs[0].source, "setup.cfg")

    def test_mypy_on_path_only_is_not_the_project_env(self):
        write(self.root, "pyproject.toml", "[tool.mypy]\nstrict = true\n")
        self.assertEqual(inference.python_checks(self.root, which_from("mypy")), [])

    def test_ruff_toml_with_ruff_on_path(self):
        write(self.root, "ruff.toml", "line-length = 100\n")
        specs = inference.python_checks(self.root, which_from("ruff"))
        self.assertEqual(
            commands(specs),
            [
                ("/usr/bin/ruff", "check", "."),
                ("/usr/bin/ruff", "format", "--check", "."),
            ],
        )
        self.assertEqual(specs[0].source, "ruff.toml")

    def test_ruff_needs_both_config_and_executable(self):
        write(self.root, "pyproject.toml", "[project]\nname = 'x'\n")
        self.assertEqual(inference.python_checks(self.root, which_from("ruff")), [])
        write(self.root, ".ruff.toml", "")
        self.assertEqual(inference.python_checks(self.root, no_tools), [])

    def test_non_executable_env_file_is_ignored(self):
        write(self.root, "ruff.toml", "")
        write(self.root, ".venv/bin/ruff", "not executable")
        self.assertEqual(inference.python_checks(self.root, no_tools), [])


class ToolchainTests(TempRootTest):
    def test_go_vet_and_gofmt_listing(self):
        write(self.root, "go.mod", "module example.com/x\n")
        write(self.root, "main.go", "package main\n")
        write(self.root, "pkg/a_test.go", "package pkg\n")
        specs = inference.infer_project_checks(
            self.root, which=which_from("go", "gofmt")
        )
        self.assertEqual(
            commands(specs),
            [
                ("/usr/bin/go", "vet", "./..."),
                ("/usr/bin/gofmt", "-l", "main.go", "pkg/a_test.go"),
            ],
        )
        self.assertEqual([spec.fail_on_output for spec in specs], [False, True])

    def test_gofmt_skips_what_the_go_tool_skips(self):
        """Regression: ``gofmt -l .`` walked vendor/ and testdata/."""
        write(self.root, "go.mod", "module x\n")
        for path in (
            "cmd/app/main.go",
            "vendor/dep/old.go",
            "pkg/testdata/broken.go",
            "_tools/tools.go",
            ".cache/x.go",
            "pkg/_skip.go",
            "nested/go.mod",
            "nested/n.go",
            "pkg/README.md",
        ):
            write(self.root, path, "package x\n")
        self.assertEqual(inference.go_source_files(self.root), ["cmd/app/main.go"])

    def test_gofmt_runs_in_batches_with_a_short_display(self):
        write(self.root, "go.mod", "module x\n")
        for name in "abc":
            write(self.root, f"{name}.go", "package x\n")
        with mock.patch.object(inference, "GOFMT_FILES_PER_COMMAND", 2):
            specs = inference.go_checks(self.root, which_from("go", "gofmt"))
        self.assertEqual(
            [spec.name for spec in specs],
            ["go vet", "gofmt (1 of 2)", "gofmt (2 of 2)"],
        )
        self.assertEqual(specs[2].command, ("/usr/bin/gofmt", "-l", "c.go"))
        many = inference.CheckSpec("gofmt", ("gofmt", "-l", *map(str, range(20))), "x")
        self.assertEqual(many.display(), "gofmt -l 0 1 2 3 4 5 6 ... (+13 more)")

    def test_go_without_gofmt_or_without_go(self):
        write(self.root, "go.mod", "module x\n")
        self.assertEqual(
            commands(inference.go_checks(self.root, which_from("go"))),
            [("/usr/bin/go", "vet", "./...")],
        )
        self.assertEqual(inference.go_checks(self.root, which_from("gofmt")), [])

    def test_rust_check_is_offline_and_fmt_needs_rustfmt(self):
        write(self.root, "Cargo.toml", "[package]\nname = 'x'\n")
        cargo = "/usr/bin/cargo"
        self.assertEqual(
            commands(inference.rust_checks(self.root, which_from("cargo"))),
            [(cargo, "check", "--all-targets", "--offline")],
        )
        write(self.root, "Cargo.lock", "version = 3\n")
        self.assertEqual(
            commands(inference.rust_checks(self.root, which_from("cargo", "rustfmt"))),
            [
                (cargo, "check", "--all-targets", "--frozen", "--offline"),
                (cargo, "fmt", "--check"),
            ],
        )
        self.assertEqual(inference.rust_checks(self.root, no_tools), [])

    def test_dotnet_prefers_solutions_over_projects(self):
        write(self.root, "App.csproj")
        dotnet = which_from("dotnet")
        self.assertEqual(
            commands(inference.dotnet_checks(self.root, dotnet)),
            [("/usr/bin/dotnet", "build", "App.csproj", "--no-restore")],
        )
        write(self.root, "App.sln")
        specs = inference.dotnet_checks(self.root, dotnet)
        self.assertEqual(
            commands(specs), [("/usr/bin/dotnet", "build", "App.sln", "--no-restore")]
        )
        self.assertEqual(specs[0].source, "App.sln")
        self.assertEqual(inference.dotnet_checks(self.root, no_tools), [])

    def test_swift_build(self):
        write(self.root, "Package.swift", "// swift-tools-version:5.9\n")
        self.assertEqual(
            commands(inference.swift_checks(self.root, which_from("swift"))),
            [("/usr/bin/swift", "build")],
        )
        self.assertEqual(inference.swift_checks(self.root, no_tools), [])

    def test_makefile_check_targets(self):
        write(
            self.root,
            "Makefile",
            "build:\n\tcc x.c\nlint:\n\truff check .\ncheck: lint\n\ttrue\n"
            "lint:\n\techo again\n",
        )
        specs = inference.infer_project_checks(self.root, which=no_tools)
        self.assertEqual(commands(specs), [("make", "lint"), ("make", "check")])
        self.assertEqual(specs[0].source, "Makefile")
        self.assertEqual(
            commands(inference.make_checks(self.root, which_from("make"))),
            [("/usr/bin/make", "lint"), ("/usr/bin/make", "check")],
        )

    def test_empty_repository_declares_nothing(self):
        self.assertEqual(
            inference.infer_project_checks(self.root, which=which_from("go", "cargo")),
            [],
        )


class ConfiguredCommandTests(TempRootTest):
    def test_missing_or_empty_commands_leave_inference_on(self):
        for section in ({}, {"commands": []}, {"commands": ""}, {"commands": None}):
            with self.subTest(section=section):
                self.assertIsNone(
                    inference.configured_project_checks(section, self.root)
                )

    def test_single_command_as_text(self):
        specs = inference.configured_project_checks(
            {"commands": "make lint"}, self.root
        )
        self.assertEqual(commands(specs), [("make", "lint")])
        self.assertEqual(specs[0].source, ".quality/quality-gate.json")

    def test_list_of_commands_in_every_form(self):
        value = [
            ["npm", "run", "lint"],
            [".venv/bin/python", "-m", "mypy", "{root}/src", 3],
            {"name": "format", "command": "gofmt -l .", "fail_on_output": True},
            {"command": ["npm", "run", "lint"]},
            ["ruff", "check", "src/my dir"],
        ]
        specs = inference.configured_project_checks({"commands": value}, self.root)
        self.assertEqual(
            commands(specs),
            [
                ("npm", "run", "lint"),
                (".venv/bin/python", "-m", "mypy", f"{self.root}/src", "3"),
                ("gofmt", "-l", "."),
                ("ruff", "check", "src/my dir"),
            ],
        )
        self.assertEqual(specs[2].name, "format")
        self.assertTrue(specs[2].fail_on_output)
        self.assertEqual(specs[0].name, "npm run lint")

    def test_bare_string_lists_are_refused_as_ambiguous(self):
        """Regression: ``["mypy", "pyright"]`` and ``["ruff", "check", "src/my
        dir"]`` were guessed into the wrong commands."""
        for value in (
            ["mypy", "pyright"],
            ["ruff", "check", "src/my dir"],
            ["npm run lint"],
            [["tsc"], "npm run lint"],
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "argument list"):
                    inference.configured_project_checks({"commands": value}, self.root)

    def test_configured_gofmt_listing_fails_on_output_by_default(self):
        """Regression: ``"gofmt -l ."`` in the config could never fail."""
        cases = (
            ("gofmt -l .", True),
            ([["/go/bin/goimports", "-l", "."]], True),
            ([{"command": "gofmt -l ."}], True),
            ([{"command": "gofmt -l .", "fail_on_output": False}], False),
            ("gofmt -d .", False),
            ("go vet ./...", False),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                specs = inference.configured_project_checks(
                    {"commands": value}, self.root
                )
                self.assertEqual(specs[0].fail_on_output, expected)

    def test_dict_without_name_uses_the_command(self):
        specs = inference.configured_project_checks(
            {"commands": [{"command": ["tsc", "--noEmit"]}]}, self.root
        )
        self.assertEqual(specs[0].name, "tsc --noEmit")
        self.assertFalse(specs[0].fail_on_output)

    def test_invalid_commands_are_rejected(self):
        for value in (42, [{"command": {"x": 1}}], "  ", [[]], [3]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    inference.configured_project_checks({"commands": value}, self.root)

    def test_configured_commands_replace_inference(self):
        write_package(self.root, scripts={"lint": "eslint ."})
        specs = checks.project_check_specs(self.root, {"commands": "make check"})
        self.assertEqual(commands(specs), [("make", "check")])
        inferred = checks.project_check_specs(self.root, {}, which=no_tools)
        self.assertEqual(commands(inferred), [("npm", "run", "lint")])


def assert_problems(test: unittest.TestCase, cases: Mapping[str, str]) -> None:
    for text, marker in cases.items():
        with test.subTest(text=text):
            problem = checks.command_problem(text)
            test.assertIsNotNone(problem)
            test.assertIn(marker, problem or "")


def assert_safe(test: unittest.TestCase, texts: Sequence[str]) -> None:
    for text in texts:
        with test.subTest(text=text):
            test.assertIsNone(checks.command_problem(text))


class WritingCommandTests(unittest.TestCase):
    def test_commands_that_write_files_are_refused(self):
        assert_problems(
            self,
            {
                "eslint . --fix": "--fix modifies files",
                "eslint --fix-dry-run .": "--fix-dry-run hides fixable problems",
                "eslint --fix=true .": "--fix",
                "prettier --write .": "--write",
                "prettier -w src": "prettier -w",
                "gofmt -w .": "gofmt -w",
                "ruff check . && ruff format .": "ruff format",
                "black src": "black",
                "isort .": "isort",
                "cargo fmt": "cargo fmt",
                "go fmt ./...": "go fmt",
            },
        )

    def test_writing_flags_with_values_or_suffixes(self):
        """Regression: ``--fix-only`` and ``--write=true`` were missed."""
        assert_problems(
            self,
            {
                "ruff check --fix-only .": "--fix-only",
                "prettier --write=true .": "--write",
                "biome format --write=true .": "--write",
            },
        )

    def test_writers_beyond_fix_and_write_flags(self):
        """Regression: these rewrote the working tree and then passed."""
        assert_problems(
            self,
            {
                "biome check --apply .": "biome --apply",
                "biome check --apply-unsafe .": "--apply-unsafe",
                "rubocop -A": "rubocop -A",
                "bundle exec rubocop --autocorrect": "--autocorrect",
                "clang-format -i src/*.c": "clang-format -i",
                "dprint fmt": "dprint fmt",
                "rustfmt src/lib.rs": "rustfmt",
                "autopep8 --in-place x.py": "autopep8 --in-place",
                "yapf -i x.py": "yapf -i",
                "php-cs-fixer fix src": "php-cs-fixer fix",
                "ktlint -F": "ktlint -F",
                "sqlfluff fix .": "sqlfluff fix",
                "terraform fmt": "terraform fmt",
                "mix format": "mix format",
                "dotnet format": "dotnet format",
                "swiftformat .": "swiftformat",
            },
        )

    def test_wrappers_do_not_hide_the_tool(self):
        assert_problems(
            self,
            {
                "python3.12 -m black src": "black",
                ".venv/bin/python -m isort .": "isort",
                "poetry run black .": "black",
                "uv run ruff format .": "ruff format",
                "npx --yes prettier -w .": "prettier -w",
                "pnpm exec prettier -w .": "prettier -w",
                "cross-env CI=1 gofmt -w .": "gofmt -w",
                "env FOO=1 black .": "black",
                "sh -c 'black .'": "black",
                "bash -ec 'black .'": "black",
                "find . -name '*.c' | xargs clang-format -i": "clang-format -i",
                "C:/tools/black.exe src": "black",
            },
        )

    def test_check_only_commands_are_safe(self):
        assert_safe(
            self,
            (
                "eslint . --max-warnings=0",
                "prettier --check .",
                "ruff format --check . && ruff check .",
                "ruff check --no-fix .",
                "black --check src",
                "isort --check-only .",
                "isort -c .",
                "cargo fmt --check",
                "gofmt -l .",
                "tsc --noEmit -p tsconfig.json",
                "grep -w TODO src",
                "eslint --fix-type problem .",
                "eslint -c black.json .",
                "stylelint --config ./config/black.js",
                "rubocop",
                "terraform fmt -check",
                "dotnet format --verify-no-changes",
                "echo run black --fix later",
                "sh script.sh",
                "sh -c",
                "bash -x",
                "biome ci .",
            ),
        )


class ExitStatusTests(unittest.TestCase):
    def test_commands_whose_failure_cannot_fail_the_check(self):
        """Regression: these exited 0 although the check failed."""
        assert_problems(
            self,
            {
                "eslint . || true": "|| true hides its failure",
                "eslint . ||true": "|| true",
                "tsc || exit 0": "|| exit 0",
                "tsc || :": "|| :",
                "tsc; exit 0": "a later command decides",
                "eslint . ; true": "a later command decides",
                "eslint .\nexit 0": "a later command decides",
                "eslint . | cat": "| keeps only the last",
                "eslint . | tee lint.log": "| keeps only the last",
                "eslint . || echo lint failed": "|| echo lint failed",
                "eslint . & ": "& runs it in the background",
                "eslint . & wait": "background",
                "eslint . || true && tsc": "'eslint .'",
                "(eslint . || true) && tsc": "'eslint .'",
                "{ eslint .; tsc; }": "'eslint .'",
                "set -e; eslint . && prettier --check .; tsc": "'eslint .'",
                "for d in a b; do eslint $d; done": "a loop keeps only",
                "sh -c 'eslint . || true'": "|| true",
                "eslint . | tee log\nset -o pipefail": "| keeps only",
                "a_very_long_command_name_that_goes_on_and_on --x || true": "...",
                "( ) || true; ( eslint . ) || true": "'eslint .'",
                "eslint . || ( echo x )": "|| echo x hides",
                "eslint . || ( )": "|| ( ) hides",
                "eslint . || X=1": "|| X=1 hides",
            },
        )

    def test_housekeeping_and_guarded_failures_are_safe(self):
        """Regression: ``rm -rf dist || true`` before the real check failed
        a legitimate build."""
        assert_safe(
            self,
            (
                "rm -rf dist || true",
                "rm -rf dist || true && tsc",
                "rimraf dist; tsc",
                "echo Linting; eslint .",
                "eslint . && echo done",
                "eslint . || { echo failed; exit 1; }",
                "eslint . || exit",
                "eslint . || exit 2; tsc",
                "eslint . || false",
                "eslint . ||",
                "set -e; eslint .; tsc",
                "set -o errexit\neslint .\ntsc",
                "set -euo pipefail\neslint . | tee log",
                "for d in a b; do eslint $d || exit 1; done",
                "if ! eslint .; then exit 1; fi",
                "if [ -d x ]; then eslint x; else tsc; fi",
                "while read f; do echo $f; done",
                "case $x in a) eslint . ;; esac",
                "find . -name '*.sh' | xargs shellcheck",
                "git ls-files | xargs eslint",
                "(cd a && eslint .) && (cd b && eslint .)",
                "NODE_ENV=x tsc -p . > out.txt 2>&1",
                "eslint . # || true",
                "echo '|| true'",
                'echo "a \\" || true"',
                "eslint $(npm bin)/x `pwd` ${HOME}",
                "eslint '",
                "X=1",
                ") eslint .",
                "eslint \\\n  .",
                "eslint . &&\n\n  tsc",
                "sh -c 'eslint .'",
                "",
            ),
        )


class ShellParsingTests(unittest.TestCase):
    def test_tokens_keep_quoted_operators_as_words(self):
        tokens = shell.tokenize("a '|| true' \"x;y\" b\;c $(d || e) 2>&1 # note")
        self.assertEqual(
            [(token.text, token.operator) for token in tokens],
            [
                ("a", False),
                ("|| true", False),
                ("x;y", False),
                ("b;c", False),
                ("$(d || e)", False),
                ("2", False),
                (">&", True),
                ("1", False),
            ],
        )

    def test_structure_of_lists_pipelines_and_groups(self):
        [first, second] = shell.parse("a | b && { c; } || (d)\ne &")
        self.assertEqual(first.operators, ["&&", "||"])
        self.assertEqual(first.terminator, "\n")
        self.assertEqual(len(first.pipelines[0]), 2)
        self.assertIsInstance(first.pipelines[1][0], shell.Group)
        self.assertEqual(second.terminator, "&")

    def test_command_words_and_names(self):
        self.assertEqual(shell.command_words(["X=1", "!", "tsc"]), ["tsc"])
        self.assertEqual(shell.command_words(["for", "d", "in", "a"]), [])
        self.assertEqual(shell.basename("C:\\bin\\ruff.exe"), "ruff")
        self.assertEqual(shell.basename("node_modules/.bin/tsc.cmd"), "tsc")


class PackageScriptInspectionTests(TempRootTest):
    def reason(self, *command: str) -> str | None:
        return checks.unsafe_reason(
            inference.CheckSpec("check", command, "package.json"), self.root
        )

    def test_package_script_text_and_the_scripts_it_calls_are_inspected(self):
        write_package(
            self.root,
            scripts={
                "lint": "eslint . && npm run lint:style",
                "lint:style": "stylelint --fix '**/*.css' && npm run lint",
                "check": "tsc --noEmit",
            },
        )
        specs = {spec.name: spec for spec in inference.package_script_checks(self.root)}
        self.assertEqual(
            checks.unsafe_reason(specs["npm run lint"], self.root),
            "check cannot fail or modifies files "
            "(--fix modifies files in package.json script 'lint:style')",
        )
        self.assertIsNone(checks.unsafe_reason(specs["npm run check"], self.root))

    def test_pre_and_post_scripts_are_inspected(self):
        """Regression: npm runs ``prelint`` automatically."""
        write_package(self.root, scripts={"prelint": "eslint --fix .", "lint": "x"})
        self.assertIn("'prelint'", self.reason("npm", "run", "lint") or "")
        write_package(self.root, scripts={"lint": "x", "postlint": "x || true"})
        self.assertIn("'postlint'", self.reason("npm", "run", "lint") or "")

    def test_run_flags_before_the_script_name_are_skipped(self):
        """Regression: ``npm run -s fix`` captured ``-s`` as the script."""
        for runner in (
            "npm run -s fix",
            "npm run --silent fix",
            "npm --if-present run fix",
        ):
            with self.subTest(runner=runner):
                write_package(
                    self.root, scripts={"fix": "eslint --fix .", "lint": runner}
                )
                self.assertIn("'fix'", self.reason("npm", "run", "lint") or "")

    def test_script_runners_and_their_globs(self):
        """Regression: npm-run-all, run-s, run-p, and concurrently."""
        cases = (
            "npm-run-all lint:*",
            "run-s 'lint:*'",
            "run-p --print-label lint:**",
            'npm-run-all "lint:js -- --fix"',
            "concurrently 'npm:lint:*'",
            'concurrently -n js "npm run lint:fix"',
        )
        for runner in cases:
            with self.subTest(runner=runner):
                write_package(
                    self.root,
                    scripts={
                        "lint:fix": "eslint --fix .",
                        "lint:js": "x",
                        "lint": runner,
                    },
                )
                self.assertIn("--fix", self.reason("npm", "run", "lint") or "")

    def test_concurrently_with_inline_commands(self):
        write_package(self.root, scripts={"lint": "concurrently 'eslint . || true'"})
        self.assertIn("|| true", self.reason("npm", "run", "lint") or "")
        write_package(self.root, scripts={"lint": "concurrently npm:a:* 'tsc'"})
        self.assertIsNone(self.reason("npm", "run", "lint"))

    def test_arguments_passed_on_to_a_script(self):
        write_package(self.root, scripts={"lint": "eslint ."})
        self.assertIn("--fix", self.reason("npm", "run", "lint", "--", "--fix") or "")
        self.assertIn("--fix", self.reason("yarn", "lint", "--fix") or "")
        self.assertIsNone(self.reason("npm", "run", "lint"))

    def test_shorthand_lifecycle_and_unknown_forms(self):
        write_package(self.root, scripts={"lint": "x || true", "test": "y || true"})
        self.assertIsNotNone(self.reason("yarn", "lint"))
        self.assertIsNotNone(self.reason("bun", "run", "lint"))
        self.assertIsNotNone(self.reason("npm", "t"))
        self.assertIsNone(self.reason("npm", "install"))
        self.assertIsNone(self.reason("npm", "run"))
        self.assertIsNone(self.reason("npm", "run", "-s"))
        self.assertIsNone(self.reason("npm", "--prefix"))
        self.assertIsNone(self.reason("npm", "exec", "tsc"))
        self.assertIsNone(self.reason("npm", "run", "missing"))

    def test_configured_commands_resolve_scripts_too(self):
        """Regression: the config source skipped script inspection."""
        write_package(self.root, scripts={"lint": "eslint --fix ."})
        specs = inference.configured_project_checks(
            {"commands": [["npm", "run", "lint"]]}, self.root
        )
        self.assertIn("--fix", checks.unsafe_reason(specs[0], self.root) or "")

    def test_workspace_runs_read_every_package(self):
        write_package(self.root, scripts={"lint": "pnpm -r lint"})
        write(
            self.root,
            "packages/web/package.json",
            json.dumps({"scripts": {"lint": "eslint . || true"}}),
        )
        write(self.root, "node_modules/x/package.json", '{"scripts": {"lint": "x -w"}}')
        reason = self.reason("pnpm", "run", "lint")
        self.assertIn("packages/web/package.json script 'lint'", reason or "")
        for runner in (
            ("turbo", "run", "lint"),
            ("turbo", "lint", "--filter=web"),
            ("lerna", "run", "lint"),
            ("yarn", "workspaces", "foreach", "-A", "run", "lint"),
            ("yarn", "workspace", "web", "lint"),
            ("npm", "run", "lint", "--workspaces"),
            ("npm", "--workspace=web", "run", "lint"),
        ):
            with self.subTest(runner=runner):
                self.assertIsNotNone(self.reason(*runner))

    def test_text_without_a_root_does_not_read_scripts(self):
        write_package(self.root, scripts={"lint": "eslint --fix ."})
        self.assertIsNone(checks.command_problem("npm run lint"))
        self.assertIsNotNone(checks.command_problem("npm run lint", self.root))

    def test_legitimate_cleanup_build_runs_and_passes(self):
        """Regression: a non-final ``|| true`` cleanup step failed the gate."""
        write_package(
            self.root,
            scripts={"clean": "rm -rf dist || true", "build": "npm run clean && tsc"},
        )
        specs = inference.package_script_checks(self.root)
        outcome = checks.run_project_checks(self.root, specs, FakeRunner(), 60)
        self.assertEqual(outcome.status, "pass")


class MakefileInspectionTests(TempRootTest):
    def reason(self, makefile: str, *command: str) -> str | None:
        write(self.root, "Makefile", makefile)
        spec = inference.CheckSpec("check", command or ("make", "lint"), "Makefile")
        return checks.unsafe_reason(spec, self.root)

    def test_make_recipe_is_inspected(self):
        write(
            self.root,
            "Makefile",
            "lint:\n\truff check . || true\n\ncheck:\n\truff check .\nother:\n",
        )
        specs = {spec.name: spec for spec in inference.make_checks(self.root, no_tools)}
        self.assertIn(
            "|| true", checks.unsafe_reason(specs["make lint"], self.root) or ""
        )
        self.assertIsNone(checks.unsafe_reason(specs["make check"], self.root))

    def test_recipes_make_really_runs_are_read(self):
        """Regression: each of these Makefiles rewrote files or hid a failure."""
        cases = {
            "prerequisite": "lint: fmt\n\truff check .\nfmt:\n\truff format .\n",
            "inline": "lint: ; ruff check --fix .\n",
            "blank line": "lint:\n\truff check .\n\n\truff check --fix .\n",
            "comment line": "lint:\n# fix it\n\truff check --fix .\n",
            "sub-make": "lint:\n\t$(MAKE) impl\nimpl:\n\truff check . || true\n",
            "plain make": "lint:\n\t@make -s impl\nimpl:\n\truff check . || true\n",
            "variable": "FLAGS := --fix\nlint:\n\truff check $(FLAGS) .\n",
            "appended": "F = -q\nF += --fix\nlint:\n\truff check ${F} .\n",
            "conditional": "lint:\nifdef CI\n\truff check .\nelse\n\truff --fix .\nendif\n",
            "redefined": "lint:\n\truff check .\nlint:\n\truff check --fix .\n",
            "continued": "lint:\n\truff check . \\\n\t  --fix\n",
        }
        for label, makefile in cases.items():
            with self.subTest(label=label):
                self.assertIsNotNone(self.reason(makefile))

    def test_ways_make_ignores_failures(self):
        cases = {
            "lint:\n\t-ruff check .\n": "the - prefix",
            "lint:\n\t@-ruff check .\n": "the - prefix",
            ".IGNORE: lint\nlint:\n\truff check .\n": ".IGNORE",
            ".IGNORE:\nlint:\n\truff check .\n": ".IGNORE",
        }
        for makefile, marker in cases.items():
            with self.subTest(makefile=makefile):
                self.assertIn(marker, self.reason(makefile) or "")
        good = "lint:\n\truff check .\n"
        for flags in (("-i",), ("--ignore-errors",), ("-ki",)):
            with self.subTest(flags=flags):
                reason = self.reason(good, "make", *flags, "lint")
                self.assertIn("make -i", reason or "")

    def test_safe_makefiles(self):
        cases = (
            "lint:\n\truff check .\nlint:\n\techo again\n",
            "lint:\n\truff check --fix .\nlint:\n\truff check .\n",
            ".IGNORE: other\nlint:\n\truff check .\n",
            "lint: CFLAGS = -O2\nlint: check\n\techo ok\ncheck: lint\n\ttrue\n",
            "define FIX\n\truff --fix .\nlint: x\nendef\nlint:\n\truff check .\n",
            "lint: # docs\n\t+ruff check .\n\n# trailing\n",
            "include other.mk\nlint:\n\truff check .\n",
            "X != echo --fix\nY ?= a\nY ?= --fix\nlint:\n\truff check $(Y) $(UNSET) $$HOME\n",
            "LOOP = $(LOOP)\nlint:\n\techo $(LOOP)\n",
            "\truff --fix .\nVAR = 1\n\truff --fix .\nlint:\n\truff check .\n",
        )
        for makefile in cases:
            with self.subTest(makefile=makefile):
                self.assertIsNone(self.reason(makefile))

    def test_one_shell_and_errexit_settings(self):
        recipe = "lint:\n\truff check .\n\tmypy .\n"
        self.assertIsNone(self.reason(recipe))
        self.assertIn("later command", self.reason(".ONESHELL:\n" + recipe) or "")
        self.assertIsNone(self.reason(".ONESHELL:\n.POSIX:\n" + recipe))
        self.assertIsNone(self.reason(".ONESHELL:\n.SHELLFLAGS = -ec\n" + recipe))

    def test_make_command_line_options(self):
        write(self.root, "sub/Makefile", "lint:\n\truff check --fix .\n")
        write(self.root, "ci.mk", "lint:\n\truff check --fix .\n")
        write(self.root, "Makefile", "lint:\n\truff check .\n")
        cases = (
            ("make", "-C", "sub", "lint"),
            ("make", "-Csub", "lint"),
            ("make", "--directory=sub", "lint"),
            ("make", "--directory", "sub", "V=1", "lint"),
            ("gmake", "-f", "ci.mk", "lint"),
            ("make", "--file=ci.mk", "-j4", "lint"),
            ("make", "-fci.mk"),
        )
        for command in cases:
            with self.subTest(command=command):
                spec = inference.CheckSpec("x", command, "config")
                self.assertIsNotNone(checks.unsafe_reason(spec, self.root))
        missing = inference.CheckSpec("x", ("make", "-C", "nowhere", "lint"), "c")
        self.assertIsNone(checks.unsafe_reason(missing, self.root))

    def test_makefile_outside_the_repository_is_named_by_file(self):
        with tempfile.TemporaryDirectory() as name:
            write(Path(name), "Makefile", "lint:\n\tblack .\n")
            spec = inference.CheckSpec("x", ("make", "-C", name, "lint"), "c")
            reason = checks.unsafe_reason(spec, self.root)
        self.assertIn("in Makefile target 'lint'", reason or "")

    def test_default_goal(self):
        self.assertIsNotNone(
            self.reason(".PHONY: all\nall:\n\tblack .\nlint:\n", "make")
        )
        self.assertIsNotNone(
            self.reason(".DEFAULT_GOAL := lint\nall:\nlint:\n\tblack .\n", "make")
        )
        self.assertIsNone(self.reason("%.o: %.c\n\tcc $<\n", "make"))

    def test_parsed_makefile(self):
        parsed = make.parse_makefile(
            "export A ::= 1\noverride B = $(A)\nall: x y\nx:\n\t@-echo $(B)\nx: z\n"
        )
        self.assertEqual(parsed.default_goal, "all")
        self.assertEqual(parsed.rules["x"].prerequisites, ["z"])
        self.assertEqual(
            parsed.recipe_lines("x"), [make.RecipeLine("echo 1", ignores_errors=True)]
        )
        self.assertFalse(parsed.ignores_errors("x"))
        self.assertFalse(parsed.errexit)


class SafetyHelperTests(unittest.TestCase):
    def test_make_call(self):
        call = safety.make_call(["-ks", "--file", "x.mk", "A=1", "lint", "check"])
        self.assertEqual(
            (call.file, call.targets, call.ignores_errors),
            ("x.mk", ["lint", "check"], False),
        )

    def test_matching_scripts(self):
        scripts = {"lint": "", "lint:js": "", "lint:js:fix": "", "lintx": ""}
        self.assertEqual(safety.matching_scripts("lint:*", scripts), ["lint:js"])
        self.assertEqual(
            safety.matching_scripts("lint:**", scripts), ["lint:js", "lint:js:fix"]
        )
        self.assertEqual(safety.matching_scripts("nope", scripts), [])

    def test_unsafe_reason_reads_argument_lists_without_a_shell(self):
        root = Path(".")
        literal = inference.CheckSpec("x", ("eslint", ".", "||", "true"), "config")
        self.assertIsNone(checks.unsafe_reason(literal, root))
        empty = inference.CheckSpec("x", ("X=1",), "config")
        self.assertIsNone(checks.unsafe_reason(empty, root))
        fixed = inference.CheckSpec("fix", ("eslint", "--fix", "."), "config")
        self.assertIsNotNone(checks.unsafe_reason(fixed, root))


def spec(name: str, *command: str, fail_on_output: bool = False):
    return inference.CheckSpec(name, tuple(command), "config", fail_on_output)


class RunProjectChecksTests(TempRootTest):
    def test_no_declared_checks_is_not_applicable(self):
        outcome = checks.run_project_checks(self.root, [], FakeRunner(), 60)
        self.assertEqual(outcome.status, "not_applicable")
        self.assertEqual(outcome.summary, checks.NOT_DECLARED_SUMMARY)
        self.assertEqual(outcome.key, "project_checks")
        self.assertEqual(outcome.title, "Project checks")

    def test_every_passing_check_passes_the_gate(self):
        runner = FakeRunner()
        specs = [spec("lint", "lint", "."), spec("types", "tsc")]
        outcome = checks.run_project_checks(self.root, specs, runner, 60)
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.summary, "Project checks passed: 2 of 2.")
        self.assertEqual(runner.calls, [("lint", "."), ("tsc",)])
        self.assertEqual(outcome.items, [])
        self.assertEqual(
            [row["status"] for row in outcome.measurements["checks"]], ["pass", "pass"]
        )

    def test_non_zero_exit_fails_with_the_output_tail(self):
        output = "\n".join(f"src/a.ts:{n}:1 error" for n in range(1, 41))
        runner = FakeRunner({"tsc": (2, output)})
        outcome = checks.run_project_checks(
            self.root, [spec("lint", "lint"), spec("types", "tsc")], runner, 60
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.summary, "1 of 2 project checks failed.")
        self.assertEqual(len(outcome.items), 1)
        item = outcome.items[0]
        self.assertEqual(
            (item.path, item.name, item.metric, item.value),
            ("config", "types", "exit code", "2"),
        )
        self.assertIn("    src/a.ts:40:1 error", outcome.details)
        self.assertNotIn("    src/a.ts:10:1 error", outcome.details)
        self.assertEqual(outcome.measurements["failures"], 1)

    def test_timeout_fails(self):
        runner = FakeRunner(timed_out=["slow"])
        outcome = checks.run_project_checks(
            self.root, [spec("slow", "slow")], runner, 30
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.items[0].metric, "runtime seconds")
        self.assertEqual(outcome.items[0].value, "over 30")

    def test_missing_executable_blocks(self):
        runner = FakeRunner(
            {"ruff check .": (127, "No such file or directory: 'ruff'")}
        )
        outcome = checks.run_project_checks(
            self.root,
            [spec("ruff", "ruff", "check", "."), spec("ok", "ok")],
            runner,
            60,
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertIn("could not start", outcome.summary)
        self.assertEqual(outcome.measurements["blocked"], 1)
        self.assertTrue(any("ruff" in line for line in outcome.details))

    def test_failure_outranks_blocked(self):
        runner = FakeRunner({"gone": (127, ""), "bad": (1, "boom")})
        outcome = checks.run_project_checks(
            self.root, [spec("gone", "gone"), spec("bad", "bad")], runner, 60
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.measurements["blocked"], 1)

    def test_listing_tool_fails_on_output_with_an_item_per_file(self):
        listed = "\n".join(f"pkg/file{n}.go" for n in range(52))
        runner = FakeRunner({"gofmt -l .": (0, listed)})
        gofmt = spec("gofmt", "gofmt", "-l", ".", fail_on_output=True)
        outcome = checks.run_project_checks(self.root, [gofmt], runner, 60)
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(len(outcome.items), 51)
        self.assertEqual(outcome.items[0].path, "pkg/file0.go")
        self.assertEqual(outcome.items[-1].metric, "more files listed")
        self.assertEqual(outcome.items[-1].value, "2")
        self.assertEqual(outcome.measurements["failures"], 1)

    def test_listing_tool_with_no_output_passes(self):
        runner = FakeRunner({"gofmt -l .": (0, "\n")})
        gofmt = spec("gofmt", "gofmt", "-l", ".", fail_on_output=True)
        outcome = checks.run_project_checks(self.root, [gofmt], runner, 60)
        self.assertEqual(outcome.status, "pass")

    def test_unsafe_check_is_never_run(self):
        runner = FakeRunner()
        outcome = checks.run_project_checks(
            self.root, [spec("fix", "eslint", "--fix", ".")], runner, 60
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.items[0].metric, "unsafe check")
        self.assertEqual(outcome.measurements["checks"][0]["status"], "unsafe")

    def test_real_subprocess_runner(self):
        python = sys.executable
        specs = [
            spec("ok", python, "-c", "print('fine')"),
            spec("bad", python, "-c", "raise ValueError('E1 broken')"),
            spec("missing", str(self.root / "no-such-tool")),
        ]
        outcome = checks.run_project_checks(
            self.root, specs, subprocess_runner(self.root), 60
        )
        statuses = [row["status"] for row in outcome.measurements["checks"]]
        self.assertEqual(statuses, ["pass", "fail", "blocked"])
        self.assertIn("    ValueError: E1 broken", outcome.details)


class LegacySectionTests(unittest.TestCase):
    def test_declared_project_checks_win(self):
        config = {
            "project_checks": {"commands": [["make", "lint"]], "timeout_seconds": 9},
            "types": {"commands": [["mypy", "src"]]},
        }
        self.assertEqual(
            inference.project_checks_section(config),
            {"commands": [["make", "lint"]], "timeout_seconds": 9},
        )

    def test_legacy_lint_then_type_commands_carry_over(self):
        config = {
            "project_checks": {"timeout_seconds": 60},
            "format_lint": {"commands": [["ruff", "check", "."]]},
            "types": {"commands": [["mypy", "src"], ["mypy", "gate.py"]]},
        }
        self.assertEqual(
            inference.project_checks_section(config),
            {
                "timeout_seconds": 60,
                "commands": [
                    ["ruff", "check", "."],
                    ["mypy", "src"],
                    ["mypy", "gate.py"],
                ],
            },
        )

    def test_disabled_or_malformed_legacy_sections_are_skipped(self):
        config = {
            "format_lint": {"enabled": False, "commands": [["eslint", "."]]},
            "types": {"commands": "mypy ."},
        }
        self.assertEqual(inference.project_checks_section(config), {})
        self.assertEqual(inference.project_checks_section({"types": None}), {})


if __name__ == "__main__":
    unittest.main()
