from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import gate_types  # noqa: E402
import gate_vulnerabilities as gate  # noqa: E402
from portable_vulnerabilities import (  # noqa: E402
    BytesResponse,
    VulnerabilityFinding,
    query_osv,
)

REPOSITORY = diff_scope.repository_changes()


def package_lock(**versions: str) -> str:
    packages = {"": {"name": "app"}}
    for name, version in versions.items():
        packages[f"node_modules/{name}"] = {"version": version}
    return json.dumps({"lockfileVersion": 3, "packages": packages}, indent=2)


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def commit_all(root: Path) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")


class RecordingQuery:
    """An OSV stand-in: records each query and reports advisories by name."""

    def __init__(self, advisories=None, error=None):
        self.advisories = advisories or {}
        self.error = error
        self.calls = []

    def __call__(self, packages):
        self.calls.append(packages)
        if self.error is not None:
            raise self.error
        return tuple(
            VulnerabilityFinding(self.advisories[item.name], item)
            for item in packages
            if item.name in self.advisories
        )

    def queried_names(self):
        return sorted(item.name for call in self.calls for item in call)


def node_project(root: Path, **versions: str) -> None:
    dependencies = {name: f"^{version}" for name, version in versions.items()}
    write(root, "package.json", json.dumps({"dependencies": dependencies}))
    write(root, "package-lock.json", package_lock(**versions))


MEASUREMENT_KEYS = {
    "packages_checked",
    "lockfiles",
    "unsupported",
    "findings",
    "vulnerabilities",
}


def npm_workspace(root: Path) -> None:
    """Root package.json with workspaces, one member with a dependency, and
    the lockfile npm writes: the member folder and its link are listed."""
    write(root, "package.json", json.dumps({"workspaces": ["packages/*"]}))
    member = {"name": "a", "dependencies": {"lodash": "^4.17.0"}}
    write(root, "packages/a/package.json", json.dumps(member))
    packages = {
        "": {"name": "root", "workspaces": ["packages/*"]},
        "node_modules/a": {"resolved": "packages/a", "link": True},
        "node_modules/lodash": {"version": "4.17.21"},
        "packages/a": {"name": "a", "dependencies": {"lodash": "^4.17.0"}},
    }
    write(root, "package-lock.json", json.dumps({"packages": packages}, indent=2))


CARGO_LOCK = """version = 3

[[package]]
name = "app"
version = "0.1.0"
dependencies = ["serde"]

[[package]]
name = "serde"
version = "1.0.190"
"""


def cargo_workspace(root: Path) -> None:
    write(root, "Cargo.toml", '[workspace]\nmembers = ["app"]\n')
    write(
        root,
        "app/Cargo.toml",
        '[package]\nname = "app"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n',
    )
    write(root, "app/src/main.rs", "fn main() {}\n")
    write(root, "Cargo.lock", CARGO_LOCK)


