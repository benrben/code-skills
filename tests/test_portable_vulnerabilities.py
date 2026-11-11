from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/code-discipline/scripts/portable_vulnerabilities.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "portable_vulnerabilities_test", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load vulnerability scanner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = load_module()


def locked_inventory(root: Path) -> SimpleNamespace:
    """Packages, lockfiles and unsupported lockfiles directly in ``root``."""
    packages, lockfiles, unsupported = set(), [], []
    for path in sorted(root.iterdir()):
        kind = scanner.classify_lockfile(path.name) if path.is_file() else None
        if kind == "unsupported":
            unsupported.append(path.name)
        elif kind == "supported":
            lockfiles.append(path.name)
            text = path.read_text(encoding="utf-8")
            packages.update(scanner.read_lockfile(path.name, text, path.name).packages)
    return SimpleNamespace(
        packages=tuple(packages), lockfiles=lockfiles, unsupported=tuple(unsupported)
    )


class PortableInventoryTests(unittest.TestCase):
    def test_extracts_common_lockfiles_without_installing_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements-dev.txt").write_text(
                "requests==2.31.0\nunpinned>=1\nignored==latest\n", encoding="utf-8"
            )
            (root / "package-lock.json").write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"name": "app", "version": "1.0.0"},
                            "node_modules/lodash": {"version": "4.17.20"},
                            "node_modules/@scope/pkg": {"version": "2.0.0"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "Cargo.lock").write_text(
                '[[package]]\nname = "time"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )

            inventory = locked_inventory(root)

            coordinates = {
                (item.ecosystem, item.name, item.version) for item in inventory.packages
            }
            self.assertIn(("PyPI", "requests", "2.31.0"), coordinates)
            self.assertIn(("npm", "lodash", "4.17.20"), coordinates)
            self.assertIn(("npm", "@scope/pkg", "2.0.0"), coordinates)
            self.assertIn(("crates.io", "time", "0.1.0"), coordinates)
            self.assertNotIn(("PyPI", "unpinned", "1"), coordinates)
            self.assertEqual(len(inventory.lockfiles), 3)

    def test_unsupported_lockfiles_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bun.lock").write_text("{}\n", encoding="utf-8")
            inventory = locked_inventory(root)
            self.assertEqual(inventory.packages, ())
            self.assertEqual(inventory.unsupported, ("bun.lock",))

    def test_extracts_each_supported_ecosystem_and_ignores_cache_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "Pipfile.lock": {
                    "default": {
                        "requests": {"version": "==2.32.0"},
                        "plain": "==1.0",
                        "range": ">=2",
                    },
                    "develop": "invalid",
                },
                "composer.lock": {
                    "packages": [
                        {"name": "vendor/app", "version": "1.2.3"},
                        "invalid",
                    ],
                    "packages-dev": "invalid",
                },
                "packages.lock.json": {
                    "dependencies": {
                        "net8.0": {
                            "Example": {"resolved": "3.0.0"},
                            "Missing": {},
                        },
                        "invalid": [],
                    }
                },
            }
            for name, value in files.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            (root / "go.mod").write_text(
                "module example\n"
                "go 1.23\n\n"
                "require (\n"
                "  example.com/one v1.2.3\n"
                ")\n"
                "require example.com/two v2.0.0 // indirect\n"
                "// ignored\n"
                "invalid\n"
                "example.com/no-version latest\n",
                encoding="utf-8",
            )
            (root / "Gemfile.lock").write_text(
                "GEM\n  remote: https://rubygems.org/\n  specs:\n"
                "    rack (3.0.0)\n    malformed\nPLATFORMS\n  ruby\n",
                encoding="utf-8",
            )
            (root / "poetry.lock").write_text(
                'name = "orphan"\n[[package]]\nname = "alpha"\nversion = "1.0"\n'
                'ignored = true\n[[package]]\nname = "beta"\nversion = "2.0"\n',
                encoding="utf-8",
            )
            (root / "uv.lock").write_text(
                '[[package]]\nname = "incomplete"\n', encoding="utf-8"
            )
            ignored = root / "node_modules"
            ignored.mkdir()
            (ignored / "requirements.txt").write_text("hidden==1\n", encoding="utf-8")
            (root / "notes.txt").write_text("not a lock\n", encoding="utf-8")

            inventory = locked_inventory(root)
            coordinates = {
                (item.ecosystem, item.name, item.version) for item in inventory.packages
            }

            self.assertTrue(
                {
                    ("PyPI", "requests", "2.32.0"),
                    ("PyPI", "plain", "1.0"),
                    ("Packagist", "vendor/app", "1.2.3"),
                    ("NuGet", "Example", "3.0.0"),
                    ("Go", "example.com/one", "1.2.3"),
                    ("Go", "example.com/two", "2.0.0"),
                    ("RubyGems", "rack", "3.0.0"),
                    ("PyPI", "alpha", "1.0"),
                    ("PyPI", "beta", "2.0"),
                }.issubset(coordinates)
            )
            self.assertFalse(any(item.name == "hidden" for item in inventory.packages))

    def test_malformed_container_shapes_return_empty_inventories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = {
                "package-lock.json": {"packages": []},
                "Pipfile.lock": [],
                "composer.lock": [],
                "packages.lock.json": {"dependencies": []},
            }
            for name, value in cases.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            inventory = locked_inventory(root)
            self.assertEqual(inventory.packages, ())
            self.assertEqual(len(inventory.lockfiles), len(cases))

    def test_locked_coordinate_validation_is_explicit(self):
        self.assertIsNone(scanner._locked("npm", None, "1", "lock"))
        self.assertIsNone(scanner._locked("npm", "x", None, "lock"))
        self.assertIsNone(scanner._locked("npm", " ", "1", "lock"))
        self.assertIsNone(scanner._locked("npm", "x", "latest", "lock"))
        item = scanner._locked("Go", "example.com/x", "v1.2.0", "go.mod")
        self.assertEqual(item.version, "1.2.0")


