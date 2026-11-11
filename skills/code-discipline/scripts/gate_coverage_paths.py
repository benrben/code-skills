"""Map the file paths a coverage report writes onto repository files.

Reports name files in many ways: absolute paths from this checkout or a CI
machine, ``file://`` URLs, Windows separators, Go import paths, or JaCoCo
package paths. Matching works on whole path segments, so ``studio.py``
never matches ``io.py``, and an ambiguous match is dropped rather than
guessed: a wrong match would credit one file with another file's tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

_DRIVE = re.compile(r"^[A-Za-z]:/")


def _segments_of(path: str) -> list[str]:
    return [
        part for part in path.replace("\\", "/").split("/") if part not in ("", ".")
    ]


def _suffix_score(report_parts: list[str], repo_parts: list[str]) -> int:
    """How many trailing segments match when one path is a whole-segment
    suffix of the other; 0 otherwise."""
    size = min(len(report_parts), len(repo_parts))
    if report_parts[-size:] == repo_parts[-size:]:
        return size
    return 0


def _common_tail(report_parts: list[str], repo_parts: list[str]) -> int:
    """How many trailing segments the two paths share."""
    count = 0
    for report_part, repo_part in zip(
        reversed(report_parts), reversed(repo_parts), strict=False
    ):
        if report_part != repo_part:
            break
        count += 1
    return count


def _best(
    candidates: Iterable[str], score: Callable[[str], int], minimum: int
) -> list[str]:
    """The candidates sharing the highest score, when it reaches ``minimum``."""
    scores = {candidate: score(candidate) for candidate in candidates}
    top = max(scores.values(), default=0)
    if top < minimum:
        return []
    return [candidate for candidate, value in scores.items() if value == top]


class ReportPathResolver:
    """Maps report paths onto known repository paths.

    First the repository root is stripped from absolute paths. A path that
    then names a known file is an exact match. Otherwise the known file that
    shares the longest whole-segment suffix wins. When no file is a whole
    suffix (Kotlin drops the common root package from directories, so
    ``org/example/app/net/Socket.kt`` lives in ``src/main/kotlin/net/``), the
    file sharing the most trailing segments wins: at least the name plus one
    directory, or only the name for a package-relative path. Either way the
    winner must be the only file with that score.
    """

    def __init__(self, root: Path, known_paths: Iterable[str]) -> None:
        self._prefixes = sorted(
            {
                root.as_posix().rstrip("/") + "/",
                root.resolve().as_posix().rstrip("/") + "/",
            },
            key=len,
            reverse=True,
        )
        self._known = set(known_paths)
        self._by_name: dict[str, list[str]] = {}
        for path in self._known:
            self._by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    def _strip_root(self, report_path: str) -> str:
        text = report_path.replace("\\", "/").removeprefix("file://")
        for prefix in self._prefixes:
            if text.startswith(prefix):
                return text[len(prefix) :]
        return text

    def relative(self, report_path: str) -> str:
        return "/".join(_segments_of(self._strip_root(report_path)))

    def exact(self, report_path: str) -> str | None:
        relative = self.relative(report_path)
        return relative if relative in self._known else None

    def by_suffix(self, report_path: str, excluded: Iterable[str] = ()) -> str | None:
        stripped = self._strip_root(report_path)
        parts = _segments_of(stripped)
        if not parts:
            return None
        blocked = set(excluded)
        candidates = [
            path for path in self._by_name.get(parts[-1], []) if path not in blocked
        ]
        best = _best(candidates, lambda path: _suffix_score(parts, path.split("/")), 1)
        if not best:
            outside = stripped.startswith("/") or bool(_DRIVE.match(stripped))
            minimum = 2 if outside else 1
            best = _best(
                candidates, lambda path: _common_tail(parts, path.split("/")), minimum
            )
        return best[0] if len(best) == 1 else None


def match_report_paths(
    report_paths: Iterable[str], root: Path, known_paths: Iterable[str]
) -> dict[str, str]:
    """Report path -> repository path for every report path that matches.

    Exact matches are resolved first, so a dependency's ``src/lib.rs`` can
    never take over a file the report already names exactly. A suffix match
    is kept only when no other unresolved report path claims the same file.
    Unmatched and ambiguous report paths are left out.
    """
    resolver = ReportPathResolver(root, known_paths)
    matched: dict[str, str] = {}
    pending: list[str] = []
    for report_path in report_paths:
        exact = resolver.exact(report_path)
        if exact is None:
            pending.append(report_path)
        else:
            matched[report_path] = exact
    claimed = set(matched.values())
    claims: dict[str, list[str]] = {}
    for report_path in pending:
        match = resolver.by_suffix(report_path, claimed)
        if match is not None:
            claims.setdefault(match, []).append(report_path)
    for path, claimants in claims.items():
        if len(claimants) == 1:
            matched[claimants[0]] = path
    return matched