class IncompleteResponse:
    """A urlopen response whose body ends early, as a dropped connection
    does."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum=-1):
        raise http.client.IncompleteRead(b"x", 100)


class RepositoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def evaluate(self, query=None, offline=False):
        query = query or RecordingQuery()
        return gate.evaluate_vulnerabilities(self.root, REPOSITORY, query, offline)

    def test_repository_without_dependency_files_is_not_applicable(self):
        write(self.root, "main.py", "print('hi')\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.NOT_APPLICABLE)
        self.assertEqual(outcome.key, "vulnerabilities")
        self.assertEqual(outcome.title, "Vulnerable dependencies")
        self.assertEqual(set(outcome.measurements), MEASUREMENT_KEYS)
        self.assertEqual(outcome.measurements["findings"], 0)
        self.assertEqual(outcome.measurements["vulnerabilities"], [])

    def test_known_vulnerability_fails_with_an_actionable_item(self):
        node_project(self.root, lodash="4.17.20", express="4.18.2")
        query = RecordingQuery({"lodash": "GHSA-35jh-r3h4-6jhm"})
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(query.queried_names(), ["express", "lodash"])
        [item] = outcome.items
        self.assertEqual(item.path, "package-lock.json")
        self.assertEqual(item.value, "GHSA-35jh-r3h4-6jhm")
        self.assertIn("lodash@4.17.20 (npm)", item.name)
        self.assertIn("1 known vulnerability and 0 unlocked", outcome.summary)
        self.assertEqual(outcome.measurements["findings"], 1)
        self.assertEqual(
            outcome.measurements["vulnerabilities"][0]["package"], "lodash"
        )

    def test_clean_lockfiles_pass_and_each_version_is_queried_once(self):
        node_project(self.root, lodash="4.17.21")
        write(self.root, "tools/package.json", '{"dependencies": {"lodash": "4"}}')
        write(self.root, "tools/package-lock.json", package_lock(lodash="4.17.21"))
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)
        self.assertEqual(query.queried_names(), ["lodash"])
        self.assertIn("1 locked package version from 2 lockfiles", outcome.summary)
        self.assertEqual(outcome.measurements["packages_checked"], 1)

    def test_manifest_without_dependencies_passes_without_a_query(self):
        write(self.root, "package.json", '{"name": "app"}')
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(outcome.summary, "No locked package versions to check.")
        self.assertEqual(query.calls, [])

    def test_unlocked_manifests_fail_and_ask_for_a_lockfile(self):
        write(self.root, "package.json", '{"dependencies": {"left-pad": "^1"}}')
        write(self.root, "api/pyproject.toml", '[project]\ndependencies = ["x>=1"]\n')
        write(self.root, "api/requirements.txt", "requests>=2\n")
        write(self.root, "Gemfile", 'gem "rails"\n')
        write(self.root, "svc/go.mod", "module svc\nrequire a.io/b v1.0.0\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        unlocked = sorted(item.path for item in outcome.items)
        self.assertEqual(
            unlocked,
            [
                "Gemfile",
                "api/pyproject.toml",
                "api/requirements.txt",
                "package.json",
                "svc/go.mod",
            ],
        )
        for item in outcome.items:
            self.assertEqual(
                (item.metric, item.value), ("dependency versions", "not locked")
            )

    def test_pinned_requirements_lock_a_python_project(self):
        write(self.root, "pyproject.toml", '[project]\ndependencies = ["a>=1"]\n')
        write(self.root, "requirements.txt", "a==1.2.0\n")
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)
        self.assertEqual(query.queried_names(), ["a"])

    def test_library_crate_without_lockfile_is_a_note(self):
        write(
            self.root, "Cargo.toml", '[package]\nname = "x"\n[dependencies]\nb = "1"\n'
        )
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertTrue(any("library without a lockfile" in d for d in outcome.details))

    def test_ecosystems_without_a_readable_lockfile_block(self):
        write(self.root, "pom.xml", "<project><dependency></dependency></project>")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.BLOCKED)
        self.assertIn("pom.xml", outcome.summary)
        self.assertIn("cannot be read without a lockfile", outcome.summary)

    def test_unsupported_lockfile_formats_block_explicitly(self):
        write(self.root, "package.json", '{"dependencies": {"a": "1"}}')
        write(self.root, "bun.lock", "{}\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.BLOCKED)
        self.assertEqual(outcome.measurements["unsupported"], ["bun.lock"])
        self.assertIn("bun.lock", outcome.summary)

    def test_gradle_lockfile_supplies_maven_versions(self):
        write(
            self.root, "build.gradle", "dependencies {\n  implementation 'a:b:1'\n}\n"
        )
        write(self.root, "gradle.lockfile", "a:b:1.0=runtimeClasspath\n")
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)
        self.assertEqual(query.queried_names(), ["a:b"])

    def test_lockfiles_that_cannot_be_read_fail(self):
        write(self.root, "package-lock.json", "{not json")
        write(
            self.root, "web/package-lock.json", '{"packages": {"node_modules/x": {}}}'
        )
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        paths = sorted(item.path for item in outcome.items)
        self.assertEqual(paths, ["package-lock.json", "web/package-lock.json"])
        for item in outcome.items:
            self.assertEqual(item.metric, "packages read")
            self.assertTrue(item.value.startswith("none ("))

    def test_malformed_manifest_fails(self):
        write(self.root, "package.json", "{oops")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(outcome.items[0].metric, "manifest")

    def test_unreadable_files_block(self):
        write(self.root, "package.json", '{"dependencies": {"a": "1"}}')
        write(self.root, "package-lock.json", package_lock(a="1.0.0"))
        write(self.root, "requirements.txt", "a==1\n")
        for name in ("package.json", "package-lock.json", "requirements.txt"):
            os.chmod(self.root / name, 0)
        try:
            outcome = self.evaluate()
        finally:
            for name in ("package.json", "package-lock.json", "requirements.txt"):
                os.chmod(self.root / name, 0o644)
        self.assertEqual(outcome.status, gate_types.BLOCKED)
        self.assertIn("could not be read", outcome.summary)

    def test_offline_and_network_failures_block(self):
        node_project(self.root, lodash="4.17.21")
        offline = self.evaluate(offline=True)
        self.assertEqual(offline.status, gate_types.BLOCKED)
        self.assertIn("Offline", offline.summary)
        failing = RecordingQuery(error=OSError("network unreachable"))
        blocked = self.evaluate(failing)
        self.assertEqual(blocked.status, gate_types.BLOCKED)
        self.assertIn("network unreachable", blocked.summary)
        self.assertEqual(blocked.measurements["packages_checked"], 0)

    def test_findings_still_fail_when_another_file_is_blocked(self):
        node_project(self.root, lodash="4.17.20")
        write(self.root, "tool/package.json", '{"dependencies": {"a": "1"}}')
        write(self.root, "tool/bun.lockb", "binary")
        outcome = self.evaluate(RecordingQuery({"lodash": "GHSA-1"}))
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertTrue(any("tool/bun.lockb" in d for d in outcome.details))

    def test_dependency_files_in_fixture_folders_are_samples(self):
        write(self.root, "tests/fixtures/package.json", '{"dependencies": {"a": "1"}}')
        write(self.root, "pkg/testdata/requirements.txt", "flask>=2\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.NOT_APPLICABLE)

    def test_test_suite_dependencies_are_real_dependencies(self):
        playwright = {"devDependencies": {"@playwright/test": "^1.40"}}
        write(self.root, "e2e/package.json", json.dumps(playwright))
        write(self.root, "tests/requirements.txt", "pytest>=7\n")
        write(self.root, "requirements.txt", "flask==2.0.0\n")
        write(self.root, "requirements_test.txt", "pytest>=7\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            sorted(item.path for item in outcome.items),
            ["e2e/package.json", "requirements_test.txt", "tests/requirements.txt"],
        )

    def test_spaced_and_triple_equals_pins_are_locked_and_queried(self):
        write(self.root, "requirements.txt", "Django == 4.2.7\nflask===2.3.3\n")
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)
        self.assertEqual(query.queried_names(), ["Django", "flask"])
        self.assertEqual(
            sorted(item.version for call in query.calls for item in call),
            ["2.3.3", "4.2.7"],
        )

    def test_one_unpinned_requirement_unlocks_the_file(self):
        write(self.root, "requirements.txt", "flask==2.3.3\ndjango<1.12\n")
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual([item.path for item in outcome.items], ["requirements.txt"])
        self.assertEqual(query.queried_names(), ["flask"])

    def test_a_pinned_sibling_does_not_lock_another_requirements_file(self):
        write(self.root, "requirements.txt", "flask==2.3.3\n")
        write(self.root, "requirements-dev.txt", "django<1.12\n")
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items], ["requirements-dev.txt"]
        )

    def test_a_partly_pinned_requirements_file_does_not_lock_pyproject(self):
        write(self.root, "pyproject.toml", '[project]\ndependencies = ["a>=1"]\n')
        write(self.root, "requirements.txt", "a==1.2.0\nb>=2\n")
        outcome = self.evaluate()
        self.assertEqual(
            sorted(item.path for item in outcome.items),
            ["pyproject.toml", "requirements.txt"],
        )

    def test_parent_lockfile_of_another_project_does_not_lock_a_new_package(self):
        node_project(self.root, a="1.0.0")
        write(self.root, "tools/x/package.json", '{"dependencies": {"lodash": "4"}}')
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items], ["tools/x/package.json"]
        )

    def test_workspace_lockfile_in_the_root_locks_its_members(self):
        npm_workspace(self.root)
        cargo_workspace(self.root)
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)
        self.assertTrue({"lodash", "serde"}.issubset(query.queried_names()))

    def test_workspace_only_npm_lockfile_has_nothing_to_check(self):
        npm_workspace(self.root)
        write(self.root, "packages/a/package.json", '{"name": "a"}')
        lock = {
            "packages": {
                "": {"name": "root"},
                "node_modules/a": {"resolved": "packages/a", "link": True},
                "packages/a": {"version": "1.0.0"},
            }
        }
        write(self.root, "package-lock.json", json.dumps(lock))
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)

    def test_protocol_errors_from_osv_block_instead_of_crashing(self):
        node_project(self.root, lodash="4.17.21")

        def broken_connection(packages):
            return query_osv(packages, opener=lambda *_a, **_k: IncompleteResponse())

        outcome = self.evaluate(broken_connection)
        self.assertEqual(outcome.status, gate_types.BLOCKED)
        self.assertIn("IncompleteRead", outcome.summary)

    def test_curl_timeouts_block(self):
        node_project(self.root, lodash="4.17.21")
        failing = RecordingQuery(error=subprocess.TimeoutExpired(["curl"], 25))
        outcome = self.evaluate(failing)
        self.assertEqual(outcome.status, gate_types.BLOCKED)

    def test_real_osv_adapter_reports_findings(self):
        node_project(self.root, lodash="4.17.20")
        payload = json.dumps({"results": [{"vulns": [{"id": "GHSA-real"}]}]})

        def recorded(packages):
            return query_osv(
                packages, opener=lambda *_a, **_k: BytesResponse(payload.encode())
            )

        outcome = self.evaluate(recorded)
        self.assertEqual([item.value for item in outcome.items], ["GHSA-real"])


class IncrementalScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_repository(self.root)
        node_project(self.root, lodash="4.17.20", express="4.18.2")
        write(self.root, "src/app.js", "module.exports = 1;\n")
        commit_all(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def evaluate(self, query):
        changes = diff_scope.local_changes(self.root)
        return gate.evaluate_vulnerabilities(self.root, changes, query)

    def test_change_without_dependency_files_passes_without_a_query(self):
        write(self.root, "src/app.js", "module.exports = 2;\n")
        query = RecordingQuery({"lodash": "GHSA-old"})
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(outcome.summary, "No dependency changes.")
        self.assertEqual(query.calls, [])
        self.assertEqual(set(outcome.measurements), MEASUREMENT_KEYS)

    def test_only_new_or_upgraded_packages_are_queried(self):
        write(
            self.root,
            "package-lock.json",
            package_lock(lodash="4.17.20", express="4.19.0", qs="6.11.0"),
        )
        query = RecordingQuery({"lodash": "GHSA-old", "qs": "GHSA-new"})
        outcome = self.evaluate(query)
        self.assertEqual(query.queried_names(), ["express", "qs"])
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual([item.value for item in outcome.items], ["GHSA-new"])

    def test_removing_packages_leaves_nothing_to_check(self):
        write(self.root, "package-lock.json", package_lock(lodash="4.17.20"))
        query = RecordingQuery()
        outcome = self.evaluate(query)
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertEqual(
            outcome.summary, "No new or changed locked package versions to check."
        )
        self.assertEqual(query.calls, [])

    def test_clean_new_packages_pass(self):
        write(
            self.root,
            "package-lock.json",
            package_lock(lodash="4.17.21", express="4.18.2"),
        )
        outcome = self.evaluate(RecordingQuery())
        self.assertEqual(outcome.status, gate_types.PASS)
        self.assertIn(
            "1 new or changed locked package version from 1 lockfile", outcome.summary
        )

    def test_previously_unreadable_lockfile_counts_every_package_as_new(self):
        write(self.root, "package-lock.json", "{broken")
        commit_all(self.root)
        write(self.root, "package-lock.json", package_lock(lodash="4.17.21"))
        query = RecordingQuery()
        self.evaluate(query)
        self.assertEqual(query.queried_names(), ["lodash"])

    def test_deleting_the_lockfile_unlocks_the_manifest(self):
        (self.root / "package-lock.json").unlink()
        outcome = self.evaluate(RecordingQuery())
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual([item.path for item in outcome.items], ["package.json"])

    def test_new_manifest_elsewhere_is_checked_but_untouched_ones_are_not(self):
        write(self.root, "legacy/Gemfile", 'gem "rails"\n')
        commit_all(self.root)
        write(self.root, "worker/requirements.txt", "celery>=5\n")
        outcome = self.evaluate(RecordingQuery())
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items], ["worker/requirements.txt"]
        )


class WorkspaceIncrementalTests(unittest.TestCase):
    """Deleting a workspace lockfile in the root unlocks members in
    sub-folders, which an incremental run must notice."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_repository(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def evaluate(self):
        changes = diff_scope.local_changes(self.root)
        return gate.evaluate_vulnerabilities(self.root, changes, RecordingQuery())

    def test_deleting_the_root_npm_lockfile_unlocks_members(self):
        npm_workspace(self.root)
        commit_all(self.root)
        (self.root / "package-lock.json").unlink()
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual(
            [item.path for item in outcome.items], ["packages/a/package.json"]
        )

    def test_deleting_the_root_cargo_lockfile_unlocks_members(self):
        cargo_workspace(self.root)
        commit_all(self.root)
        (self.root / "Cargo.lock").unlink()
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.FAIL)
        self.assertEqual([item.path for item in outcome.items], ["app/Cargo.toml"])

    def test_a_change_in_a_member_does_not_recheck_siblings(self):
        npm_workspace(self.root)
        write(self.root, "packages/b/package.json", '{"dependencies": {"x": "1"}}')
        commit_all(self.root)
        write(self.root, "packages/a/package.json", '{"name": "a"}')
        outcome = self.evaluate()
        self.assertEqual(outcome.status, gate_types.PASS, outcome.details)


class HelperTests(unittest.TestCase):
    def test_unique_packages_keep_the_first_source(self):
        first = gate.LockedPackage("npm", "a", "1", "one/package-lock.json")
        second = gate.LockedPackage("npm", "a", "1", "two/package-lock.json")
        self.assertEqual(gate.unique_packages([first, second]), (first,))

    def test_changed_dependency_files_include_deletions(self):
        changes = diff_scope.ChangeSet(
            diff_scope.LOCAL_CHANGES,
            "HEAD",
            {"src/a.py": diff_scope.FileChange("src/a.py")},
            frozenset({"svc/go.sum"}),
        )
        self.assertEqual(gate.changed_dependency_files(changes), {"svc/go.sum"})


if __name__ == "__main__":
    unittest.main()
