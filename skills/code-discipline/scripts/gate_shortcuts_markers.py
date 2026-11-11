"""The marker table for the "No shortcuts" gate: every comment, annotation,
or call that switches a check off, with the languages where it applies.

This file spells every marker out, so the marker scan exempts it by name."""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Iterable
from pathlib import Path

import source_files

SUPPRESSION = "suppression"
COVERAGE_EXCLUSION = "coverage exclusion"
TEST_SKIP = "test skip"
TEST_FOCUS = "test focus"


@dataclasses.dataclass(frozen=True)
class Marker:
    """A comment, annotation, or call that switches a check off for some code.

    ``extensions`` limits a marker whose spelling is common elsewhere (an
    iterator's ``it.skip(1)``, Ruby's ``skip``) to the languages where it
    means a skip; ``tests_only`` further limits it to test files.
    """

    label: str
    kind: str
    pattern: re.Pattern[str]
    extensions: frozenset[str] = frozenset()
    tests_only: bool = False

    def applies_to(self, relative: str) -> bool:
        if self.extensions and Path(relative).suffix.lower() not in self.extensions:
            return False
        return not self.tests_only or source_files.is_test_path(relative)


@dataclasses.dataclass(frozen=True)
class MarkerHit:
    """A marker found on a line, with the text that matched it."""

    marker: Marker
    text: str
    column: int = 0


def _marker(
    label: str,
    kind: str,
    pattern: str,
    extensions: Iterable[str] = (),
    *,
    tests_only: bool = False,
) -> Marker:
    return Marker(label, kind, re.compile(pattern), frozenset(extensions), tests_only)


PYTHON = (".py", ".pyi")
JAVASCRIPT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
JVM = (".java", ".kt", ".kts", ".groovy", ".scala")
GO = (".go",)
RUBY = (".rb",)

# Receivers that test frameworks hang ``.skip`` / ``.only`` on, possibly
# through modifiers such as ``test.concurrent.skip``.
_TEST_RECEIVER = r"\b(?:it|describe|test|context|suite|specify|bench)(?:\.\w+)*?"
# An inline mypy setting that weakens checking: ignoring errors, an
# ``allow-``/``implicit-``/``no-`` option, or an option set to false.
_MYPY_WEAKENING = (
    r"#\s*mypy:(?=[^#\n]*?(?:ignore-errors|disable-error-code"
    r"|(?<![\w-])(?:allow-|implicit-|no-(?!implicit-reexport))"
    r"|=\s*(?:[Ff]alse|0)\b))[^#\n]*"
)

