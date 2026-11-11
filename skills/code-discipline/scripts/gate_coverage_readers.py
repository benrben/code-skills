"""Coverage report readers: turn every supported report format into one shape.

Each reader parses report text into ``RawCoverage`` keyed by the path exactly
as the report wrote it. ``resolve_report_paths`` then maps those paths onto
repository files (``gate_coverage_paths``), so the coverage gate only ever
sees repo-relative POSIX paths. Every parser checks the marks of its format
and raises ``CoverageReportError`` on a mismatch, so a report in the wrong
format is an error rather than an empty measurement.
"""

from __future__ import annotations

import dataclasses
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from gate_coverage_data import CoverageData as CoverageData
from gate_coverage_data import CoverageReportError as CoverageReportError
from gate_coverage_data import FileCoverage as FileCoverage
from gate_coverage_data import RawCoverage as RawCoverage
from gate_coverage_data import file_coverage as _file
from gate_coverage_gcov import parse_gcov
from gate_coverage_paths import match_report_paths
from source_files import normalize_path, read_text, walk_files

# --- coverage.py JSON -------------------------------------------------------


def _json_object(text: str, required: Iterable[str], kind: str) -> dict[str, Any]:
    """Parse JSON and insist on the keys that mark this report format."""
    value = json.loads(text)
    if not isinstance(value, dict) or not all(key in value for key in required):
        raise CoverageReportError(f"this is not a {kind} report")
    return value


def parse_coverage_py_json(text: str) -> RawCoverage:
    """``coverage json`` output. Branch arcs are ``[from, to]`` pairs grouped
    by their source line; ``excluded_lines`` are not executable."""
    value = _json_object(text, ("meta", "files"), "coverage.py JSON")
    raw: RawCoverage = {}
    for filename, details in value["files"].items():
        coverage = _file(raw, str(filename))
        for line in details.get("executed_lines", []):
            coverage.add_line(int(line), 1)
        for line in details.get("missing_lines", []):
            coverage.add_line(int(line), 0)
        for arc in details.get("executed_branches", []):
            coverage.add_outcome(int(arc[0]), str(arc[1]), 1)
        for arc in details.get("missing_branches", []):
            coverage.add_outcome(int(arc[0]), str(arc[1]), 0)
    return raw


# --- Istanbul JSON (coverage-final.json) ------------------------------------