YARN_CLASSIC = """# yarn lockfile v1


"@babel/code-frame@^7.0.0", "@babel/code-frame@^7.10.4":
  version "7.12.13"
  resolved "https://registry.yarnpkg.com/@babel/code-frame/-/code-frame-7.12.13.tgz"
  dependencies:
    "@babel/highlight" "^7.12.13"

lodash@^4.17.20:
  version "4.17.21"
"""

YARN_BERRY = """__metadata:
  version: 6
  cacheKey: 8

"lodash@npm:^4.17.20":
  version: 4.17.21
  resolution: "lodash@npm:4.17.21"

"my-app@workspace:.":
  version: 0.0.0-use.local
  resolution: "my-app@workspace:."
"""

PNPM_V9 = """lockfileVersion: '9.0'

importers:
  .:
    dependencies:
      lodash:
        specifier: ^4.17.20
        version: 4.17.21

packages:

  '@babel/core@7.24.0':
    resolution: {integrity: sha512-x}

  lodash@4.17.21:
    resolution: {integrity: sha512-y}

snapshots:

  ignored@1.0.0: {}
"""

PNPM_V6 = """lockfileVersion: '6.0'

packages:
  /@babel/core@7.0.0(supports-color@5.5.0):
    resolution: {integrity: sha512-x}
  /left-pad@1.3.0:
    resolution: {integrity: sha512-y}
"""

PNPM_V5 = """lockfileVersion: 5.4

packages:
  /@babel/core/7.0.0:
    resolution: {integrity: sha512-x}
  /react-dom/17.0.2_react@17.0.2:
    resolution: {integrity: sha512-y}
  github.com/owner/repo/abcdef:
    resolution: {tarball: x}
"""

GRADLE_LOCK = """# This is a Gradle generated file for dependency locking.
# Manual edits can break the build and are not advised.
com.google.guava:guava:31.1-jre=compileClasspath,runtimeClasspath
org.slf4j:slf4j-api:2.0.9=runtimeClasspath
empty=annotationProcessor
"""


