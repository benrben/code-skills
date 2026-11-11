from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import quality_config  # noqa: E402
from quality_model import GATE_ORDER  # noqa: E402

V2_THRESHOLDS = {
    "schema_version": 2,
    "metrics": {
        "coverage_limit": 85,
        "branch_coverage_limit": 80,
        "complexity_limit": 8,
        "craap_limit": 20,
    },
    "portable_analysis": {"max_cognitive_complexity": 12, "max_nesting_depth": 3},
    "file_loc": {"max_lines": 500},
    "test_execution": {"min_executed": 5, "max_skipped": 0},
    "mutation": {"max_surviving_mutants": 0},
}


class TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path


class ThresholdTests(TemporaryDirectoryTest):
    def test_bundled_defaults_are_schema_3(self) -> None:
        defaults = quality_config.default_thresholds()
        self.assertEqual(defaults["schema_version"], 3)
        self.assertEqual(quality_config.load_thresholds(None), defaults)
        self.assertEqual(set(defaults) - {"schema_version"}, set(GATE_ORDER))

    def test_bundled_path_falls_back_to_the_scripts_folder(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        fake_module = scripts / "quality_config.py"
        with mock.patch.object(quality_config, "__file__", str(fake_module)):
            with self.assertRaisesRegex(ValueError, "not found"):
                quality_config.bundled_thresholds_path()
            (scripts / "quality-thresholds.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                quality_config.bundled_thresholds_path(),
                scripts / "quality-thresholds.json",
            )

    def test_schema_2_is_migrated_in_memory(self) -> None:
        path = self.write_json("old.json", V2_THRESHOLDS)
        loaded = quality_config.load_thresholds(path)
        self.assertEqual(loaded["schema_version"], 3)
        self.assertEqual(loaded["coverage"]["line_coverage_limit"], 85)
        self.assertEqual(loaded["coverage"]["branch_coverage_limit"], 80)
        self.assertEqual(loaded["complexity"]["cyclomatic_limit"], 8)
        self.assertEqual(loaded["complexity"]["crap_limit"], 20)
        self.assertEqual(loaded["complexity"]["cognitive_limit"], 12)
        self.assertEqual(loaded["complexity"]["nesting_limit"], 3)
        self.assertEqual(loaded["file_loc"]["max_lines"], 500)
        self.assertEqual(loaded["tests"]["min_executed"], 5)
        self.assertNotIn("mutation", loaded)
        self.assertEqual(json.loads(path.read_text())["schema_version"], 2)

    def test_schema_3_file_gets_missing_goals(self) -> None:
        defaults = quality_config.default_thresholds()
        partial = {k: v for k, v in defaults.items() if k != "duplication"}
        partial["complexity"] = {"cyclomatic_limit": 7}
        loaded = quality_config.load_thresholds(self.write_json("t.json", partial))
        self.assertEqual(loaded["duplication"], defaults["duplication"])
        self.assertEqual(loaded["complexity"]["cyclomatic_limit"], 7)
        self.assertEqual(
            loaded["complexity"]["crap_limit"], defaults["complexity"]["crap_limit"]
        )

    def test_invalid_values_are_rejected(self) -> None:
        cases = [
            (("shortcuts", "max_findings"), 1, "remain 0"),
            (("coverage", "line_coverage_limit"), 101, "between 0 and 100"),
            (("file_loc", "max_lines"), True, "must be a number"),
            (("file_loc", "max_lines"), 1.5, "whole number"),
            (("duplication", "min_tokens"), 5, "at least 10"),
            (("complexity", "crap_limit"), "30", "must be a number"),
        ]
        for (section, key), value, message in cases:
            with self.subTest(section=section, key=key):
                thresholds = quality_config.default_thresholds()
                thresholds[section][key] = value
                path = self.write_json("bad.json", thresholds)
                with self.assertRaisesRegex(ValueError, message):
                    quality_config.load_thresholds(path)

    def test_shape_errors_are_rejected(self) -> None:
        thresholds = quality_config.default_thresholds()
        with self.assertRaisesRegex(ValueError, "schema_version must be 3"):
            quality_config.validate_thresholds({**thresholds, "schema_version": 4})
        with self.assertRaisesRegex(ValueError, "unknown keys: extra"):
            quality_config.validate_thresholds({**thresholds, "extra": {}})
        with self.assertRaisesRegex(ValueError, "missing keys: secrets"):
            quality_config.validate_thresholds(
                {k: v for k, v in thresholds.items() if k != "secrets"}
            )
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            quality_config.validate_thresholds({**thresholds, "secrets": 0})
        bad_section = {**thresholds, "secrets": {"max_findings": 0, "other": 1}}
        with self.assertRaisesRegex(ValueError, "unknown keys: other"):
            quality_config.validate_thresholds(bad_section)

    def test_unreadable_files_are_errors(self) -> None:
        path = self.root / "broken.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot read thresholds"):
            quality_config.load_thresholds(path)
        with self.assertRaisesRegex(ValueError, "must contain a JSON object"):
            quality_config.load_thresholds(self.write_json("list.json", [1]))
        with self.assertRaisesRegex(ValueError, "cannot read thresholds"):
            quality_config.load_thresholds(self.root / "missing.json")


