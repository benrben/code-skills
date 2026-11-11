"""Source text of the small programs that coverage recipes write or run.

Recipes stay data: they name these programs in commands or write them as
files, and the orchestrator runs them. Helper steps are ``python -c``
programs, so they behave the same on every platform.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

# Remove stale output from earlier runs, then recreate the directory.
FRESH_DIRECTORY = (
    "import pathlib, shutil, sys; "
    "shutil.rmtree(sys.argv[1], ignore_errors=True); "
    "pathlib.Path(sys.argv[1]).mkdir(parents=True)"
)

# Copy the file whose path a command prints (``swift test --show-codecov-path``).
COPY_PRINTED_PATH = (
    "import shutil, subprocess, sys; "
    "found = subprocess.run(sys.argv[2:], stdout=subprocess.PIPE, text=True, "
    "check=True).stdout.strip().splitlines()[-1]; "
    "shutil.copyfile(found, sys.argv[1])"
)

# Build the JaCoCo XML report from whatever class directories the build made.
JACOCO_REPORT = """
import pathlib, subprocess, sys
cli, execution, report, root = sys.argv[1:5]
base = pathlib.Path(root)
def found(patterns):
    return sorted({str(p) for pattern in patterns for p in base.glob(pattern)
                   if p.is_dir() and "node_modules" not in p.parts})
classes = found(["**/target/classes", "**/build/classes/java/main",
                 "**/build/classes/kotlin/main"])
sources = found(["**/src/main/java", "**/src/main/kotlin"])
if not classes:
    sys.exit("JaCoCo report: no compiled classes found under " + root)
command = ["java", "-jar", cli, "report", execution]
for directory in classes:
    command += ["--classfiles", directory]
for directory in sources:
    command += ["--sourcefiles", directory]
sys.exit(subprocess.call(command + ["--xml", report]))
"""

# Run as ``python -S -c PYTHON_FINALIZE <rcfile> <report> <startup dir>``.
# ``-S`` keeps this process unmeasured; its children run without
# COVERAGE_PROCESS_START, so combining and reporting are not measured either.
PYTHON_FINALIZE = """
import os, subprocess, sys
rcfile, report, startup = sys.argv[1:4]
env = dict(os.environ)
env.pop("COVERAGE_PROCESS_START", None)
env["PYTHONPATH"] = os.pathsep.join(filter(None, [startup, env.get("PYTHONPATH")]))
# A test may run code from a file it then deletes; --ignore-errors keeps
# that one missing source from blocking the report for every other file.
for step in (["combine"], ["json", "-o", report, "--ignore-errors"]):
    command = [sys.executable, "-m", "coverage", *step, "--rcfile=" + rcfile]
    code = subprocess.call(command, env=env)
    if code:
        sys.exit(code)
"""


def python_startup(tool_paths: Sequence[str]) -> str:
    """``sitecustomize.py`` for a directory the recipe puts on PYTHONPATH.

    Every Python process the test command starts imports it, so tests run in
    subprocesses are measured too, and nothing is written into the
    project's interpreter. Coverage.py comes from the tool cache, appended
    after the project's own packages so those keep winning. A project
    ``sitecustomize`` that this file hides still runs afterwards.
    """
    return f'''"""Written by the coverage gate: measure this Python process."""
import importlib.machinery
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.extend({list(tool_paths)!r})
if os.environ.get("COVERAGE_PROCESS_START"):
    import coverage

    coverage.process_startup()
_OTHERS = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]
_SPEC = importlib.machinery.PathFinder.find_spec("sitecustomize", _OTHERS)
if _SPEC is not None and _SPEC.loader is not None:
    _SPEC.loader.exec_module(importlib.util.module_from_spec(_SPEC))
'''


def coverage_rc(root: Path, data_dir: Path, omitted: Sequence[Path]) -> str:
    """coverage.py settings for the gate.

    Exclusions are explicit so no comment can hide code from the gate:
    coverage.py's default exclusions honor a "no cover" pragma comment in
    any letter case. Only stub bodies (``...``) and ``if TYPE_CHECKING:``
    blocks, which never run, are excluded, and no comment marks a branch as
    partial.
    """
    omit = "\n".join(f"    {directory}/*" for directory in omitted)
    return (
        "[run]\n"
        "branch = True\n"
        f"source =\n    {root}\n"
        f"omit =\n{omit}\n"
        f"data_file = {data_dir / '.coverage'}\n"
        "parallel = True\n"
        "\n"
        "[report]\n"
        "exclude_lines =\n"
        r"    ^\s*(((async )?def .*?)?\)(\s*->.*?)?:\s*)?\.\.\.\s*(#|$)" + "\n"
        r"    if (typing\.)?TYPE_CHECKING:" + "\n"
        "partial_branches =\n"
    )


def _ruby_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ruby_helper(root: Path, output_dir: Path) -> str:
    """Ruby code loaded with ``-r`` into every Ruby process: it records line
    and branch coverage of files under ``root`` and writes one JSON file per
    process, because ``rake`` and friends start several Ruby processes.
    Each branch outcome keeps its target id, so outcomes taken in different
    processes add up when the reports are merged."""
    return f"""require "coverage"
Coverage.start(lines: true, branches: true)
at_exit do
  require "json"
  root = File.join(File.expand_path({_ruby_string(str(root))}), "")
  files = {{}}
  Coverage.result.each do |path, data|
    full = File.expand_path(path)
    next unless full.start_with?(root)
    branches = []
    (data[:branches] || {{}}).each do |condition, targets|
      targets.each {{ |target, count| branches << [condition[2], target[1], count] }}
    end
    files[full] = {{ "lines" => data[:lines], "branches" => branches }}
  end
  output = File.join({_ruby_string(str(output_dir))}, "#{{Process.pid}}.json")
  File.write(output, JSON.dump({{ "files" => files }})) unless files.empty?
end
"""
