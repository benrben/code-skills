"""Small real Behave fixture for tests of the full gate orchestration."""

import json
import sys
from pathlib import Path


def acceptance_config(root, command):
    environment = root / ".venv"
    if not environment.exists():
        environment.symlink_to(Path(sys.prefix), target_is_directory=True)
    features = root / "tests/features"
    steps = features / "steps"
    steps.mkdir(parents=True, exist_ok=True)
    (features / "public_command.feature").write_text(
        "Feature: Public command\n"
        "  Scenario: The public workflow succeeds\n"
        "    When I execute the public command\n"
        "    Then the command succeeds\n"
    )
    (steps / "command_steps.py").write_text(
        "import subprocess\nfrom behave import when, then\n"
        "@when('I execute the public command')\n"
        "def execute(context):\n"
        f"    context.result = subprocess.run({command!r}, capture_output=True, text=True, check=False)\n"
        "@then('the command succeeds')\n"
        "def succeeds(context):\n"
        "    assert context.result.returncode == 0, context.result.stderr\n"
    )
    return {
        "command": [
            sys.executable,
            "-m",
            "behave",
            "tests/features",
            "-f",
            "json",
            "-o",
            "{report}",
        ],
        "report": ".quality/gherkin.json",
        "format": "behave-json",
    }


def add_acceptance_config(root, command):
    path = root / ".quality/quality-gate.json"
    config = json.loads(path.read_text())
    config["gherkin"] = acceptance_config(root, command)
    path.write_text(json.dumps(config))
