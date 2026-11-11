"""Run project commands the way the quality loop needs them.

Every project command runs from the repository root with an environment that
keeps package managers offline, so a check never restores or upgrades the
project's dependencies. A command that runs too long is stopped together with
every process it started.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gate_types import CommandRun, Runner

# The tail of a command's output that is kept. Large enough for Go's JSON
# test events, whose counts need every line.
OUTPUT_LIMIT = 4_000_000
STOP_GRACE_SECONDS = 2

OFFLINE_ENVIRONMENT = {
    "UV_NO_SYNC": "1",
    "UV_OFFLINE": "1",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "NPM_CONFIG_OFFLINE": "true",
    "YARN_ENABLE_NETWORK": "0",
    "BUNDLE_FROZEN": "true",
    "COMPOSER_DISABLE_NETWORK": "1",
    "CARGO_NET_OFFLINE": "true",
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "GOPROXY": "off",
    "GOSUMDB": "off",
    "GOTOOLCHAIN": "local",
}


def project_execution_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Variables that stop common package managers from restoring dependencies,
    with ``extra`` applied last."""
    return {**OFFLINE_ENVIRONMENT, **(extra or {})}


def command_list(value: Any) -> list[str] | None:
    """A configured command (shell-style text or an argument array), or None
    when it is empty. Raises ValueError for any other shape."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(
        isinstance(part, (str, int, float)) for part in value
    ):
        return [str(part) for part in value]
    raise ValueError("a command must be text or an array of arguments")


def run_command(
    command: Sequence[str],
    root: Path,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> CommandRun:
    """Run ``command`` in ``root`` with ``env`` over the inherited environment.

    Exit 124 means it timed out (the whole process tree is stopped); 127
    means it could not start."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=root,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            **_process_group_options(),
        )
    except OSError as error:
        return CommandRun(tuple(command), 127, str(error), _since(started))
    try:
        output, _ = process.communicate(timeout=max(1, timeout_seconds))
    except subprocess.TimeoutExpired:
        output = stop_process_tree(process)
        return CommandRun(
            tuple(command), 124, _tail(output), _since(started), timed_out=True
        )
    except BaseException:
        stop_process_tree(process)
        raise
    return CommandRun(
        tuple(command), process.returncode, _tail(output), _since(started)
    )


def stop_process_tree(process: subprocess.Popen[str]) -> str:
    """Stop a command and every descendant; return the output captured so far."""
    if process.poll() is None:
        _signal_tree(process, signal.SIGTERM)
    try:
        output, _ = process.communicate(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_tree(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        output, _ = process.communicate()
    return output or ""


def runner_for(root: Path) -> Runner:
    """The runner every gate uses: commands run in ``root``, offline."""

    def run(
        command: Sequence[str],
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        return run_command(command, root, timeout_seconds, project_execution_env(env))

    return run


def _process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        windows_subprocess: Any = subprocess
        return {"creationflags": int(windows_subprocess.CREATE_NEW_PROCESS_GROUP)}
    return {"start_new_session": True}


def _signal_tree(process: subprocess.Popen[str], signal_number: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        # The group already exited between the poll and the signal.
        return


def _tail(output: str | None) -> str:
    return (output or "")[-OUTPUT_LIMIT:]


def _since(started: float) -> float:
    return time.monotonic() - started
