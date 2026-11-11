"""Plain result types shared by the generic gate modules.

Gate modules return a ``GateOutcome``; the orchestrator converts it into the
report's ``GateResult``. Keeping these types here lets every gate module stay
independent of the orchestrator.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

# Gates return the first four. A run also records gates it did not run:
# switched off by the project, not selected, or deferred by ``--fast``.
Status = Literal[
    "pass", "fail", "blocked", "not_applicable", "off", "skipped", "deferred"
]

PASS: Status = "pass"
FAIL: Status = "fail"
BLOCKED: Status = "blocked"
NOT_APPLICABLE: Status = "not_applicable"
OFF: Status = "off"
SKIPPED: Status = "skipped"
DEFERRED: Status = "deferred"


@dataclasses.dataclass(frozen=True)
class GateItem:
    """One finding: where it is, what was measured, and the limit."""

    path: str
    line: int | None
    name: str
    metric: str
    value: str
    limit: str

    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def describe(self) -> str:
        label = f" {self.name}" if self.name else ""
        return (
            f"{self.location()}{label}: {self.metric} {self.value} (limit {self.limit})"
        )


@dataclasses.dataclass
class GateOutcome:
    """The complete result of one gate run."""

    key: str
    title: str
    status: Status
    summary: str
    details: list[str] = dataclasses.field(default_factory=list)
    items: list[GateItem] = dataclasses.field(default_factory=list)
    measurements: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in (PASS, NOT_APPLICABLE)

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    @property
    def applicable(self) -> bool:
        return self.status != NOT_APPLICABLE


def plural(count: int, noun: str, many: str | None = None) -> str:
    """``1 file`` or ``2 files``; ``many`` names an irregular plural."""
    return f"{count} {noun if count == 1 else many or noun + 's'}"


def outcome_for_items(
    key: str,
    title: str,
    items: list[GateItem],
    passing_summary: str,
    failing_summary: str,
    **extra: Any,
) -> GateOutcome:
    """Build a PASS outcome when ``items`` is empty, otherwise a FAIL outcome."""
    status: Status = FAIL if items else PASS
    summary = failing_summary if items else passing_summary
    details = [item.describe() for item in items]
    return GateOutcome(key, title, status, summary, details, list(items), **extra)


@dataclasses.dataclass(frozen=True)
class FunctionInfo:
    """One function as lizard reports it, with 1-based inclusive line range."""

    path: str
    name: str
    long_name: str
    start_line: int
    end_line: int
    cyclomatic: int = 1
    cognitive: int = 0
    nesting: int = 0
    parameters: int = 0
    is_method: bool = False

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line

    @property
    def bare_name(self) -> str:
        """The last segment of the name: ``Cart::total`` and ``Cart.total`` give
        ``total``."""
        for separator in ("::", "."):
            if separator in self.name:
                return self.name.rsplit(separator, 1)[-1]
        return self.name


@dataclasses.dataclass(frozen=True)
class CommandRun:
    """One finished command: exit code, combined output, and wall time."""

    command: tuple[str, ...]
    returncode: int
    output: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Runner(Protocol):
    """Runs a project command from the repository root.

    The orchestrator supplies a runner that applies its offline, no-restore
    environment and kills the whole process tree on timeout.
    """

    def __call__(
        self,
        command: Sequence[str],
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun: ...


@dataclasses.dataclass(frozen=True)
class ToolPython:
    """The interpreter and environment that can import the pinned quality tools
    (lizard and Pygments)."""

    executable: str = sys.executable
    env: Mapping[str, str] = dataclasses.field(default_factory=dict)


def subprocess_runner(root: Path) -> Runner:
    """A plain runner for tests and standalone use."""

    def run(
        command: Sequence[str],
        timeout_seconds: int,
        env: Mapping[str, str] | None = None,
    ) -> CommandRun:
        started = time.monotonic()
        try:
            result = subprocess.run(
                list(command),
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, **(env or {})},
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b""
            text = output.decode("utf-8", errors="replace")
            elapsed = time.monotonic() - started
            return CommandRun(tuple(command), 124, text, elapsed, timed_out=True)
        except OSError as error:
            elapsed = time.monotonic() - started
            return CommandRun(tuple(command), 127, str(error), elapsed)
        text = result.stdout.decode("utf-8", errors="replace")
        elapsed = time.monotonic() - started
        return CommandRun(tuple(command), result.returncode, text, elapsed)

    return run
