import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
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

            inventory = scanner.discover_locked_packages(root)

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
            (root / "yarn.lock").write_text("left-pad@1.0.0:\n", encoding="utf-8")
            inventory = scanner.discover_locked_packages(root)
            self.assertEqual(inventory.packages, ())
            self.assertEqual(inventory.unsupported, ("yarn.lock",))

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

            inventory = scanner.discover_locked_packages(root)
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
            inventory = scanner.discover_locked_packages(root)
            self.assertEqual(inventory.packages, ())
            self.assertEqual(len(inventory.lockfiles), len(cases))

    def test_locked_coordinate_validation_is_explicit(self):
        self.assertIsNone(scanner._locked("npm", None, "1", "lock"))
        self.assertIsNone(scanner._locked("npm", "x", None, "lock"))
        self.assertIsNone(scanner._locked("npm", " ", "1", "lock"))
        self.assertIsNone(scanner._locked("npm", "x", "latest", "lock"))
        item = scanner._locked("Go", "example.com/x", "v1.2.0", "go.mod")
        self.assertEqual(item.version, "1.2.0")


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
