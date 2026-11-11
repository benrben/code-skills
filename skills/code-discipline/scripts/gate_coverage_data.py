"""The one shape every coverage report reader produces.

``gate_coverage_readers`` and the format modules beside it (such as
``gate_coverage_gcov``) parse report text into ``RawCoverage`` keyed by the
path the report wrote; the coverage gate only ever sees ``CoverageData``
keyed by repo-relative POSIX paths.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class FileCoverage:
    """Line hit counts and per-line branch outcomes (covered, total).

    ``outcomes`` names each branch outcome (line -> outcome id -> hits) for
    formats that identify outcomes, so merging the reports of two processes
    unions the outcomes they took. Formats that only give counts per line
    (Cobertura, JaCoCo) merge with ``max``: a lower bound when two runs took
    different sides of one condition.
    """

    lines: dict[int, int] = dataclasses.field(default_factory=dict)
    branches: dict[int, tuple[int, int]] = dataclasses.field(default_factory=dict)
    outcomes: dict[int, dict[str, int]] = dataclasses.field(default_factory=dict)

    def add_line(self, line: int, hits: int) -> None:
        self.lines[line] = max(self.lines.get(line, 0), hits)

    def add_branches(self, line: int, covered: int, total: int) -> None:
        """Record anonymous outcome counts; the best evidence per line wins."""
        old_covered, old_total = self.branches.get(line, (0, 0))
        self.branches[line] = (max(old_covered, covered), max(old_total, total))

    def add_outcome(self, line: int, outcome: str, hits: int) -> None:
        """Record one named outcome; the same outcome seen twice is one."""
        named = self.outcomes.setdefault(line, {})
        named[outcome] = max(named.get(outcome, 0), hits)
        taken = sum(1 for count in named.values() if count > 0)
        self.branches[line] = (taken, len(named))

    def merge(self, other: FileCoverage) -> None:
        """Combine two reports of one file, keeping the best evidence per line."""
        for line, hits in other.lines.items():
            self.add_line(line, hits)
        for line, (covered, total) in other.branches.items():
            if line not in other.outcomes:
                self.add_branches(line, covered, total)
        for line, named in other.outcomes.items():
            for outcome, hits in named.items():
                self.add_outcome(line, outcome, hits)


# Report path as written by the tool -> coverage of that file.
RawCoverage = dict[str, FileCoverage]
# Repo-relative POSIX path -> coverage of that file.
CoverageData = dict[str, FileCoverage]


class CoverageReportError(ValueError):
    """The report is missing or cannot be parsed."""


def file_coverage(raw: RawCoverage, path: str) -> FileCoverage:
    """The entry for ``path``, created empty on first use."""
    return raw.setdefault(path, FileCoverage())
