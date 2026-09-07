import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills/code-discipline/scripts/project_quality.py"


def load_module():
    spec = importlib.util.spec_from_file_location("project_quality_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load project quality module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


project_quality = load_module()


class ProjectQualityTests(unittest.TestCase):
    def test_metric_catalog_covers_every_quality_dimension(self):
        dimensions = {item.key for item in project_quality.DIMENSIONS}
        metric_dimensions = {item.dimension for item in project_quality.METRICS}
        self.assertEqual(dimensions, metric_dimensions)
        self.assertGreaterEqual(len(project_quality.METRICS), 50)

    def test_metric_priorities_match_the_generic_library_baseline(self):
        applicable = {
            "product",
            "security",
            "supply_chain",
            "governance",
            "language_semantics",
        }
        priorities = [
            project_quality._metric_priority(metric)
            for metric in project_quality.METRICS
            if metric.dimension in applicable
        ]
        self.assertEqual(
            {priority: priorities.count(priority) for priority in set(priorities)},
            {"required": 11, "recommended": 13, "optional": 8},
        )

    def test_profile_values_support_confirmed_records_and_plain_answers(self):
        profile = {
            "answers": {
                "product.behaviors": {
                    "value": ["Create a report"],
                    "source": "confirmed",
                },
                "operations.runbooks": 2,
            }
        }
        self.assertEqual(
            project_quality.profile_value(profile, "product.behaviors"),
            (["Create a report"], "confirmed"),
        )
        self.assertEqual(
            project_quality.profile_value(profile, "operations.runbooks"),
            (2, "confirmed"),
        )
        self.assertEqual(
            project_quality.profile_value(profile, "missing"), (None, "missing")
        )

    def test_assessment_distinguishes_measured_context_unsupported_and_na(self):
        facts = {
            "has_ui": False,
            "has_api": True,
            "has_database": False,
            "has_infrastructure": False,
            "deployable": True,
            "tests.executed": 8,
            "tests.pass_rate": 100.0,
            "coverage.branch_percent": 100.0,
            "security.secret_findings": 0,
            "security.known_vulnerabilities": 0,
            "performance.test_suite_seconds": 0.5,
        }
        profile = {
            "answers": {
                "product.behaviors": {
                    "value": ["Publish a report"],
                    "source": "confirmed",
                },
                "security.risk_level": {
                    "value": "standard",
                    "source": "confirmed",
                },
            }
        }
        result = project_quality.assess_quality_dimensions(profile, facts)
        by_key = {item.key: item for item in result}
        self.assertEqual(by_key["ui_accessibility"].status, "not_applicable")
        self.assertEqual(by_key["integration"].status, "needs_context")
        self.assertIn(
            "unsupported",
            {metric.status for metric in by_key["language_semantics"].metrics},
        )
        security = {metric.key: metric for metric in by_key["security"].metrics}
        self.assertEqual(security["secret_exposure_count"].value, 0)
        self.assertEqual(security["secret_exposure_count"].priority, "required")
        self.assertEqual(security["risk_level"].status, "confirmed")
        self.assertEqual(security["sast_dast_findings"].priority, "optional")
        code = {metric.key: metric for metric in by_key["language_semantics"].metrics}
        self.assertEqual(code["static_smells"].priority, "recommended")
        self.assertEqual(code["npath"].priority, "optional")

    def test_explicit_applicability_overrides_detection(self):
        profile = {
            "answers": {
                "dimension.ui_accessibility.applicable": {
                    "value": True,
                    "source": "confirmed",
                }
            }
        }
        dimensions = project_quality.assess_quality_dimensions(
            profile, {"has_ui": False}
        )
        ui = next(item for item in dimensions if item.key == "ui_accessibility")
        self.assertNotEqual(ui.status, "not_applicable")

        dimensions = project_quality.assess_quality_dimensions(
            {"dimensions": {"ui_accessibility": {"applicable": False}}},
            {"has_ui": True},
        )
        ui = next(item for item in dimensions if item.key == "ui_accessibility")
        self.assertEqual(ui.status, "not_applicable")

    def test_complete_dimension_summary_and_empty_branch_measurements(self):
        measured = project_quality.MetricEvidence(
            "tests", "Tests", "measured", 1, "quality gate", "Proves behavior"
        )
        self.assertEqual(
            project_quality._dimension_summary("measured", (measured,)),
            "1/1 metrics are available.",
        )
        function = SimpleNamespace(
            branch_coverage_measured=False,
            branch_coverage_percent=0.0,
            complexity=1,
            crap_score=1.0,
        )
        self.assertIsNone(
            project_quality._function_facts((function,))["coverage.branch_percent"]
        )

    def test_certifications_require_complete_dimensions_and_repository(self):
        complete = project_quality.QualityDimension(
            "product",
            "Product behavior",
            "product",
            "measured",
            "Measured",
            (),
        )
        missing = project_quality.QualityDimension(
            "security",
            "Security",
            "security",
            "needs_context",
            "Needs context",
            (),
        )
        states = project_quality.certification_states([complete, missing], True)
        self.assertEqual(states["repository"]["status"], "certified")
        self.assertEqual(states["security"]["status"], "needs_context")
        self.assertEqual(states["full_profile"]["status"], "needs_context")
        self.assertEqual(
            project_quality.certification_states([complete], False)["repository"][
                "status"
            ],
            "needs_work",
        )
        applicable = project_quality.certification_states([complete], True)
        self.assertEqual(applicable["deployment"]["status"], "not_applicable")
        self.assertEqual(applicable["operations"]["status"], "not_applicable")
        self.assertEqual(applicable["full_profile"]["status"], "certified")

    def test_load_profile_handles_missing_valid_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            self.assertEqual(project_quality.load_profile(path), {})
            path.write_text(json.dumps({"answers": {}}), encoding="utf-8")
            self.assertEqual(project_quality.load_profile(path), {"answers": {}})
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                project_quality.load_profile(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cannot read project profile"):
                project_quality.load_profile(path)

    def test_analysis_facts_preserve_zeroes_and_gate_evidence(self):
        gate = lambda key, passed: SimpleNamespace(  # noqa: E731
            key=key,
            passed=passed,
            applicable=True,
            deferred=False,
            blocked=False,
            unsupported=False,
            needs_context=False,
        )
        function = SimpleNamespace(
            coverage_percent=100.0,
            branch_coverage_percent=95.0,
            branch_coverage_measured=True,
            complexity=3,
            crap_score=3.0,
        )
        portable = SimpleNamespace(
            complexity=(
                SimpleNamespace(
                    status="supported",
                    functions=(SimpleNamespace(cognitive_complexity=4),),
                ),
                SimpleNamespace(status="unsupported", functions=()),
            ),
            duplication=SimpleNamespace(percentage=2.5),
            dependencies=SimpleNamespace(
                cycles=(),
                modules=(SimpleNamespace(module="a", fan_in=2, fan_out=1),),
            ),
            secrets=(),
            hotspots=(SimpleNamespace(path="a.py"),),
        )
        analysis = SimpleNamespace(
            passed=True,
            gates=[gate("smoke", True), gate("flaky", True), gate("mutation", True)],
            functions=[function],
            files=[SimpleNamespace(lines=20)],
            test_execution=SimpleNamespace(
                measured=True, executed=2, passed=2, failed=0, skipped=0
            ),
            suite_duration_seconds=0.25,
            smoke_probes=[SimpleNamespace(passed=True)],
            mutations=[SimpleNamespace(survived=False)],
            portable_analysis=portable,
            vulnerabilities=(),
        )
        facts = project_quality.analysis_facts(analysis)
        self.assertEqual(facts["tests.pass_rate"], 100.0)
        self.assertEqual(facts["mutation.score"], 100.0)
        self.assertEqual(facts["architecture.max_fan_in"], 2)
        self.assertEqual(facts["complexity.unsupported_files"], 1)
        self.assertEqual(facts["source.max_file_lines"], 20)

    def test_attach_project_quality_loads_configured_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_path = root / ".quality" / "custom.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps({"answers": {"product.behaviors": ["Run"]}}),
                encoding="utf-8",
            )
            analysis = SimpleNamespace(
                passed=False,
                gates=[],
                functions=[],
                files=[],
                test_execution=SimpleNamespace(measured=False),
                suite_duration_seconds=None,
                smoke_probes=[],
                mutations=[],
                portable_analysis=None,
                vulnerabilities=(),
            )
            project_quality.attach_project_quality(
                analysis,
                root,
                {"project_quality": {"profile": ".quality/custom.json"}},
            )
            self.assertEqual(
                analysis.project_profile["answers"], {"product.behaviors": ["Run"]}
            )
            self.assertEqual(
                len(analysis.quality_dimensions), len(project_quality.DIMENSIONS)
            )
            self.assertIn("full_profile", analysis.certifications)

    def test_attach_uses_detected_and_profile_surfaces_and_serializes_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_path = root / ".quality" / "project-profile.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps(
                    {
                        "detected": {
                            "has_ui": True,
                            "deployable": True,
                            "ignored": "value",
                        },
                        "facts": {
                            "project_type": {"value": ["web", "api"]},
                            "databases": {"value": ["sqlite"]},
                            "infrastructure": {"value": ["Dockerfile"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            analysis = self._empty_analysis()
            project_quality.attach_project_quality(analysis, root, {})
            dimensions = {item.key: item for item in analysis.quality_dimensions}
            self.assertNotEqual(dimensions["ui_accessibility"].status, "not_applicable")
            self.assertNotEqual(dimensions["integration"].status, "not_applicable")
            self.assertNotEqual(dimensions["data_safety"].status, "not_applicable")
            self.assertNotEqual(dimensions["infrastructure"].status, "not_applicable")
            serialized = project_quality.dimension_dicts(
                (dimensions["ui_accessibility"],)
            )
            self.assertEqual(serialized[0]["key"], "ui_accessibility")
            self.assertEqual(serialized[0]["metrics"][0]["priority"], "required")

    def test_attach_tolerates_non_mapping_optional_profile_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_path = root / ".quality" / "project-profile.json"
            profile_path.parent.mkdir()
            profile_path.write_text(
                json.dumps({"detected": [], "facts": []}), encoding="utf-8"
            )
            analysis = self._empty_analysis()
            project_quality.attach_project_quality(analysis, root, {})
            self.assertEqual(
                len(analysis.quality_dimensions), len(project_quality.DIMENSIONS)
            )

    @staticmethod
    def _empty_analysis():
        return SimpleNamespace(
            passed=False,
            gates=[],
            functions=[],
            files=[],
            test_execution=SimpleNamespace(measured=False),
            suite_duration_seconds=None,
            smoke_probes=[],
            mutations=[],
            portable_analysis=None,
            vulnerabilities=(),
        )


if __name__ == "__main__":
    unittest.main()
