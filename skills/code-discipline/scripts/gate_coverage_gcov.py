"""Read gcov's text reports (GCC ``gcov`` and LLVM ``llvm-cov gcov``).

A report holds one or more records, each starting with a ``Source:`` header
line. Every source line is ``count:line:text``: a number is the hit count
(GCC marks lines with unexecuted blocks ``5*``), ``#####`` and ``=====``
mean never executed, and ``-`` means no code. With ``-b -c`` each condition
adds ``branch N taken X`` or ``branch N never executed`` lines after its
source line. GCC's ``(throw)`` branches are exception edges the compiler
added, not conditions in the code, so they are left out.
"""

from __future__ import annotations

import re

from gate_coverage_data import (
    CoverageReportError,
    FileCoverage,
    RawCoverage,
    file_coverage,
)

_SOURCE_LINE = re.compile(r"^\s*(-|#####|=====|\$\$\$\$\$|%%%%%|\d+)\*?:\s*(\d+):(.*)$")
_BRANCH = re.compile(r"^branch\s+(\d+)\s+(?:taken\s+(\d+)|never executed)(.*)$")
_NOT_CODE = "-"
_SOURCE_HEADER = "Source:"


def _hits(count: str) -> int:
    """Hit count of one source line; the unexecuted marks read as 0."""
    return int(count) if count.isdigit() else 0


def _add_branch(coverage: FileCoverage, line: int, text: str) -> None:
    match = _BRANCH.match(text.strip())
    if match is None or "(throw)" in match.group(3):
        return
    coverage.add_outcome(line, match.group(1), int(match.group(2) or 0))


class _Reader:
    """Feeds report rows into ``raw``: the record and line a branch belongs to
    are the last ``Source:`` header and the last source line."""

    def __init__(self) -> None:
        self.raw: RawCoverage = {}
        self.current: FileCoverage | None = None
        self.line = 0

    def feed(self, row: str) -> None:
        match = _SOURCE_LINE.match(row)
        if match is None:
            self._other(row)
        else:
            self._source(match.group(1), int(match.group(2)), match.group(3))

    def _other(self, row: str) -> None:
        if self.current is not None and self.line:
            _add_branch(self.current, self.line, row)

    def _source(self, count: str, line: int, text: str) -> None:
        self.line = line
        if line == 0 and text.startswith(_SOURCE_HEADER):
            path = text[len(_SOURCE_HEADER) :].strip()
            self.current = file_coverage(self.raw, path)
        elif self.current is not None and line and count != _NOT_CODE:
            self.current.add_line(line, _hits(count))


def parse_gcov(text: str) -> RawCoverage:
    """gcov text output; several records may share one report."""
    reader = _Reader()
    for row in text.splitlines():
        reader.feed(row)
    if not reader.raw:
        raise CoverageReportError("this is not a gcov text report (no Source: line)")
    return reader.raw