PYLOCK = """lock-version = "1.0"
created-by = "pip"

[[packages]]
name = "django"
version = "1.11.0"

[[packages.wheels]]
name = "Django-1.11-py2.py3-none-any.whl"
url = "https://files.example/Django-1.11-py2.py3-none-any.whl"

[[packages]]
name = "urllib3"
version = "1.24.1"

[packages.sdist]
name = "urllib3-1.24.1.tar.gz"
url = "https://files.example/urllib3-1.24.1.tar.gz"
"""

CARGO_WITH_PATCH = """[[package]]
name = "anyhow"
version = "1.0.75"

[[package]]
name = "time"
version = "0.1.43"
dependencies = [
 "libc",
]

[[patch.unused]]
name = "foo"
version = "0.1.0"
"""

GO_MOD_BLOCKS = """module example.com/app

go 1.21

require (
\tgithub.com/gin-gonic/gin v1.6.0
\tgolang.org/x/net v0.17.0 // indirect
\texample.com/local v1.0.0
\texample.com/pinned v1.0.0
\texample.com/broken v1.0.0
)

require example.com/single v2.0.0

exclude (
\tgolang.org/x/net v0.7.0
)

exclude golang.org/x/text v0.3.0

replace (
\tgithub.com/gin-gonic/gin => github.com/myfork/gin v1.9.1
\texample.com/local => ../local
\texample.com/pinned v0.9.0 => example.com/fork v0.9.1
)

replace example.com/broken v1.0.0 => example.com/fixed
retract v1.0.1
"""


def coordinates(packages):
    return {(item.ecosystem, item.name, item.version) for item in packages}


