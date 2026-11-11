import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_vulnerabilities_manifests as manifests  # noqa: E402


def declares(name: str, text: str) -> bool:
    rule = manifests.manifest_rule(name)
    assert rule is not None
    return rule.declares(text)


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DeclaresDependenciesTests(unittest.TestCase):
    def test_pyproject_dependency_tables(self):
        cases = {
            '[project]\nname = "x"\ndependencies = [\n  "requests>=2",\n]\n': True,
            '[project]\nname = "x"\ndependencies = []\n': False,
            '[project]\ndependencies = [\n  # a comment\n  "a",\n]\n': True,
            '[project.optional-dependencies]\ndev = ["pytest"]\n': True,
            '[dependency-groups]\ntest = ["pytest>=8"]\n': True,
            '[tool.uv]\ndev-dependencies = ["ruff"]\n': True,
            '[tool.pdm.dev-dependencies]\nlint = ["ruff"]\n': True,
            '[tool.poetry.dependencies]\npython = "^3.10"\n': False,
            '[tool.poetry.dependencies]\npython = "^3.10"\nrequests = "^2"\n': True,
            '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n': True,
            "[tool.ruff]\nline-length = 88\n": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(declares("pyproject.toml", text), expected)

    def test_cargo_dependency_tables(self):
        cases = {
            '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\n': True,
            '[package]\nname = "x"\n\n[dependencies]\n\n[[bin]]\nname = "x"\n': False,
            '[dependencies.serde]\nversion = "1"\n': True,
            "[target.'cfg(unix)'.dev-dependencies]\nlibc = \"0.2\"\n": True,
            '[workspace]\nmembers = ["a"]\n': False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(declares("Cargo.toml", text), expected)

    def test_pipfile_and_json_manifests(self):
        self.assertTrue(declares("Pipfile", '[packages]\nrequests = "*"\n'))
        self.assertFalse(declares("Pipfile", '[requires]\npython_version = "3.12"\n'))
        self.assertTrue(declares("package.json", '{"devDependencies": {"jest": "1"}}'))
        self.assertFalse(declares("package.json", '{"dependencies": {}}'))
        self.assertFalse(declares("package.json", '{"dependencies": ["x"]}'))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            declares("package.json", "[]")
        self.assertTrue(declares("composer.json", '{"require": {"monolog/x": "^3"}}'))
        self.assertFalse(
            declares(
                "composer.json",
                '{"require": {"php": "^8", "ext-json": "*"}, "require-dev": []}',
            )
        )

    def test_pubspec_ignores_flutter_sdk_entries(self):
        flutter_only = (
            "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n"
            "dev_dependencies:\n  flutter_test:\n    sdk: flutter\n"
        )
        self.assertFalse(declares("pubspec.yaml", flutter_only))
        self.assertTrue(declares("pubspec.yaml", flutter_only + "  mockito: ^5.0.0\n"))
        self.assertFalse(declares("pubspec.yaml", "name: app\nflutter:\n  x: 1\n"))

    def test_line_pattern_manifests(self):
        cases = [
            ("requirements.txt", "requests>=2\n", True),
            ("requirements.txt", "# only comments\n-r base.txt\n", False),
            ("Gemfile", 'source "https://rubygems.org"\ngem "rails"\n', True),
            ("gems.rb", "gem 'rack'\n", True),
            ("go.mod", "module x\n\ngo 1.22\n", False),
            ("go.mod", "module x\n\nrequire golang.org/x/text v0.14.0\n", True),
            ("go.mod", "module x\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n", True),
            ("mix.exs", 'defp deps do\n  [{:phoenix, "~> 1.7"}]\nend\n', True),
            ("Package.swift", '.package(url: "https://x", from: "1.0.0")', True),
            ("pom.xml", "<project><dependencies><dependency>", True),
            ("pom.xml", "<project></project>", False),
            ("build.gradle", "dependencies {\n  implementation 'a:b:1'\n}\n", True),
            ("build.gradle.kts", 'dependencies {\n  api("a:b:1")\n}\n', True),
            ("App.csproj", '<PackageReference Include="X" Version="1" />', True),
            ("App.fsproj", "<Project />", False),
        ]
        for name, text, expected in cases:
            with self.subTest(name=name, text=text):
                self.assertEqual(declares(name, text), expected)

    def test_unknown_names_have_no_rule(self):
        self.assertIsNone(manifests.manifest_rule("README.md"))
        self.assertTrue(manifests.is_dependency_file("go.sum"))
        self.assertTrue(manifests.is_dependency_file("yarn.lock"))
        self.assertFalse(manifests.is_dependency_file("main.py"))


def index_for(root: Path, files: dict) -> "manifests.LockIndex":
    for relative, text in files.items():
        write(root, relative, text)
    return manifests.LockIndex.build(root, files)


NODE = manifests.MANIFEST_RULES["package.json"]
PYPROJECT = manifests.MANIFEST_RULES["pyproject.toml"]
DEPENDS = '{"dependencies": {"a": "1"}}'

PNPM_WORKSPACE_LOCK = """lockfileVersion: '9.0'

importers:

  .:
    dependencies: {}

  packages/a:
    dependencies:
      lodash:
        specifier: ^4
        version: 4.17.21
"""

YARN_BERRY_WORKSPACE_LOCK = """__metadata:
  version: 8

"a@workspace:packages/a":
  version: 0.0.0-use.local
"""

UV_WORKSPACE_LOCK = """version = 1

[manifest]
members = ["api", "root"]

[[package]]
name = "api"
version = "0.1.0"
source = { editable = "packages/api" }
"""


class LockIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_same_folder_lockfiles_lock_and_go_never_searches_parents(self):
        index = index_for(
            self.root,
            {"package-lock.json": "{}", "package.json": DEPENDS, "svc/go.mod": ""},
        )
        go = manifests.MANIFEST_RULES["go.mod"]
        self.assertTrue(index.has_lock(NODE, "package.json"))
        self.assertFalse(index.has_lock(go, "svc/go.mod"))

    def test_parent_lockfiles_lock_only_the_workspace_members_they_list(self):
        cases = {
            "package-lock.json": '{"packages": {"packages/a": {"version": "1"}}}',
            "npm-shrinkwrap.json": '{"packages": {"packages/a": {}}}',
            "pnpm-lock.yaml": PNPM_WORKSPACE_LOCK,
            "yarn.lock": YARN_BERRY_WORKSPACE_LOCK,
        }
        for lock, text in cases.items():
            with self.subTest(lock=lock), tempfile.TemporaryDirectory() as temp:
                index = index_for(
                    Path(temp),
                    {lock: text, "packages/a/package.json": DEPENDS},
                )
                self.assertTrue(index.has_lock(NODE, "packages/a/package.json"))
                self.assertFalse(index.has_lock(NODE, "packages/b/package.json"))
                self.assertFalse(index.has_lock(NODE, "tools/a/package.json"))

    def test_yarn_classic_members_come_from_root_workspaces(self):
        files = {
            "package.json": '{"workspaces": {"packages": ["packages/*"]}}',
            "yarn.lock": '# yarn lockfile v1\n\nlodash@^4:\n  version "4.17.21"\n',
        }
        index = index_for(self.root, files)
        self.assertTrue(index.has_lock(NODE, "packages/a/package.json"))
        self.assertFalse(index.has_lock(NODE, "tools/x/package.json"))
        for root_manifest in ("{oops", '{"workspaces": "packages/*"}', "[]"):
            with self.subTest(root_manifest=root_manifest):
                write(self.root, "package.json", root_manifest)
                self.assertFalse(index.has_lock(NODE, "packages/a/package.json"))

    def test_cargo_members_are_listed_by_package_name(self):
        cargo = manifests.MANIFEST_RULES["Cargo.toml"]
        index = index_for(
            self.root,
            {
                "Cargo.lock": '[[package]]\nname = "app"\nversion = "0.1.0"\n',
                "app/Cargo.toml": '[package]\nname = "app"\n',
                "other/Cargo.toml": '[package]\nname = "other"\n',
                "virtual/Cargo.toml": '[workspace]\nmembers = ["x"]\n',
            },
        )
        self.assertTrue(index.has_lock(cargo, "app/Cargo.toml"))
        self.assertFalse(index.has_lock(cargo, "other/Cargo.toml"))
        self.assertFalse(index.has_lock(cargo, "virtual/Cargo.toml"))

    def test_uv_workspace_members_are_listed_by_path(self):
        index = index_for(
            self.root,
            {
                "uv.lock": UV_WORKSPACE_LOCK,
                "packages/api/pyproject.toml": "",
                "docs/requirements.txt": "",
            },
        )
        requirements = manifests.manifest_rule("requirements.txt")
        self.assertTrue(index.has_lock(PYPROJECT, "packages/api/pyproject.toml"))
        self.assertFalse(index.has_lock(requirements, "docs/requirements.txt"))

    def test_unreadable_parent_lockfiles_prove_nothing(self):
        index = manifests.LockIndex.build(self.root, ["package-lock.json"])
        self.assertFalse(index.has_lock(NODE, "packages/a/package.json"))

    def test_requirements_files_lock_pyproject_only_when_fully_pinned(self):
        pinned = index_for(self.root, {"requirements.txt": "a==1.0\n# note\n"})
        self.assertTrue(pinned.has_lock(PYPROJECT, "pyproject.toml"))
        write(self.root, "requirements.txt", "a==1.0\nb>=2\n")
        self.assertFalse(pinned.has_lock(PYPROJECT, "pyproject.toml"))
        parent = index_for(self.root, {"requirements.txt": "a==1.0\n"})
        self.assertFalse(parent.has_lock(PYPROJECT, "app/pyproject.toml"))
        nested = index_for(
            self.root, {"app/requirements-lock.txt": "a==1\n", "app/x.lock": ""}
        )
        self.assertTrue(nested.has_lock(PYPROJECT, "app/pyproject.toml"))

    def test_gradle_lockfiles_lock_only_their_own_project(self):
        gradle = manifests.MANIFEST_RULES["build.gradle"]
        index = index_for(self.root, {"gradle.lockfile": "a:b:1=x\n"})
        self.assertTrue(index.has_lock(gradle, "build.gradle"))
        self.assertFalse(index.has_lock(gradle, "app/build.gradle"))


class RequirementsPinnedTests(unittest.TestCase):
    def test_every_requirement_line_must_be_an_exact_pin(self):
        cases = {
            "a==1.0\nb == 2.0\nc===3.0\nd[x]==4 ; python_version < '4'\n": True,
            "a==1.0 \\\n    --hash=sha256:abc\n-r base.txt\n-e .\n": True,
            "pkg @ https://example.com/pkg.whl\ngit+https://x/y.git@v1\n": True,
            "a==1.0\nb>=2\n": False,
            "a==1.*\n": False,
            "a==1.0,!=1.0.1\n": False,
            "a\n": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(manifests.requirements_pinned(text), expected)

    def test_lock_search_reaches_parents_only_for_workspace_ecosystems(self):
        self.assertEqual(
            manifests.lock_search_directories(NODE, "a/b/package.json"),
            ["a/b", "a", "."],
        )
        go = manifests.MANIFEST_RULES["go.mod"]
        self.assertEqual(manifests.lock_search_directories(go, "a/go.mod"), ["a"])


class CheckManifestTests(unittest.TestCase):
    def test_verdicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, "web/package.json", '{"dependencies": {"a": "1"}}')
            write(root, "empty/package.json", '{"name": "x"}')
            write(root, "locked/package.json", '{"dependencies": {"a": "1"}}')
            write(root, "locked/yarn.lock", "")
            write(root, "java/pom.xml", "<dependency>")
            write(root, "pinned/requirements.txt", "a==1\n")
            files = [
                "web/package.json",
                "empty/package.json",
                "locked/package.json",
                "locked/yarn.lock",
                "java/pom.xml",
                "pinned/requirements.txt",
            ]
            index = manifests.LockIndex.build(root, files)

            def verdict(relative):
                return manifests.check_manifest(root, relative, index).verdict

            self.assertEqual(verdict("web/package.json"), manifests.UNLOCKED_FAILS)
            self.assertEqual(verdict("empty/package.json"), "no dependencies")
            self.assertEqual(verdict("locked/package.json"), "locked")
            self.assertEqual(verdict("java/pom.xml"), manifests.UNLOCKED_BLOCKS)
            self.assertEqual(verdict("pinned/requirements.txt"), "locked")
            self.assertEqual(
                manifests.check_manifest(root, "web/package.json", index),
                manifests.ManifestCheck("web/package.json", manifests.UNLOCKED_FAILS),
            )
            with self.assertRaisesRegex(ValueError, "not a dependency manifest"):
                manifests.check_manifest(root, "locked/yarn.lock", index)

    def test_cargo_libraries_may_omit_the_lockfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = '[package]\nname = "lib"\n[dependencies]\nserde = "1"\n'
            write(root, "lib/Cargo.toml", library)
            write(root, "lib/src/lib.rs", "")
            write(root, "app/Cargo.toml", library)
            write(root, "app/src/main.rs", "fn main() {}\n")
            write(root, "tools/Cargo.toml", library)
            (root / "tools" / "src" / "bin").mkdir(parents=True)
            write(root, "multi/Cargo.toml", library + '[[bin]]\nname = "x"\n')
            index = manifests.LockIndex.build(root, [])
            expected = {
                "lib/Cargo.toml": manifests.UNLOCKED_IS_NORMAL,
                "app/Cargo.toml": manifests.UNLOCKED_FAILS,
                "tools/Cargo.toml": manifests.UNLOCKED_FAILS,
                "multi/Cargo.toml": manifests.UNLOCKED_FAILS,
            }
            for relative, wanted in expected.items():
                with self.subTest(relative=relative):
                    check = manifests.check_manifest(root, relative, index)
                    self.assertEqual(check.verdict, wanted)


class DependencyFilesTests(unittest.TestCase):
    def test_lists_manifests_and_lockfiles_outside_fixture_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, "package.json", "{}")
            write(root, "package-lock.json", "{}")
            write(root, "svc/go.sum", "")
            write(root, "e2e/package.json", "{}")
            write(root, "tests/requirements.txt", "")
            write(root, "requirements_test.txt", "")
            write(root, "tests/fixtures/package.json", "{}")
            write(root, "go/testdata/go.mod", "")
            write(root, "node_modules/x/package.json", "{}")
            write(root, "src/app.py", "")
            self.assertEqual(
                manifests.dependency_files(root),
                [
                    "e2e/package.json",
                    "package-lock.json",
                    "package.json",
                    "requirements_test.txt",
                    "svc/go.sum",
                    "tests/requirements.txt",
                ],
            )


if __name__ == "__main__":
    unittest.main()
