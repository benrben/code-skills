import json
import subprocess
import sys
import tempfile
from pathlib import Path

from behave import given, then, when

LOOP = (
    Path(__file__).resolve().parents[3]
    / "skills/code-discipline/scripts/quality_loop.py"
)


@given("a temporary repository with {validity} Python")
def repository(context, validity):
    context.repository = tempfile.TemporaryDirectory()
    context.root = Path(context.repository.name)
    contents = {"valid": "answer = 42\n", "invalid": "answer =\n"}
    (context.root / "example.py").write_text(contents[validity])
    quality = context.root / ".quality"
    quality.mkdir()
    config = {
        "format_lint": {
            "enabled": True,
            "required": True,
            "commands": [[sys.executable, "-m", "py_compile", "example.py"]],
        },
        "tools": {"auto_install": False},
    }
    (quality / "quality-gate.json").write_text(json.dumps(config))


@when("I run its lint gate")
def run_lint(context):
    context.result = subprocess.run(
        [sys.executable, str(LOOP), "--root", str(context.root), "--lint"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


@then("the command exits with {code:d}")
def command_exit(context, code):
    assert context.result.returncode == code, (
        context.result.stdout + context.result.stderr
    )


@then("the status is {status}")
def status_line(context, status):
    assert f"QUALITY_LOOP={status}\n" in context.result.stdout, context.result.stdout


@then("the report does not certify the repository")
def no_certification(context):
    state = json.loads((context.root / ".quality/quality-gate-state.json").read_text())
    assert state["certified"] is False