def _istanbul_entries(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(value, dict):
        raise CoverageReportError("this is not an Istanbul coverage-final.json report")
    entries = [
        (str(name), details)
        for name, details in value.items()
        if isinstance(details, dict) and "statementMap" in details
    ]
    if value and not entries:
        raise CoverageReportError("this is not an Istanbul coverage-final.json report")
    return entries


def parse_istanbul_json(text: str) -> RawCoverage:
    """Istanbul ``coverage-final.json``: statements give line hits, branch
    locations give outcomes on their start line."""
    raw: RawCoverage = {}
    for filename, details in _istanbul_entries(json.loads(text)):
        coverage = _file(raw, str(details.get("path", filename)))
        hits = details.get("s", {})
        for statement, location in details["statementMap"].items():
            coverage.add_line(_start_line(location), int(hits.get(statement, 0)))
        _add_istanbul_branches(coverage, details)
    return raw


def _start_line(location: Mapping[str, Any]) -> int:
    return int(location["start"]["line"])


def _add_istanbul_branches(coverage: FileCoverage, details: Mapping[str, Any]) -> None:
    counts = details.get("b", {})
    for branch, location in details.get("branchMap", {}).items():
        line = _start_line(location.get("loc") or location["locations"][0])
        for index, count in enumerate(counts.get(branch, [])):
            coverage.add_outcome(line, f"{branch}.{index}", int(count))


# --- LCOV --------------------------------------------------------------------


def parse_lcov(text: str) -> RawCoverage:
    """LCOV tracefile: ``SF`` starts a record, ``DA:line,hits`` and
    ``BRDA:line,block,branch,taken`` (``-`` means never evaluated).

    A file may have several records (concatenated tracefiles). Hits are
    kept per line and per named outcome with ``max``, so the records merge
    like separate reports and an outcome two records share counts once."""
    raw: RawCoverage = {}
    current: FileCoverage | None = None
    for line in text.splitlines():
        record, _, value = line.strip().partition(":")
        if record == "SF":
            current = _file(raw, value)
        elif current is not None:
            _add_lcov_record(current, record, value.split(","))
    return raw


def _add_lcov_record(coverage: FileCoverage, record: str, fields: list[str]) -> None:
    if record == "DA":
        coverage.add_line(int(fields[0]), int(float(fields[1])))
    elif record == "BRDA":
        taken = 0 if fields[3] == "-" else int(fields[3])
        coverage.add_outcome(int(fields[0]), f"{fields[1]},{fields[2]}", taken)


# --- Cobertura and JaCoCo XML ------------------------------------------------

_CONDITION = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


def _xml_root(text: str, tag: str, kind: str) -> ET.Element:
    tree = ET.fromstring(text)
    if tree.tag != tag:
        raise CoverageReportError(f"this is not a {kind} report (<{tree.tag}>)")
    return tree


def parse_cobertura(text: str) -> RawCoverage:
    """Cobertura XML (coverage.py, coverlet, PHPUnit, istanbul). A single
    ``<source>`` directory is joined to the relative class filenames. The
    per-line condition counts name no outcome, so merged reports keep the
    larger count (a lower bound)."""
    tree = _xml_root(text, "coverage", "Cobertura XML")
    sources = [node.text.strip() for node in tree.iter("source") if node.text]
    prefix = sources[0].rstrip("/\\") + "/" if len(sources) == 1 else ""
    raw: RawCoverage = {}
    for class_node in tree.iter("class"):
        filename = class_node.attrib.get("filename", "")
        if not filename:
            continue
        if not Path(filename).is_absolute():
            filename = prefix + filename
        _add_lines(
            _file(raw, filename), class_node.findall("lines/line"), _add_cobertura_line
        )
    return raw


LineReader = Callable[[FileCoverage, Mapping[str, str]], None]


def _add_lines(
    coverage: FileCoverage, nodes: Iterable[ET.Element], read: LineReader
) -> None:
    """Feed every ``line`` element of one source file to its format's reader."""
    for node in nodes:
        read(coverage, node.attrib)


def _add_cobertura_line(coverage: FileCoverage, attributes: Mapping[str, str]) -> None:
    number = int(attributes["number"])
    coverage.add_line(number, int(float(attributes.get("hits", "0"))))
    condition = _CONDITION.search(attributes.get("condition-coverage", ""))
    if attributes.get("branch") == "true" and condition:
        coverage.add_branches(number, int(condition.group(1)), int(condition.group(2)))


def parse_jacoco(text: str) -> RawCoverage:
    """JaCoCo XML: ``package/sourcefile`` names a file by package path; each
    ``line`` has missed/covered instruction (mi/ci) and branch (mb/cb) counts."""
    tree = _xml_root(text, "report", "JaCoCo XML")
    raw: RawCoverage = {}
    for package in tree.iter("package"):
        directory = package.attrib.get("name", "").strip("/")
        for source in package.findall("sourcefile"):
            name = source.attrib.get("name", "")
            path = f"{directory}/{name}" if directory else name
            _add_lines(_file(raw, path), source.findall("line"), _add_jacoco_line)
    return raw


def _add_jacoco_line(coverage: FileCoverage, attributes: Mapping[str, str]) -> None:
    number = int(attributes["nr"])
    missed = int(attributes.get("mi", "0"))
    covered = int(attributes.get("ci", "0"))
    if missed + covered:
        coverage.add_line(number, covered)
    missed_branches = int(attributes.get("mb", "0"))
    covered_branches = int(attributes.get("cb", "0"))
    if missed_branches + covered_branches:
        coverage.add_branches(
            number, covered_branches, missed_branches + covered_branches
        )


# --- Go cover profile --------------------------------------------------------

_GO_BLOCK = re.compile(r"^(.+):(\d+)\.\d+,(\d+)\.\d+ (\d+) (\d+)$")
_GO_MODULE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)


