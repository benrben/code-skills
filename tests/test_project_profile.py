import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/code-discipline/scripts/project_profile.py"


def load_module():
    spec = importlib.util.spec_from_file_location("project_profile_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load project profile module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


profile = load_module()


def write(root, relative, content=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fingerprint(root):
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        result[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


class ProjectDetectionTests(unittest.TestCase):
    def test_detects_python_service_and_asks_three_relevant_questions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(
                root,
                "pyproject.toml",
                """
[project]
dependencies = ["fastapi", "psycopg", "opentelemetry-api", "pytest"]
[project.scripts]
serve = "app.main:run"
""",
            )
            write(
                root,
                "main.py",
                'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/health")\ndef health(): return {}\n',
            )
            write(root, "tests/test_app.py", "def test_app(): assert True\n")
            write(root, "openapi.yaml", "openapi: 3.1.0\n")
            write(root, "migrations/001.sql", "select 1;\n")
            write(root, "Dockerfile", "FROM scratch\n")
            write(root, "infra/main.tf", "terraform {}\n")
            write(root, "k8s/deployment.yaml", "kind: Deployment\n")
            write(root, ".github/workflows/ci.yml", "jobs: {}\n")

            result = profile.inspect_project(root)

            self.assertEqual(result.fact("project_type").value, ["api", "cli"])
            self.assertEqual(result.fact("languages").value, ["Python"])
            self.assertIn("FastAPI", result.fact("frameworks").value)
            self.assertEqual(result.fact("test_commands").value, ["python3 -m pytest"])
            self.assertIn("serve: app.main:run", result.fact("entrypoints").value)
            self.assertEqual(result.fact("api_specs").value, ["openapi.yaml"])
            self.assertIn("PostgreSQL", result.fact("databases").value)
            self.assertEqual(
                result.fact("infrastructure").value,
                ["Docker", "Kubernetes", "Terraform"],
            )
            self.assertEqual(result.fact("ci").value, ["GitHub Actions"])
            self.assertEqual(
                result.fact("observability").value,
                ["Health endpoint", "OpenTelemetry"],
            )
            self.assertIn("Docker", result.fact("deployment_targets").value)
            self.assertEqual(
                [question.category for question in result.questions],
                ["product", "runtime", "risk"],
            )
            self.assertEqual(
                [question.key for question in result.questions],
                [
                    "critical_user_stories",
                    "observability_requirements",
                    "data_safety",
                ],
            )
            self.assertNotIn("languages", {item.key for item in result.questions})
            self.assertEqual(
                result.dimensions["data_safety"]["status"], "needs_context"
            )
            self.assertTrue(result.dimensions["integration"]["applicable"])

    def test_detects_typescript_web_stack_and_package_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(
                root,
                "package.json",
                json.dumps(
                    {
                        "main": "dist/server.js",
                        "bin": {"tool": "dist/cli.js"},
                        "scripts": {"test": "vitest", "start": "node server.js"},
                        "dependencies": {
                            "react": "1",
                            "express": "1",
                            "mongoose": "1",
                            "@sentry/node": "1",
                        },
                    }
                ),
            )
            write(root, "pnpm-lock.yaml", "lockfileVersion: 9\n")
            write(root, "server.ts", "export const server = true;\n")
            write(root, "schema.graphql", "type Query { ok: Boolean }\n")
            write(root, "docker-compose.yml", "services: {}\n")
            write(root, "serverless.yml", "service: app\n")
            write(root, "vercel.json", "{}\n")
            write(root, ".gitlab-ci.yml", "test: {}\n")

            result = profile.detect_profile(root)

            self.assertEqual(
                result.fact("languages").value, ["JavaScript", "TypeScript"]
            )
            self.assertEqual(result.fact("project_type").value, ["api", "cli", "web"])
            self.assertEqual(result.fact("test_commands").value, ["pnpm test"])
            self.assertEqual(result.fact("api_specs").value, ["schema.graphql"])
            self.assertIn("MongoDB", result.fact("databases").value)
            self.assertEqual(result.fact("ci").value, ["GitLab CI"])
            self.assertIn("Sentry", result.fact("observability").value)
            self.assertIn(
                "Serverless platform", result.fact("deployment_targets").value
            )
            self.assertIn(
                "Application platform", result.fact("deployment_targets").value
            )
            self.assertTrue(result.dimensions["ui_accessibility"]["applicable"])

    def test_detects_offline_test_commands_for_compiled_ecosystems(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in {
                "go.mod": "module example.test/app\n",
                "Cargo.toml": "[package]\nname='app'\n",
                "pom.xml": "<project/>",
                "gradlew": "#!/bin/sh\n",
                "App.csproj": "<Project/>",
                "main.go": "package main\n",
                "src/main.rs": "fn main() {}\n",
            }.items():
                write(root, name, content)

            commands = profile.inspect_project(root).fact("test_commands").value

            self.assertEqual(
                commands,
                [
                    "./gradlew --offline test",
                    "cargo test --all --frozen",
                    "dotnet test --no-restore",
                    "go test -json ./...",
                    "mvn -o test",
                ],
            )

    def test_unknown_repository_asks_for_type_but_never_language_setup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, "README.md", "notes\n")

            result = profile.generate_project_profile(root)

            keys = [question.key for question in result.questions]
            self.assertEqual(keys, ["project_type", "security_level"])
            self.assertNotIn("languages", keys)
            self.assertEqual(
                result.dimensions["language_semantics"]["status"], "not_applicable"
            )
            self.assertEqual(
                result.dimensions["performance"]["status"], "not_applicable"
            )

    def test_library_has_measure_only_default_and_no_runtime_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, "Cargo.toml", "[package]\nname='lib'\n")
            write(root, "src/lib.rs", "pub fn value() -> i32 { 1 }\n")

            result = profile.inspect_project(root)

            self.assertEqual(result.fact("project_type").value, ["library"])
            self.assertEqual(result.fact("performance").value, {"mode": "measure_only"})
            self.assertEqual(result.fact("performance").source, "default")
            self.assertIn("Package registry", result.fact("deployment_targets").value)
            self.assertFalse(result.dimensions["infrastructure"]["applicable"])
            self.assertEqual(
                {question.category for question in result.questions},
                {"product", "risk"},
            )
            self.assertEqual(result.fact("data_safety").status, "not_applicable")

    def test_worker_without_entrypoint_requests_runtime_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(
                root,
                "pyproject.toml",
                '[project]\ndependencies = ["fastapi", "celery"]\n',
            )

            result = profile.inspect_project(root)

            self.assertEqual(result.fact("project_type").value, ["api", "worker"])
            self.assertEqual(result.questions[1].key, "entrypoints")
            self.assertEqual(
                result.dimensions["reliability"]["status"], "needs_context"
            )

    def test_unreadable_large_and_invalid_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root, "package.json", "not-json")
            write(root, "large.txt", "large")
            write(root, "migrations/001.sql", "select 1;\n")
            snapshot = profile.Snapshot(root, ("package.json", "large.txt"))

            self.assertEqual(snapshot.read("missing.txt"), "")
            self.assertEqual(snapshot.read("large.txt", limit=1), "")
            self.assertEqual(profile._json_file(snapshot, "package.json"), {})
            with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
                self.assertEqual(snapshot.read("large.txt"), "")

            with mock.patch.object(Path, "relative_to", side_effect=ValueError):
                self.assertEqual(profile._snapshot(root).paths, ())
            self.assertEqual(
                profile.inspect_project(root).fact("databases").value,
                ["Database engine not detected"],
            )


class ProfileLifecycleTests(unittest.TestCase):
    def make_service(self, root):
        write(
            root,
            "package.json",
            json.dumps(
                {
                    "main": "server.js",
                    "scripts": {"test": "jest"},
                    "dependencies": {"express": "1"},
                }
            ),
        )
        write(root, "server.js", "module.exports = {};\n")

    def test_confirmed_answers_are_preserved_and_not_asked_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_service(root)
            first = profile.inspect_project(root)
            answers = {
                "critical_user_stories": ["User can open the API"],
                "observability_requirements": ["logs", "health"],
                "security_level": "internal",
            }

            answered = profile.merge_answers(first, answers)
            rescanned = profile.inspect_project(root, answered.as_dict())

            for key, value in answers.items():
                self.assertEqual(rescanned.fact(key).value, value)
                self.assertEqual(rescanned.fact(key).source, "confirmed")
                self.assertNotIn(key, {item.key for item in rescanned.questions})
            self.assertEqual(rescanned.dimensions["product"]["status"], "ready")
            self.assertEqual(rescanned.dimensions["observability"]["status"], "ready")
            direct = profile.inspect_project(root, answered)
            self.assertEqual(direct.fact("security_level").source, "confirmed")

    def test_decide_later_remains_context_and_question_limit_is_respected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_service(root)
            first = profile.inspect_project(root, question_limit=1)
            self.assertEqual(len(first.questions), 1)

            later = profile.merge_answers(
                first, {first.questions[0].key: "Decide later"}, question_limit=1
            )

            self.assertEqual(later.questions[0].key, first.questions[0].key)
            self.assertEqual(later.fact(first.questions[0].key).source, "missing")

            empty = Path(temporary) / "empty"
            empty.mkdir()
            normalized = profile.merge_answers(
                profile.inspect_project(empty),
                {"project_type": "API", "critical_user_stories": "Call succeeds"},
            )
            self.assertEqual(normalized.fact("project_type").value, ["api"])
            self.assertEqual(
                normalized.fact("critical_user_stories").value, ["Call succeeds"]
            )

    def test_serialization_helpers_round_trip_profile_and_question_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            self.make_service(root)
            result = profile.inspect_project(root)
            output = root / profile.PROFILE_NAME

            written = profile.write_profile(output, result)
            loaded = profile.load_profile(written)
            rows = profile.profile_questions(loaded)

            self.assertEqual(
                json.loads(result.to_json())["facts"], result.as_dict()["facts"]
            )
            self.assertEqual(loaded.facts, result.facts)
            self.assertTrue(rows)
            self.assertEqual(
                set(rows[0]),
                {
                    "key",
                    "category",
                    "prompt",
                    "reason",
                    "options",
                    "recommended",
                    "required",
                },
            )
            self.assertTrue(rows[0]["required"])
            self.assertEqual(rows[0]["options"][-1], "Decide later")

    def test_detection_is_read_only_and_ignores_dependency_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_service(root)
            write(root, "node_modules/hidden.py", "import django\n")
            write(root, ".quality/old.json", "{}\n")
            before = fingerprint(root)

            with mock.patch.object(os, "system") as system:
                result = profile.inspect_project(root)

            self.assertEqual(fingerprint(root), before)
            system.assert_not_called()
            self.assertNotIn("Python", result.fact("languages").value)
            self.assertNotIn("Django", result.fact("frameworks").value)

    def test_profile_input_validation_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "not a directory"):
                profile.inspect_project(root / "missing")
            with self.assertRaisesRegex(ValueError, "cannot be negative"):
                profile.inspect_project(root, question_limit=-1)
            with self.assertRaisesRegex(ValueError, "source or status"):
                profile.ProfileFact(None, "bad", 1, "ready")
            with self.assertRaisesRegex(ValueError, "between 0 and 1"):
                profile.ProfileFact(None, "detected", 2, "ready")

            malformed = root / "bad.json"
            malformed.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "facts object"):
                profile.load_profile(malformed)
            malformed.write_text('{"facts":{"x":[]}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be an object"):
                profile.load_profile(malformed)
            malformed.write_text(
                '{"facts":{"x":{"source":1,"status":"ready","confidence":1}}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "metadata is invalid"):
                profile.load_profile(malformed)

    def test_invalid_existing_answers_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_service(root)
            result = profile.inspect_project(root)

            self.assertIsNone(profile._confirmed(profile._fact("value")))
            self.assertIsNone(profile._confirmed([]))
            self.assertIsNone(profile._confirmed({"source": "confirmed"}))
            self.assertIsNone(
                profile._confirmed(
                    {
                        "source": "confirmed",
                        "status": "invalid",
                        "confidence": 1,
                        "value": "x",
                    }
                )
            )
            unchanged = profile.inspect_project(root, {"facts": []})
            self.assertEqual(unchanged.facts, result.facts)


if __name__ == "__main__":
    unittest.main()