class ConfigTests(TemporaryDirectoryTest):
    def test_defaults_without_a_file(self) -> None:
        config = quality_config.load_config(None)
        self.assertEqual(config, quality_config.default_config())
        self.assertTrue(config["tools"]["auto_install"])

    def test_file_merges_over_defaults(self) -> None:
        path = self.write_json(
            "c.json", {"test": {"command": "make test"}, "tools": {"cache_dir": "x"}}
        )
        config = quality_config.load_config(path)
        self.assertEqual(config["test"]["command"], "make test")
        self.assertEqual(config["test"]["timeout_seconds"], 900)
        self.assertEqual(config["tools"], {"auto_install": True, "cache_dir": "x"})

    def test_gates_must_be_an_object(self) -> None:
        with self.assertRaisesRegex(ValueError, "gates must be an object"):
            quality_config.load_config(self.write_json("c.json", {"gates": []}))
        with self.assertRaisesRegex(ValueError, "gates must be an object"):
            quality_config.gate_choices({"gates": ["tests"]})

    def test_legacy_coverage_adapter(self) -> None:
        one = {
            "metrics": {
                "coverage_commands": [["make", "cov"]],
                "coverage_report": "cov.json",
                "coverage_format": "coverage-json",
            }
        }
        config = quality_config.load_config(self.write_json("one.json", one))
        self.assertEqual(
            config["coverage"],
            {
                "command": ["make", "cov"],
                "report": "cov.json",
                "format": "coverage-json",
            },
        )
        text = {"metrics": {"coverage_commands": "make cov", "coverage_report": "r"}}
        self.assertEqual(
            quality_config.load_config(self.write_json("t.json", text))["coverage"],
            {"command": "make cov", "report": "r", "format": "auto"},
        )
        many = {
            "metrics": {
                "coverage_commands": [["a"], ["b"]],
                "coverage_report": "cov.json",
            }
        }
        self.assertEqual(
            quality_config.load_config(self.write_json("m.json", many))["coverage"], {}
        )
        self.assertEqual(quality_config.legacy_coverage_adapter("x"), {})
        self.assertEqual(
            quality_config.legacy_coverage_adapter(
                {"coverage_commands": [1], "coverage_report": "r"}
            ),
            {},
        )
        explicit = {**one, "coverage": {"report": "mine.xml"}}
        config = quality_config.load_config(self.write_json("e.json", explicit))
        self.assertEqual(config["coverage"], {"report": "mine.xml"})

    def test_gate_choices_default_on(self) -> None:
        enabled, notes = quality_config.gate_choices({})
        self.assertEqual(enabled, {key: True for key in GATE_ORDER})
        self.assertEqual(notes, [])

    def test_gate_choices_map_legacy_keys(self) -> None:
        enabled, notes = quality_config.gate_choices(
            {
                "gates": {
                    "quality": True,
                    "error_handling": False,
                    "format_lint": False,
                    "types": False,
                    "cognitive": False,
                    "complexity": True,
                    "mutation": True,
                    "smoke": False,
                }
            }
        )
        self.assertFalse(enabled["shortcuts"])
        self.assertTrue(enabled["coverage"])
        self.assertFalse(enabled["project_checks"])
        self.assertTrue(enabled["complexity"])
        self.assertTrue(enabled["tests"])
        self.assertEqual(
            notes, ["ignored removed gate mutation", "ignored removed gate smoke"]
        )

    def test_gate_choice_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be true or false"):
            quality_config.gate_choices({"gates": {"tests": "yes"}})
        with self.assertRaisesRegex(ValueError, "unknown gate 'testz'"):
            quality_config.gate_choices({"gates": {"testz": True}})

    def test_initial_files_are_written_once(self) -> None:
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname='a'\n")
        config_path = quality_config.write_initial_config(self.root)
        self.assertEqual(config_path, self.root / quality_config.CONFIG_NAME)
        assert config_path is not None
        config = json.loads(config_path.read_text())
        self.assertEqual(config["gates"], {key: True for key in GATE_ORDER})
        self.assertIn("tests/**", config["source"]["exclude"])
        thresholds_path = quality_config.write_initial_thresholds(self.root)
        assert thresholds_path is not None
        self.assertEqual(
            json.loads(thresholds_path.read_text()), quality_config.default_thresholds()
        )
        self.assertIsNone(quality_config.write_initial_config(self.root))
        self.assertIsNone(quality_config.write_initial_thresholds(self.root))

    def test_ensure_git_repository(self) -> None:
        message = quality_config.ensure_git_repository(self.root)
        self.assertIn("Initialized", message or "")
        self.assertTrue((self.root / ".git").exists())
        self.assertIsNone(quality_config.ensure_git_repository(self.root))
        other = self.root / "other"
        other.mkdir()
        failure = subprocess.CalledProcessError(1, ["git"])
        with mock.patch.object(quality_config.subprocess, "run", side_effect=failure):
            self.assertIn(
                "not available", quality_config.ensure_git_repository(other) or ""
            )

    def test_deep_merge(self) -> None:
        merged = quality_config.deep_merge(
            {"a": {"b": 1, "c": 2}, "d": 1}, {"a": {"b": 3}, "d": {"e": 1}}
        )
        self.assertEqual(merged, {"a": {"b": 3, "c": 2}, "d": {"e": 1}})


if __name__ == "__main__":
    unittest.main()