def go_module_path(go_mod_text: str) -> str | None:
    match = _GO_MODULE.search(go_mod_text)
    return match.group(1).strip('"') if match else None


def parse_go_cover(text: str, module_path: str | None = None) -> RawCoverage:
    """``go test -coverprofile`` output. Every line of a block gets the block's
    count; the module prefix is removed so paths become module-relative."""
    raw: RawCoverage = {}
    prefix = module_path.rstrip("/") + "/" if module_path else None
    for line in text.splitlines():
        match = _GO_BLOCK.match(line.strip())
        if not match:
            continue
        filename, start, end, statements, count = match.groups()
        if prefix and filename.startswith(prefix):
            filename = filename[len(prefix) :]
        if int(statements) == 0:
            continue
        coverage = _file(raw, filename)
        for number in range(int(start), int(end) + 1):
            coverage.add_line(number, int(count))
    return raw


# --- llvm-cov JSON export (Swift, Rust, C/C++) -------------------------------


def parse_llvm_cov_json(text: str) -> RawCoverage:
    """``llvm-cov export`` JSON. Line counts follow llvm-cov's own rule: a line
    is executable when a counted region starts on it or a counted region
    wraps into it, and its count is the largest of those regions."""
    value = _json_object(text, ("data",), "llvm-cov JSON export")
    raw: RawCoverage = {}
    for export in value["data"]:
        for details in export.get("files", []):
            coverage = _file(raw, str(details["filename"]))
            for number, hits in _llvm_line_counts(details.get("segments", [])):
                coverage.add_line(number, hits)
            for branch in details.get("branches", []):
                _add_llvm_branch(coverage, branch)
    return raw


def _add_llvm_branch(coverage: FileCoverage, branch: list[Any]) -> None:
    """``[line, column, end_line, end_column, true_count, false_count, ...]``;
    the region's position names its two outcomes."""
    region = f"{branch[1]}:{branch[2]}:{branch[3]}"
    coverage.add_outcome(int(branch[0]), region + ":true", int(branch[4]))
    coverage.add_outcome(int(branch[0]), region + ":false", int(branch[5]))


@dataclasses.dataclass(frozen=True)
class _Segment:
    line: int
    count: int
    has_count: bool
    region_entry: bool
    gap: bool

    @property
    def starts_region(self) -> bool:
        return self.has_count and self.region_entry and not self.gap


def _segments(rows: Iterable[list[Any]]) -> list[_Segment]:
    return [
        _Segment(
            int(row[0]),
            int(row[2]),
            bool(row[3]),
            bool(row[4]),
            bool(row[5]) if len(row) > 5 else False,
        )
        for row in rows
    ]


def _llvm_line_counts(rows: Iterable[list[Any]]) -> list[tuple[int, int]]:
    segments = _segments(rows)
    if not segments:
        return []
    by_line: dict[int, list[_Segment]] = {}
    for segment in segments:
        by_line.setdefault(segment.line, []).append(segment)
    counts: list[tuple[int, int]] = []
    wrapped: _Segment | None = None
    for number in range(segments[0].line, segments[-1].line + 1):
        starting = by_line.get(number, [])
        hits = _llvm_line_count(starting, wrapped)
        if hits is not None:
            counts.append((number, hits))
        if starting:
            wrapped = starting[-1]
    return counts


def _llvm_line_count(starting: list[_Segment], wrapped: _Segment | None) -> int | None:
    if starting and starting[0].region_entry and not starting[0].has_count:
        return None  # a skipped region (e.g. inactive #if) starts on this line
    counts = [segment.count for segment in starting if segment.starts_region]
    if wrapped is not None and (counts or wrapped.has_count):
        counts.append(wrapped.count)
    return max(counts) if counts else None