class LockfileFormatTests(unittest.TestCase):
    def test_package_lock_v1_reads_the_nested_dependency_tree(self):
        text = json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "express": {
                        "version": "4.17.1",
                        "dependencies": {"qs": {"version": "6.7.0"}},
                    },
                    "broken": "not a mapping",
                    "lodash": {"version": "4.17.20"},
                },
            }
        )
        reading = scanner.read_lockfile("package-lock.json", text, "package-lock.json")
        self.assertEqual(
            coordinates(reading.packages),
            {
                ("npm", "express", "4.17.1"),
                ("npm", "qs", "6.7.0"),
                ("npm", "lodash", "4.17.20"),
            },
        )
        self.assertTrue(reading.lists_dependencies)
        self.assertFalse(reading.unreadable)

    def test_package_lock_v2_prefers_the_flat_packages_map(self):
        text = json.dumps(
            {
                "lockfileVersion": 2,
                "packages": {
                    "": {"name": "app"},
                    "node_modules/a": {"version": "1.0.0"},
                    "node_modules/a/node_modules/b": {"version": "2.0.0"},
                    "packages/local": {"version": "0.0.1"},
                },
                "dependencies": {"stale": {"version": "9.9.9"}},
            }
        )
        reading = scanner.read_lockfile("npm-shrinkwrap.json", text, "x")
        self.assertEqual(
            coordinates(reading.packages),
            {("npm", "a", "1.0.0"), ("npm", "b", "2.0.0")},
        )

    def test_package_lock_without_dependencies_lists_nothing(self):
        text = json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "app"}}})
        reading = scanner.read_lockfile("package-lock.json", text, "x")
        self.assertEqual(reading.packages, ())
        self.assertFalse(reading.lists_dependencies)
        self.assertEqual(
            scanner.read_lockfile("package-lock.json", "[]", "x").packages, ()
        )

    def test_listed_packages_without_versions_are_unreadable(self):
        text = json.dumps(
            {"packages": {"": {}, "node_modules/x": {"resolved": "file:x"}}}
        )
        reading = scanner.read_lockfile("package-lock.json", text, "x")
        self.assertTrue(reading.unreadable)

    def test_yarn_classic_and_berry_lockfiles(self):
        classic = scanner.read_lockfile("yarn.lock", YARN_CLASSIC, "yarn.lock")
        self.assertEqual(
            coordinates(classic.packages),
            {("npm", "@babel/code-frame", "7.12.13"), ("npm", "lodash", "4.17.21")},
        )
        berry = scanner.read_lockfile("yarn.lock", YARN_BERRY, "yarn.lock")
        self.assertEqual(coordinates(berry.packages), {("npm", "lodash", "4.17.21")})
        self.assertTrue(berry.lists_dependencies)

    def test_yarn_lockfile_with_only_a_workspace_lists_nothing(self):
        text = '__metadata:\n  version: 6\n\n"app@workspace:.":\n  version: 0.0.0\n'
        reading = scanner.read_lockfile("yarn.lock", text, "yarn.lock")
        self.assertEqual(reading.packages, ())
        self.assertFalse(reading.lists_dependencies)
        self.assertIsNone(scanner._yarn_name("noversion:"))

    def test_pnpm_lockfiles_of_each_key_style(self):
        expected = {
            PNPM_V9: {("npm", "@babel/core", "7.24.0"), ("npm", "lodash", "4.17.21")},
            PNPM_V6: {("npm", "@babel/core", "7.0.0"), ("npm", "left-pad", "1.3.0")},
            PNPM_V5: {("npm", "@babel/core", "7.0.0"), ("npm", "react-dom", "17.0.2")},
        }
        for text, wanted in expected.items():
            with self.subTest(text=text[:25]):
                reading = scanner.read_lockfile("pnpm-lock.yaml", text, "pnpm")
                self.assertEqual(coordinates(reading.packages), wanted)
                self.assertTrue(reading.lists_dependencies)

    def test_gradle_lockfiles_use_maven_coordinates(self):
        reading = scanner.read_lockfile(
            "buildscript-gradle.lockfile", GRADLE_LOCK, "gradle.lockfile"
        )
        self.assertEqual(
            coordinates(reading.packages),
            {
                ("Maven", "com.google.guava:guava", "31.1-jre"),
                ("Maven", "org.slf4j:slf4j-api", "2.0.9"),
            },
        )
        self.assertTrue(reading.lists_dependencies)

    def test_requirement_ranges_do_not_count_as_listed_versions(self):
        reading = scanner.read_lockfile(
            "requirements.txt", "requests>=2\n# flask==1\n", "requirements.txt"
        )
        self.assertEqual(reading.packages, ())
        self.assertFalse(reading.unreadable)

    def test_lockfile_classification_and_unknown_names(self):
        self.assertEqual(scanner.classify_lockfile("yarn.lock"), "supported")
        self.assertEqual(scanner.classify_lockfile("requirements-dev.txt"), "supported")
        self.assertEqual(scanner.classify_lockfile("gradle.lockfile"), "supported")
        self.assertEqual(scanner.classify_lockfile("bun.lockb"), "unsupported")
        self.assertIsNone(scanner.classify_lockfile("package.json"))
        with self.assertRaisesRegex(ValueError, "no parser"):
            scanner.read_lockfile("package.json", "{}", "package.json")
        with self.assertRaises(ValueError):
            scanner.read_lockfile("package-lock.json", "{not json", "x")

    def test_requirement_pins_allow_spaces_and_triple_equals(self):
        text = (
            "flask==2.3.3\nDjango == 1.11.0\nurllib3===1.24.1\n"
            "requests[socks] == 2.31.0 ; python_version > '3'\n"
            "wild==1.*\nranged==1.0,!=1.0.1\nloose>=2\n"
        )
        reading = scanner.read_lockfile("requirements.txt", text, "requirements.txt")
        self.assertEqual(
            coordinates(reading.packages),
            {
                ("PyPI", "flask", "2.3.3"),
                ("PyPI", "Django", "1.11.0"),
                ("PyPI", "urllib3", "1.24.1"),
                ("PyPI", "requests", "2.31.0"),
            },
        )
        spaced = scanner.read_lockfile("requirements.txt", "a == 1.0\n", "r")
        self.assertTrue(spaced.lists_dependencies)

    def test_toml_lockfiles_ignore_keys_of_nested_tables(self):
        pylock = scanner.read_lockfile("pylock.toml", PYLOCK, "pylock.toml")
        self.assertEqual(
            coordinates(pylock.packages),
            {("PyPI", "django", "1.11.0"), ("PyPI", "urllib3", "1.24.1")},
        )
        cargo = scanner.read_lockfile("Cargo.lock", CARGO_WITH_PATCH, "Cargo.lock")
        self.assertEqual(
            coordinates(cargo.packages),
            {("crates.io", "anyhow", "1.0.75"), ("crates.io", "time", "0.1.43")},
        )

    def test_go_mod_reads_require_after_replace_and_ignores_exclude(self):
        reading = scanner.read_lockfile("go.mod", GO_MOD_BLOCKS, "go.mod")
        self.assertEqual(
            coordinates(reading.packages),
            {
                ("Go", "github.com/myfork/gin", "1.9.1"),
                ("Go", "golang.org/x/net", "0.17.0"),
                ("Go", "example.com/pinned", "1.0.0"),
                ("Go", "example.com/single", "2.0.0"),
            },
        )

    def test_npm_workspace_links_are_not_packages(self):
        text = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "workspaces": ["packages/*"]},
                    "node_modules/a": {"resolved": "packages/a", "link": True},
                    "packages/a": {"version": "1.0.0"},
                },
            }
        )
        reading = scanner.read_lockfile("package-lock.json", text, "x")
        self.assertEqual(reading.packages, ())
        self.assertFalse(reading.lists_dependencies)
        self.assertFalse(reading.unreadable)
        single = '{"packages": {"node_modules/x": {"version": "1.0.0"}}}'
        reading = scanner.read_lockfile("package-lock.json", single, "x")
        self.assertEqual(coordinates(reading.packages), {("npm", "x", "1.0.0")})

    def test_coordinate_ignores_the_lockfile_path(self):
        first = scanner.LockedPackage("npm", "a", "1", "one.lock")
        second = scanner.LockedPackage("npm", "a", "1", "two.lock")
        self.assertEqual(first.coordinate, second.coordinate)


