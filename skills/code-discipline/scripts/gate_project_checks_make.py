"""Read a Makefile well enough to know what ``make <target>`` runs.

The project-checks gate refuses checks that cannot fail, so it must see the
recipe lines make really executes for a target: the recipes of its
prerequisites, inline ``target: ; command`` recipes, lines after blank or
comment lines, both branches of ``ifdef`` blocks, and the last recipe when a
target is defined twice. It also reports what makes make ignore a failure:
the ``-`` line prefix and ``.IGNORE``. Variables are expanded from simple
assignments so ``$(MAKE) lint-impl`` reads as ``make lint-impl``.
"""

from __future__ import annotations

import dataclasses
import re

_ASSIGNMENT = re.compile(
    r"^(?:(?:export|override)\s+)*([^\s:#=]+)\s*(\+=|\?=|!=|:{0,3}=)\s*(.*)$"
)
_RULE = re.compile(r"^([^:#=]+?)\s*::?(?!=)\s*(.*)$")
_REFERENCE = re.compile(r"\$\$|\$\(([\w.-]+)\)|\$\{([\w.-]+)\}")
_CONDITIONAL = re.compile(r"^\s*(?:ifeq|ifneq|ifdef|ifndef|else|endif)\b")
_RECIPE_PREFIX = re.compile(r"^[@+\-\s]*")
_ERREXIT_FLAG = re.compile(r"(?:^|\s)-[A-Za-z]*e")
MAX_EXPANSION_DEPTH = 8


@dataclasses.dataclass(frozen=True)
class RecipeLine:
    text: str
    ignores_errors: bool


@dataclasses.dataclass
class Rule:
    prerequisites: list[str] = dataclasses.field(default_factory=list)
    recipe: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Entry:
    """One rule line and the recipe lines written under it."""

    targets: list[str]
    prerequisites: list[str]
    lines: list[str]


@dataclasses.dataclass
class Makefile:
    rules: dict[str, Rule] = dataclasses.field(default_factory=dict)
    variables: dict[str, str] = dataclasses.field(
        default_factory=lambda: {"MAKE": "make"}
    )
    default_goal: str | None = None

    def expand(self, text: str, depth: int = 0) -> str:
        def replace(match: re.Match[str]) -> str:
            if match.group() == "$$":
                return "$"
            name = match.group(1) or match.group(2)
            if name not in self.variables or depth >= MAX_EXPANSION_DEPTH:
                return match.group()
            return self.expand(self.variables[name], depth + 1)

        return _REFERENCE.sub(replace, text)

    def ignores_errors(self, target: str) -> bool:
        """``.IGNORE:`` with no prerequisites covers every target."""
        ignored = self.rules.get(".IGNORE")
        return ignored is not None and (
            not ignored.prerequisites or target in ignored.prerequisites
        )

    @property
    def errexit(self) -> bool:
        """``.POSIX`` or ``.SHELLFLAGS = -ec`` runs recipes with ``sh -e``."""
        flags = self.variables.get(".SHELLFLAGS", "")
        return ".POSIX" in self.rules or bool(_ERREXIT_FLAG.search(flags))

    def recipe_lines(self, target: str) -> list[RecipeLine]:
        """What the shell runs for ``target``, one entry per shell."""
        lines = [_recipe_line(self.expand(line)) for line in self.rules[target].recipe]
        if ".ONESHELL" in self.rules and lines:
            text = "\n".join(line.text for line in lines)
            return [RecipeLine(text, lines[0].ignores_errors)]
        return lines


def _recipe_line(line: str) -> RecipeLine:
    """Strip make's ``@``, ``-``, and ``+`` prefixes; ``-`` ignores errors."""
    prefix = _RECIPE_PREFIX.match(line)
    end = prefix.end() if prefix else 0
    return RecipeLine(line[end:], "-" in line[:end])


class _Parser:
    def __init__(self) -> None:
        self.makefile = Makefile()
        self.entries: list[_Entry] = []
        self.open: _Entry | None = None
        self.in_define = False

    def line(self, line: str) -> None:
        if self.in_define:
            self.in_define = line.strip() != "endef"
        elif line.startswith("\t"):
            if self.open is not None:
                self.open.lines.append(line[1:])
        elif not line.strip() or line.lstrip().startswith("#"):
            return  # blank and comment lines do not end a recipe
        elif _CONDITIONAL.match(line):
            return  # both branches count
        else:
            self.open = None
            self._statement(line)

    def _statement(self, line: str) -> None:
        if line.strip().split(" ", 1)[0] == "define":
            self.in_define = True
            return
        assignment = _ASSIGNMENT.match(line)
        if assignment:
            self._assign(*assignment.groups())
            return
        rule = _RULE.match(line)
        if rule:
            self._rule(rule.group(1), rule.group(2))

    def _assign(self, name: str, operator: str, value: str) -> None:
        variables = self.makefile.variables
        if operator == "+=":
            variables[name] = f"{variables.get(name, '')} {value}".strip()
        elif operator == "?=":
            variables.setdefault(name, value)
        elif operator != "!=":
            variables[name] = value

    def _rule(self, targets_text: str, rest: str) -> None:
        prerequisites, _, inline = rest.partition(";")
        prerequisites = prerequisites.split("#", 1)[0]
        if "=" in prerequisites:
            return  # a target-specific variable, not a rule
        targets = self.makefile.expand(targets_text).split()
        lines = [inline.strip()] if inline.strip() else []
        self.open = _Entry(targets, self.makefile.expand(prerequisites).split(), lines)
        self.entries.append(self.open)

    def finish(self) -> Makefile:
        """Prerequisites accumulate across rules; the last recipe wins."""
        rules = self.makefile.rules
        for entry in self.entries:
            for target in entry.targets:
                rule = rules.setdefault(target, Rule())
                rule.prerequisites.extend(entry.prerequisites)
                if entry.lines:
                    rule.recipe = entry.lines
        self.makefile.default_goal = self.makefile.variables.get(
            ".DEFAULT_GOAL"
        ) or next(
            (
                target
                for entry in self.entries
                for target in entry.targets
                if not target.startswith(".") and "%" not in target
            ),
            None,
        )
        return self.makefile


def parse_makefile(text: str) -> Makefile:
    parser = _Parser()
    for line in text.replace("\\\n", " ").splitlines():
        parser.line(line)
    return parser.finish()