# --- Ruby stdlib Coverage (written by the recipe's helper) -------------------


def parse_ruby_coverage(text: str) -> RawCoverage:
    """``{"files": {path: {"lines": [null|hits, ...], "branches": [[line,
    target_id, hits], ...]}}}`` as the coverage recipe's Ruby helper writes
    it, one file per Ruby process. Target ids name each outcome, so outcomes
    taken in different processes add up when the files are merged."""
    value = _json_object(text, ("files",), "Ruby coverage")
    raw: RawCoverage = {}
    for filename, details in value["files"].items():
        coverage = _file(raw, str(filename))
        for index, hits in enumerate(details.get("lines") or []):
            if hits is not None:
                coverage.add_line(index + 1, int(hits))
        for line, target, hits in details.get("branches") or []:
            coverage.add_outcome(int(line), str(target), int(hits))
    return raw


# --- Dispatch ----------------------------------------------------------------

PARSERS: dict[str, Callable[[str], RawCoverage]] = {
    "coverage-json": parse_coverage_py_json,
    "istanbul-json": parse_istanbul_json,
    "lcov": parse_lcov,
    "cobertura": parse_cobertura,
    "jacoco": parse_jacoco,
    "go-cover": parse_go_cover,
    "llvm-cov-json": parse_llvm_cov_json,
    "ruby-coverage": parse_ruby_coverage,
    "gcov": parse_gcov,
}
FORMATS = tuple(PARSERS)

# When a report is a directory, these files inside it are read and merged.
_DIRECTORY_GLOBS = {
    "coverage-json": "*.json",
    "istanbul-json": "*.json",
    "lcov": "*.info",
    "cobertura": "*.xml",
    "jacoco": "*.xml",
    "go-cover": "*.out",
    "llvm-cov-json": "*.json",
    "ruby-coverage": "*.json",
    "gcov": "*.gcov",
}


# Name endings that settle a report's format without reading it.
_NAMED_FORMATS = (
    ("coverage-final.json", "istanbul-json"),
    (".info", "lcov"),
    (".out", "go-cover"),
    (".gcov", "gcov"),
)
_XML_ROOTS = {"coverage": "cobertura", "report": "jacoco"}

_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, ET.ParseError)

# Filesystems with coarse timestamps (FAT, HFS+, ext3) can stamp a report
# written just after the run started with a slightly earlier time.
MTIME_SLACK_SECONDS = 2.0