class OsvQueryTests(unittest.TestCase):
    def test_batch_response_maps_vulnerability_ids_to_package_locations(self):
        packages = (
            scanner.LockedPackage("npm", "lodash", "4.17.20", "package-lock.json"),
            scanner.LockedPackage("PyPI", "requests", "2.31.0", "requirements.txt"),
        )

        def request(_request, timeout):
            self.assertEqual(timeout, 20)
            return scanner.BytesResponse(
                json.dumps(
                    {
                        "results": [
                            {"vulns": [{"id": "GHSA-example"}]},
                            {},
                        ]
                    }
                ).encode()
            )

        findings = scanner.query_osv(packages, opener=request)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].vulnerability_id, "GHSA-example")
        self.assertEqual(findings[0].package, packages[0])

    def test_invalid_response_fails_explicitly(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        with self.assertRaisesRegex(ValueError, "result count"):
            scanner.query_osv(
                (package,),
                opener=lambda _request, timeout: scanner.BytesResponse(
                    b'{"results":[]}'
                ),
            )

    def test_empty_query_and_invalid_vulnerability_rows_are_safe(self):
        self.assertEqual(scanner.query_osv(()), ())
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")

        def request(_request, timeout):
            return scanner.BytesResponse(
                json.dumps(
                    {
                        "results": [
                            {
                                "vulns": [
                                    {"id": "GHSA-one"},
                                    {"id": ""},
                                    {"no-id": True},
                                    "invalid",
                                    {"id": "GHSA-one"},
                                ]
                            }
                        ]
                    }
                ).encode()
            )

        findings = scanner.query_osv((package,), opener=request)
        self.assertEqual(findings, (scanner.VulnerabilityFinding("GHSA-one", package),))
        self.assertEqual(scanner._vulnerability_ids({"vulns": "invalid"}), ())
        self.assertEqual(scanner._vulnerability_ids("invalid"), ())

    def test_large_response_and_non_list_results_fail_explicitly(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            scanner.query_osv(
                (package,),
                opener=lambda _request, timeout: scanner.BytesResponse(
                    b"x" * 10_000_001
                ),
            )
        with self.assertRaisesRegex(ValueError, "result count"):
            scanner.query_osv(
                (package,),
                opener=lambda _request, timeout: scanner.BytesResponse(
                    b'{"results":{}}'
                ),
            )

    def test_query_batches_more_than_one_thousand_packages(self):
        packages = tuple(
            scanner.LockedPackage("npm", f"package-{index}", "1", "lock")
            for index in range(1_001)
        )
        batch_sizes = []

        def request(request, timeout):
            queries = json.loads(request.data)["queries"]
            batch_sizes.append(len(queries))
            return scanner.BytesResponse(
                json.dumps({"results": [{} for _ in queries]}).encode()
            )

        self.assertEqual(scanner.query_osv(packages, opener=request), ())
        self.assertEqual(batch_sizes, [1_000, 1])

    def test_system_curl_securely_falls_back_when_python_https_fails(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        completed = scanner.subprocess.CompletedProcess([], 0, b'{"results":[{}]}', b"")
        with (
            mock.patch.object(scanner, "urlopen", side_effect=OSError("bad CA")),
            mock.patch.object(scanner.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(scanner.subprocess, "run", return_value=completed) as run,
        ):
            findings = scanner.query_osv((package,), opener=scanner.urlopen)

        self.assertEqual(findings, ())
        command = run.call_args.args[0]
        self.assertNotIn("-k", command)
        self.assertIn("--max-filesize", command)
        self.assertIsNotNone(run.call_args.kwargs["input"])

    def test_curl_fallback_errors_remain_explicit(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        with (
            mock.patch.object(scanner, "urlopen", side_effect=OSError("bad CA")),
            mock.patch.object(scanner.shutil, "which", return_value=None),
            self.assertRaisesRegex(OSError, "unavailable"),
        ):
            scanner.query_osv((package,), opener=scanner.urlopen)

        completed = scanner.subprocess.CompletedProcess([], 22, b"", b"denied")
        with (
            mock.patch.object(scanner, "urlopen", side_effect=OSError("bad CA")),
            mock.patch.object(scanner.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(scanner.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(OSError, "denied"),
        ):
            scanner.query_osv((package,), opener=scanner.urlopen)

    def test_protocol_errors_also_fall_back_to_curl(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        completed = scanner.subprocess.CompletedProcess([], 0, b'{"results":[{}]}', b"")
        broken = http.client.IncompleteRead(b"x", 100)
        with (
            mock.patch.object(scanner, "urlopen", side_effect=broken),
            mock.patch.object(scanner.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(scanner.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(scanner.query_osv((package,), opener=scanner.urlopen), ())

    def test_a_hung_curl_raises_os_error(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        hung = scanner.subprocess.TimeoutExpired(["curl"], 25)
        with (
            mock.patch.object(scanner, "urlopen", side_effect=OSError("bad CA")),
            mock.patch.object(scanner.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(scanner.subprocess, "run", side_effect=hung),
            self.assertRaisesRegex(OSError, "curl did not finish"),
        ):
            scanner.query_osv((package,), opener=scanner.urlopen)

    def test_custom_openers_do_not_trigger_process_fallback(self):
        package = scanner.LockedPackage("npm", "x", "1", "package-lock.json")
        custom = mock.Mock(side_effect=OSError("offline"))
        with (
            mock.patch.object(scanner.subprocess, "run") as run,
            self.assertRaisesRegex(OSError, "offline"),
        ):
            scanner.query_osv((package,), opener=custom)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