MARKERS: tuple[Marker, ...] = (
    _marker("# type: ignore", SUPPRESSION, r"#\s*type:\s*ignore\b"),
    _marker("@ts-ignore", SUPPRESSION, r"@ts-ignore\b"),
    _marker("@ts-expect-error", SUPPRESSION, r"@ts-expect-error\b"),
    _marker("@ts-nocheck", SUPPRESSION, r"@ts-nocheck\b"),
    _marker("eslint-disable", SUPPRESSION, r"\beslint-disable"),
    _marker(
        "eslint rule off",
        SUPPRESSION,
        r"/\*\s*eslint\s+[^*]*:\s*\[?\s*(?:0|[\"']?off)\b",
    ),
    _marker("biome-ignore", SUPPRESSION, r"\bbiome-ignore"),
    _marker("deno-lint-ignore", SUPPRESSION, r"\bdeno-lint-ignore"),
    # ruff and flake8 read noqa in any case. A noqa for E402 alone is allowed:
    # it only lets a script put its own directory on sys.path before
    # importing a sibling module, and hides no defect.
    _marker(
        "# noqa",
        SUPPRESSION,
        r"(?i:#\s*(?:ruff:\s*|flake8:\s*)?noqa\b(?!:\s*E402\s*$))",
    ),
    _marker("# pylint: disable", SUPPRESSION, r"#\s*pylint:\s*(?:disable|skip-file)"),
    _marker(
        "# pyright:", SUPPRESSION, r"#\s*pyright:(?!\s*strict\s*$)\s*[^#\s][^#\n]*"
    ),
    _marker("# mypy:", SUPPRESSION, _MYPY_WEAKENING),
    _marker("//nolint", SUPPRESSION, r"//\s*nolint\b"),
    _marker("//lint:ignore", SUPPRESSION, r"//\s*lint:(?:file-)?ignore\b"),
    _marker("NOLINT", SUPPRESSION, r"\bNOLINT(?:NEXTLINE|BEGIN)?\b"),
    _marker("#[allow(", SUPPRESSION, r"#!?\[(?:allow|expect)\("),
    _marker(
        "#[cfg_attr(..., allow(",
        SUPPRESSION,
        r"#!?\[cfg_attr\([^\]]*\b(?:allow|expect)\(",
    ),
    _marker("@SuppressWarnings", SUPPRESSION, r"@SuppressWarnings\b"),
    _marker("@SuppressLint", SUPPRESSION, r"@SuppressLint\b"),
    _marker("@Suppress(", SUPPRESSION, r"@Suppress\("),
    _marker("[SuppressMessage(", SUPPRESSION, r"\[(?:assembly:\s*)?SuppressMessage\("),
    _marker("#pragma warning disable", SUPPRESSION, r"#\s*pragma\s+warning\s+disable"),
    _marker(
        "#pragma diagnostic ignored",
        SUPPRESSION,
        r"#\s*pragma\s+(?:clang|GCC)\s+diagnostic\s+ignored",
    ),
    _marker("NOSONAR", SUPPRESSION, r"\bNOSONAR\b"),
    _marker("rubocop:disable", SUPPRESSION, r"\brubocop:\s*disable"),
    _marker("swiftlint:disable", SUPPRESSION, r"\bswiftlint:\s*disable"),
    _marker("@phpstan-ignore", SUPPRESSION, r"@phpstan-ignore"),
    _marker("@psalm-suppress", SUPPRESSION, r"@psalm-suppress\b"),
    # coverage.py's default exclusion accepts upper-case spellings too.
    _marker(
        "pragma: no cover", COVERAGE_EXCLUSION, r"(?i:#\s*pragma[:\s]?\s*no\s*cover)"
    ),
    _marker(
        "pragma: no branch", COVERAGE_EXCLUSION, r"(?i:#\s*pragma[:\s]?\s*no\s*branch)"
    ),
    _marker("istanbul ignore", COVERAGE_EXCLUSION, r"\bistanbul\s+ignore\b"),
    _marker("c8 ignore", COVERAGE_EXCLUSION, r"\bc8\s+ignore\b"),
    _marker("v8 ignore", COVERAGE_EXCLUSION, r"\bv8\s+ignore\b"),
    _marker("lizard forgive", COVERAGE_EXCLUSION, r"\blizard\s+forgive"),
    _marker("coverage:ignore", COVERAGE_EXCLUSION, r"\bcoverage:ignore"),
    _marker("LCOV_EXCL", COVERAGE_EXCLUSION, r"\bLCOV_EXCL"),
    # JaCoCo drops methods annotated @Generated. @GeneratedValue (JPA) is not
    # matched: it is ordinary entity mapping.
    _marker("@Generated", COVERAGE_EXCLUSION, r"@(?:\w+\.)*Generated\b", JVM),
    _marker(
        "ExcludeFromCodeCoverage", COVERAGE_EXCLUSION, r"\bExcludeFromCodeCoverage\b"
    ),
    _marker("pytest.mark.skip", TEST_SKIP, r"\bpytest\.mark\.skip(?:if)?\b"),
    _marker("pytest.mark.xfail", TEST_SKIP, r"\bpytest\.mark\.xfail\b"),
    _marker(
        "pytest.mark.skip",
        TEST_SKIP,
        r"(?<![\w.])mark\.(?:skip(?:if)?|xfail)\b",
        PYTHON,
    ),
    _marker("pytest.skip(", TEST_SKIP, r"\bpytest\.(?:skip|xfail|importorskip)\s*\("),
    _marker(
        "@unittest.skip",
        TEST_SKIP,
        r"^\s*@\(?\s*(?:\w+\.)*skip(?:If|Unless)?\b",
        PYTHON,
    ),
    _marker(
        "@unittest.expectedFailure",
        TEST_SKIP,
        r"^\s*@\(?\s*(?:\w+\.)*expectedFailure\b",
        PYTHON,
    ),
    _marker("SkipTest", TEST_SKIP, r"\bSkipTest\b", PYTHON),
    _marker("self.skipTest(", TEST_SKIP, r"\.skipTest\s*\("),
    _marker(
        ".skip(",
        TEST_SKIP,
        _TEST_RECEIVER + r"\.(?:skip|skipIf|runIf|todo|fails|failing)\b",
        JAVASCRIPT,
    ),
    _marker(".only(", TEST_FOCUS, _TEST_RECEIVER + r"\.only\b", JAVASCRIPT),
    _marker("this.skip(", TEST_SKIP, r"\bthis\.skip\s*\(", JAVASCRIPT),
    # ``xit``/``fit`` take a test name; requiring it keeps ``fit(x, y)`` out.
    _marker(
        "xit(",
        TEST_SKIP,
        r"(?<![\w.$])x(?:it|describe|test|context|specify)(?=\s*\(\s*[\"'`])",
        JAVASCRIPT,
    ),
    _marker(
        "fit(",
        TEST_FOCUS,
        r"(?<![\w.$])f(?:it|describe|context)(?=\s*\(\s*[\"'`])",
        JAVASCRIPT,
    ),
    # RSpec and Minitest calls, only at the start of a statement in a test file.
    _marker(
        "xit(",
        TEST_SKIP,
        r"^\s*x(?:it|describe|context|specify)\b(?=\s*(?:$|[(\"':#]|do\b))",
        RUBY,
        tests_only=True,
    ),
    _marker(
        "fit(",
        TEST_FOCUS,
        r"^\s*f(?:it|describe|context)\b(?=\s*(?:$|[(\"':#]|do\b))",
        RUBY,
        tests_only=True,
    ),
    _marker(
        "skip/pending",
        TEST_SKIP,
        r"^\s*(?:skip|pending)\b(?=\s*(?:$|[(\"'#]|(?:if|unless|do)\b))",
        RUBY,
        tests_only=True,
    ),
    _marker("t.Skip(", TEST_SKIP, r"\b(?:t|b|tb|f)\.Skip(?:Now|f)?\s*\(", GO),
    _marker("#[ignore]", TEST_SKIP, r"#\[ignore\b"),
    _marker("@Ignore", TEST_SKIP, r"@Ignore\b"),
    _marker("@Disabled", TEST_SKIP, r"@Disabled"),
    _marker("[Ignore]", TEST_SKIP, r"\[Ignore\b"),
    _marker("[Fact(Skip =", TEST_SKIP, r"\[(?:Fact|Theory)\s*\([^\]]*\bSkip\s*="),
    _marker("XCTSkip", TEST_SKIP, r"\bXCTSkip"),
)


@functools.lru_cache(maxsize=4096)
def markers_for(relative: str) -> tuple[Marker, ...]:
    """The markers that apply to one file, worked out once per file."""
    return tuple(marker for marker in MARKERS if marker.applies_to(relative))


def marker_in_line(line: str, relative: str) -> MarkerHit | None:
    """The first marker on ``line``, so one line yields at most one finding."""
    for marker in markers_for(relative):
        match = marker.pattern.search(line)
        if match:
            return MarkerHit(marker, match.group(0).strip(), match.start())
    return None