def _json_format(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    if "meta" in value and "files" in value:
        return "coverage-json"
    if "data" in value:
        return "llvm-cov-json"
    if "files" in value:
        return "ruby-coverage"
    if any(
        isinstance(item, dict) and "statementMap" in item for item in value.values()
    ):
        return "istanbul-json"
    return None


def _content_format(path: Path) -> str | None:
    """Sniff a JSON or XML report. Unparseable content gets the usual format
    for its extension, so the parser explains what is wrong with it."""
    suffix = path.suffix.lower()
    try:
        text = read_text(path)
        if suffix == ".json":
            return _json_format(json.loads(text))
        return _XML_ROOTS.get(ET.fromstring(text).tag)
    except _READ_ERRORS:
        return "coverage-json" if suffix == ".json" else "cobertura"


def _file_format(path: Path) -> str | None:
    name = path.name.lower()
    for ending, format_name in _NAMED_FORMATS:
        if name.endswith(ending):
            return format_name
    if path.suffix.lower() in (".json", ".xml"):
        return _content_format(path)
    return None


def guess_format(report: Path) -> str:
    """The format of a report, or of the first recognizable file in a report
    directory, from its name and content (``coverage.format: auto``)."""
    if report.is_dir():
        candidates = sorted(path for path in report.rglob("*") if path.is_file())
    else:
        candidates = [report]
    for path in candidates:
        found = _file_format(path)
        if found is not None:
            return found
    raise CoverageReportError(
        f"cannot tell the format of coverage report {report}; set coverage.format"
    )


def parse_report_text(text: str, format_name: str, root: Path) -> RawCoverage:
    """Parse one report's text. Go profiles need the module path from go.mod."""
    if format_name == "go-cover":
        go_mod = root / "go.mod"
        module = go_module_path(read_text(go_mod)) if go_mod.is_file() else None
        return parse_go_cover(text, module)
    parser = PARSERS.get(format_name)
    if parser is None:
        raise CoverageReportError(f"unknown coverage format {format_name!r}")
    return parser(text)


def _modified_at(path: Path) -> float:
    """A file's modification time; minus infinity for anything unreadable."""
    try:
        return path.stat().st_mtime if path.is_file() else float("-inf")
    except OSError:
        return float("-inf")


def _written_since(path: Path, started_at: float) -> bool:
    return _modified_at(path) >= started_at - MTIME_SLACK_SECONDS


def _directory_files(report: Path, format_name: str) -> list[Path]:
    pattern = _DIRECTORY_GLOBS.get(format_name, "*")
    return sorted(path for path in report.rglob(pattern) if path.is_file())


def _report_files(
    report: Path, format_name: str, started_at: float | None
) -> list[Path]:
    """The report file, or the matching files of a report directory. With
    ``started_at``, files older than the run are left out, so stale or
    planted reports in a directory the recipe does not clean never count."""
    if report.is_file():
        return [report]
    if not report.is_dir():
        raise CoverageReportError(f"coverage report not found: {report}")
    found = [
        path
        for path in _directory_files(report, format_name)
        if started_at is None or _written_since(path, started_at)
    ]
    if not found:
        pattern = _DIRECTORY_GLOBS.get(format_name, "*")
        when = "" if started_at is None else " written by this run"
        raise CoverageReportError(f"no {pattern} coverage files{when} in {report}")
    return found


def read_coverage_report(
    report: Path,
    format_name: str,
    root: Path,
    known_paths: Iterable[str] | None = None,
    started_at: float | None = None,
) -> CoverageData:
    """Read a report file (or every matching file in a report directory) and
    key the result by repository path. ``known_paths`` defaults to every
    repository file; ``started_at`` (a ``time.time()`` value taken before the
    test run) skips directory files the run did not write. Raises
    ``CoverageReportError`` when the report is missing, unreadable, or in
    another format."""
    if format_name == "auto":
        format_name = guess_format(report)
    raw: RawCoverage = {}
    for path in _report_files(report, format_name, started_at):
        try:
            parsed = parse_report_text(read_text(path), format_name, root)
        except _READ_ERRORS as error:
            raise CoverageReportError(f"cannot read {path}: {error}") from error
        _merge_into(raw, parsed)
    if known_paths is None:
        known_paths = [normalize_path(path, root) for path in walk_files(root)]
    return resolve_report_paths(raw, root, known_paths)


def _merge_into(
    target: dict[str, FileCoverage], source: Mapping[str, FileCoverage]
) -> None:
    for path, coverage in source.items():
        target.setdefault(path, FileCoverage()).merge(coverage)


def report_is_fresh(report: Path, started_at: float, format_name: str = "auto") -> bool:
    """True when the report, or a file of this format in a report directory,
    was written at or after ``started_at`` (a ``time.time()`` value)."""
    if report.is_dir():
        candidates = _directory_files(report, format_name)
    else:
        candidates = [report]
    return any(_written_since(path, started_at) for path in candidates)


def resolve_report_paths(
    raw: Mapping[str, FileCoverage], root: Path, known_paths: Iterable[str]
) -> CoverageData:
    """Re-key raw report entries by repository path (see
    ``gate_coverage_paths.match_report_paths``); unmatched entries are
    dropped and entries naming the same file are merged."""
    data: CoverageData = {}
    for report_path, path in match_report_paths(raw, root, known_paths).items():
        data.setdefault(path, FileCoverage()).merge(raw[report_path])
    return data
