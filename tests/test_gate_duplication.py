import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diff_scope  # noqa: E402
import gate_duplication  # noqa: E402
import gate_duplication_tokens  # noqa: E402
from gate_duplication import Clone  # noqa: E402
from gate_duplication_tokens import RawToken, Token  # noqa: E402
from gate_types import BLOCKED, FAIL, PASS, GateOutcome, ToolPython  # noqa: E402

ORIGINAL = """\
import os
import sys


def summarize_orders(orders, minimum):
    total = 0
    count = 0
    for order in orders:
        if order.amount > minimum and order.status == "paid":
            total += order.amount * 1.2
            count += 1
    average = total / count if count else 0
    return {"total": total, "count": count, "average": average}
"""

RENAMED_COPY = """\
import json


def tally_invoices(invoices, floor):
    sum_of_amounts = 0
    seen = 0
    for invoice in invoices:
        if invoice.amount > floor and invoice.status == "open":
            sum_of_amounts += invoice.amount * 3.5
            seen += 1
    mean = sum_of_amounts / seen if seen else 0
    return {"sum": sum_of_amounts, "seen": seen, "mean": mean}
"""

UNRELATED = """\
def greet(name):
    return "hello " + name
"""

JS_ORIGINAL = """\
export function shippingCost(items, region) {
  let weight = 0;
  for (const item of items) {
    if (item.fragile && region !== "local") {
      weight += item.weight * 2;
    } else {
      weight += item.weight;
    }
  }
  return weight > 20 ? weight * 1.5 : weight + 4;
}
"""

JS_COMMENTED_COPY = """\
// Copied and reformatted.
export function parcelPrice(parcels, zone) {

  let mass = 0; // running total

  /* walk every parcel */
  for (const parcel of parcels) {

    if (parcel.fragile && zone !== "abroad") {
      // fragile items cost double
      mass += parcel.weight * 3;
    } else {
      mass += parcel.weight;
    }

  }
  return mass > 50 ? mass * 2.5 : mass + 9;
}
"""

PY_IMPORTS = """\
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import (
    Any,
    NamedTuple,
    Protocol,
    TypeVar,
)
"""

JS_IMPORTS = """\
import React from "react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { render, screen, waitFor } from "@testing-library/react";
import styles from "./styles.module.css";
const fs = require("fs");
const path = require("path");
export { alpha, beta, gamma, delta } from "./letters";
export * from "./more";
"""

GO_IMPORTS = """\
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)
"""


RUST_USES = (
    "".join(
        f"pub use crate::module{index}::{{Alpha{index}, Beta{index}, Gamma{index}}};\n"
        for index in range(8)
    )
    + "pub(crate) use super::shared::{\n    One,\n    Two,\n};\n"
)

TS_REEXPORTS = (
    "".join(
        f"export type {{ Alpha{index}, Beta{index} }} from './mod{index}';\n"
        for index in range(8)
    )
    + "export * as everything from './all';\nexport type * from './types';\n"
)

CS_USINGS = "".join(
    f"global using Company.Product.Module{index}.Feature;\n" for index in range(10)
)

JAVA_MAPPER = """\
package a;
public class UserMapper {
    public UserDto toDto(User user) {
        UserDto dto = new UserDto();
        dto.setId(user.getId());
        dto.setName(user.getName());
        dto.setEmail(user.getEmail());
        dto.setPhone(user.getPhone());
        dto.setAddress(user.getAddress());
        dto.setCity(user.getCity());
        dto.setCountry(user.getCountry());
        dto.setCreated(user.getCreated());
        return dto;
    }
}
"""

PY_CALLS = """\
def publish_report(report, output, config):
    output.write_header(report.title, config.theme)
    output.write_summary(report.summary, config.locale)
    output.write_table(report.rows, report.columns, config.widths)
    output.write_chart(report.series, config.palette)
    output.write_footer(report.author, report.date, config.locale)
    output.flush(config.timeout)
    output.close()
"""

FIELDS = [f"field_{index}" for index in range(12)]
CONSTRUCTOR = (
    "class Settings:\n"
    f"    def __init__(self, {', '.join(FIELDS)}):\n"
    + "".join(f"        self.{field} = {field}\n" for field in FIELDS)
)

OPTIONS = ["input", "output", "format", "level", "workers", "timeout", "retries"]
ARGPARSE = (
    "import argparse\n\n\ndef build_parser():\n"
    "    parser = argparse.ArgumentParser()\n"
    + "".join(
        f'    parser.add_argument("--{name}", default=None, help="Set {name}.")\n'
        for name in OPTIONS * 3
    )
    + "    return parser\n"
)

GO_SWITCH = (
    "package sym\n\nfunc Name(kind int) string {\n\tswitch kind {\n"
    + "".join(f'\tcase {index}:\n\t\treturn "k{index}"\n' for index in range(20))
    + '\t}\n\treturn ""\n}\n'
)

SOLIDITY = """\
pragma solidity ^0.8.0;
contract Ledger {
    function settle(uint total, uint fee) public pure returns (uint) {
        uint counter = total;
        if (counter > fee) {
            counter = counter - fee;
        } else {
            counter = 0;
        }
        return counter * 2 + fee;
    }
}
"""


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def commit_all(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")


def clones_in(root: Path, min_tokens: int = 50, min_lines: int = 5) -> list[Clone]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    files = [path for path in files if ".git" not in path.parts]
    return gate_duplication.find_clones(
        ToolPython(), root, files, min_tokens, min_lines
    )


def raw(kind: str, value: str, line: int) -> RawToken:
    return RawToken(f"Token.{kind}", value, line)


def is_import_line(line: list[RawToken]) -> bool:
    return gate_duplication_tokens.import_span(line) is not None


def tokens(texts: str, first_line: int = 1, per_line: int = 10) -> list[Token]:
    words = texts.split()
    return [
        Token(word, first_line + index // per_line) for index, word in enumerate(words)
    ]


def local_change(**added: frozenset[int]) -> diff_scope.ChangeSet:
    files = {path: diff_scope.FileChange(path, lines) for path, lines in added.items()}
    return diff_scope.ChangeSet(diff_scope.LOCAL_CHANGES, "HEAD", files)


def clone(a: str = "a.py", b: str = "b.py", start: int = 1, end: int = 9) -> Clone:
    return Clone(a, start, end, b, start + 20, end + 20, 60)


class NormalizationTests(unittest.TestCase):
    def test_names_strings_and_numbers_are_normalized(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [
                raw("Name.Function", "total", 1),
                raw("Operator", "=", 1),
                raw("Literal.String.Double", '"', 1),
                raw("Literal.String.Escape", "\\n", 1),
                raw("Literal.String.Double", '"', 1),
                raw("Operator", "+", 1),
                raw("Literal.Number.Float", "1.5", 1),
                raw("Keyword", "return", 2),
            ]
        )
        self.assertEqual(
            [token.text for token in result], ["N", "=", "S", "+", "0", "return"]
        )

    def test_comments_are_dropped(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [
                raw("Comment.Single", "# note", 1),
                raw("Keyword", "pass", 2),
                raw("Comment.Preproc", "#include", 3),
            ]
        )
        self.assertEqual(result, [Token("pass", 2)])

    def test_multi_line_import_statement_is_dropped(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [
                raw("Keyword.Namespace", "from", 1),
                raw("Name.Namespace", "x", 1),
                raw("Keyword.Namespace", "import", 1),
                raw("Punctuation", "(", 1),
                raw("Name", "a", 2),
                raw("Punctuation", ",", 2),
                raw("Punctuation", ")", 3),
                raw("Keyword", "pass", 4),
            ]
        )
        self.assertEqual(result, [Token("pass", 4)])

    def test_import_line_recognition(self) -> None:
        cases = {
            "require declaration": (["const", "fs", "=", "require", "("], True),
            "plain declaration": (["const", "fs", "=", "load", "("], False),
            "re-export list": (["export", "{", "a", "}"], True),
            "re-export all": (["export", "*", "from"], True),
            "type re-export": (["export", "type", "{", "A", "}"], True),
            "type alias": (["export", "type", "Foo", "=", "{"], False),
            "exported function": (["export", "function", "f"], False),
            "lone export": (["export"], False),
            "variable named use": (["use", "=", "3"], False),
            "react use call": (["use", "(", "promise", ")", ";"], False),
            "using statement": (["using", "(", "var"], False),
            "using directive": (["using", "System", ";"], True),
            "global using": (["global", "using", "System", ";"], True),
            "rust pub use": (["pub", "use", "crate", "::", "a", ";"], True),
            "rust pub(crate) use": (["pub", "(", "crate", ")", "use", "x", ";"], True),
            "go import block": (["import", "("], True),
            "dynamic import": (["import", "(", "x", ")", ".", "then"], False),
            "solidity require": (["require", "(", "x", ">", "y", ")", ";"], False),
            "scala package object": (["package", "object", "a", "{"], False),
            "package table entry": (["package", "[", "x", "]", "=", "{"], False),
            "package clause": (["package", "main"], True),
            "python global": (["global", "counter"], False),
            "ordinary code": (["return", "x"], False),
        }
        for label, (words, expected) in cases.items():
            with self.subTest(label):
                line = [raw("Keyword", word, 1) for word in words]
                self.assertIs(is_import_line(line), expected)

    def test_swift_attribute_before_import_is_skipped(self) -> None:
        line = [
            raw("Punctuation", "@", 1),
            raw("Name", "testable", 1),
            raw("Keyword.Declaration", "import", 1),
            raw("Name.Class", "Foo", 1),
        ]
        self.assertTrue(is_import_line(line))

    def test_attribute_without_a_name_is_not_skipped(self) -> None:
        line = [raw("Punctuation", "@", 1), raw("Punctuation", "(", 1)]
        self.assertFalse(is_import_line(line))
        self.assertFalse(is_import_line([raw("Name", "pub", 1)]))

    def test_unclosed_modifier_group_is_not_an_import(self) -> None:
        line = [raw("Keyword", "pub", 1), raw("Punctuation", "(", 1)]
        self.assertFalse(is_import_line(line))

    def test_loader_call_with_one_string_is_an_import(self) -> None:
        php = [
            raw("Keyword", "include", 1),
            raw("Punctuation", "(", 1),
            raw("Literal.String.Single", "'a.php'", 1),
            raw("Punctuation", ");", 1),
        ]
        self.assertEqual(
            gate_duplication_tokens.import_span(php),
            gate_duplication_tokens.ImportSpan(4, 0),
        )
        dynamic = [raw("Keyword", "require", 1), raw("Punctuation", "(", 1)]
        self.assertFalse(is_import_line(dynamic))

    def test_code_after_an_import_on_the_same_line_is_kept(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [
                raw("Keyword", "import", 1),
                raw("Name.Other", "fs", 1),
                raw("Keyword", "from", 1),
                raw("Literal.String", "'fs'", 1),
                raw("Punctuation", ";", 1),
                raw("Keyword", "import", 1),
                raw("Name.Other", "os", 1),
                raw("Punctuation", ";", 1),
                raw("Keyword.Declaration", "function", 1),
                raw("Name.Other", "f", 1),
                raw("Punctuation", "{", 1),
            ]
        )
        self.assertEqual([token.text for token in result], ["function", "N", "{"])

    def test_import_list_ends_at_the_first_line_of_code(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [
                raw("Keyword", "use", 1),
                raw("Name", "std", 1),
                raw("Punctuation", "::", 1),
                raw("Punctuation", "{", 1),
                raw("Name", "io", 2),
                raw("Keyword", "as", 2),
                raw("Name", "x", 2),
                raw("Punctuation", ",", 2),
                raw("Keyword", "fn", 3),
                raw("Name.Function", "run", 3),
                raw("Operator", "=", 3),
            ]
        )
        self.assertEqual([token.text for token in result], ["fn", "N", "="])

    def test_identifiers_lexed_as_text_become_names(self) -> None:
        result = gate_duplication_tokens.normalize_tokens(
            [raw("Text", "counter", 1), raw("Text", "+", 1), raw("Other", "$x", 1)]
        )
        self.assertEqual([token.text for token in result], ["N", "+", "N"])

    def test_code_lines_and_data_lines(self) -> None:
        cases = {
            "member calls": ("N . N ( N . N ( ) ) ;", True),
            "branch": ("if N + 0 : return N - N", True),
            "keyword arguments call": ("N . N ( S , N = None , N = S )", True),
            "string list row": ("S , S ,", False),
            "tuple row": ("( S , 0 ) ,", False),
            "map row": ("S : N ,", False),
            "constant": ("N = S", False),
            "closing bracket": (")", False),
        }
        for label, (line, expected) in cases.items():
            with self.subTest(label):
                self.assertIs(
                    gate_duplication_tokens.is_code_line(line.split()), expected
                )

    def test_export_of_a_string_is_an_import(self) -> None:
        line = [raw("Keyword", "export", 1), raw("Literal.String", "'a.dart'", 1)]
        self.assertTrue(is_import_line(line))

    def test_import_word_as_a_literal_is_not_an_import(self) -> None:
        line = [raw("Literal.String", "import", 1), raw("Punctuation", ")", 1)]
        self.assertFalse(is_import_line(line))


class WindowHashTests(unittest.TestCase):
    def test_equal_windows_hash_equally(self) -> None:
        hashes = dict(gate_duplication.window_hashes([1, 2, 3, 1, 2, 3], 3))
        self.assertEqual(hashes[0], hashes[3])
        self.assertNotEqual(hashes[0], hashes[1])
        self.assertEqual(sorted(hashes), [0, 1, 2, 3])

    def test_short_streams_and_empty_windows_yield_nothing(self) -> None:
        self.assertEqual(list(gate_duplication.window_hashes([1, 2], 3)), [])
        self.assertEqual(list(gate_duplication.window_hashes([1, 2], 0)), [])


OPERATORS = ["+", "-", "*", "/", "%", "<", ">", "<=", ">=", "=="]
# Seven different statements, so no window of the stream repeats inside it.
LOGIC = " ".join(
    f"if N {OPERATORS[step]} 0 : return N {OPERATORS[(step + 3) % 10]} N"
    for step in range(7)
)


class DetectionTests(unittest.TestCase):
    def test_identical_streams_form_one_maximal_clone(self) -> None:
        sources = {"a": tokens(LOGIC), "b": tokens("x y " + LOGIC, first_line=3)}
        result = gate_duplication.detect_clones(sources, 20, 3)
        self.assertEqual(len(result), 1)
        found = result[0]
        self.assertEqual((found.a_path, found.b_path, found.tokens), ("a", "b", 63))
        self.assertEqual((found.a_start, found.a_end), (1, 7))

    def test_a_match_that_starts_mid_line_is_reported_from_the_next_whole_line(
        self,
    ) -> None:
        # "K = S" ends in the same normalized string as the lone "S" line, so
        # the token match starts inside line 1 of "a"; the clone starts at the
        # first line that is whole on both sides.
        prefix_a = [Token("K", 1), Token("=", 1), Token("S", 1)]
        sources = {
            "a": prefix_a + tokens(LOGIC, first_line=2, per_line=9),
            "b": [Token("S", 1)] + tokens(LOGIC, first_line=2, per_line=9),
        }
        found = gate_duplication.detect_clones(sources, 20, 3)[0]
        self.assertEqual(
            (found.a_start, found.a_end, found.b_start, found.b_end, found.tokens),
            (2, 8, 2, 8, 63),
        )

    def test_whole_lines_trims_partial_lines_on_both_sides(self) -> None:
        whole_lines = gate_duplication_tokens.whole_lines
        a_lines, b_lines = [1, 1, 2, 2, 3, 3], [5, 5, 6, 6, 7, 7]
        self.assertEqual(whole_lines((a_lines, 1), (b_lines, 1), 5), (1, 4))
        self.assertEqual(whole_lines((a_lines, 0), (b_lines, 0), 5), (0, 4))
        self.assertEqual(whole_lines((a_lines, 0), (b_lines, 0), 6), (0, 6))
        # No offset starts a line on both sides: the match is left as it is.
        self.assertEqual(whole_lines((a_lines, 1), (b_lines, 0), 4), (0, 4))

    def test_copies_of_a_longer_block_extend_backward(self) -> None:
        words = LOGIC.split()
        sources = {
            "a": tokens(" ".join(words[10:])),
            "b": tokens(LOGIC),
            "c": tokens(LOGIC),
        }
        result = gate_duplication.detect_clones(sources, 20, 3)
        between_copies = [found for found in result if found.a_path == "b"]
        self.assertEqual([found.tokens for found in between_copies], [len(words)])

    def test_short_line_span_is_not_a_clone(self) -> None:
        sources = {"a": tokens(LOGIC, per_line=100), "b": tokens(LOGIC, per_line=100)}
        self.assertEqual(gate_duplication.detect_clones(sources, 20, 2), [])

    def test_data_tables_are_not_clones(self) -> None:
        table = " ".join(["( S , N ( S ) ) ,"] * 12)
        sources = {"a": tokens(table), "b": tokens(table)}
        self.assertEqual(gate_duplication.detect_clones(sources, 20, 3), [])

    def test_code_that_also_holds_a_table_is_still_a_clone(self) -> None:
        table = " ".join(["( S , 0 ) ,"] * 30)
        stream = tokens(table, per_line=6) + tokens(LOGIC, first_line=31, per_line=9)
        result = gate_duplication.detect_clones({"a": stream, "b": stream}, 20, 3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].tokens, len(stream))

    def test_one_run_of_similar_statements_is_not_a_clone(self) -> None:
        sources = {"a": tokens(" ".join(["+"] * 60))}
        self.assertEqual(gate_duplication.detect_clones(sources, 20, 2), [])
        rows = " ".join(f"self . N{index % 3} = N" for index in range(24))
        self.assertEqual(
            gate_duplication.detect_clones({"a": tokens(rows, per_line=6)}, 20, 2), []
        )

    def test_adjacent_copies_in_one_file_are_one_clone(self) -> None:
        sources = {"a": tokens(LOGIC + " " + LOGIC, per_line=9)}
        result = gate_duplication.detect_clones(sources, 20, 3)
        self.assertEqual(
            [
                (found.a_start, found.a_end, found.b_start, found.b_end)
                for found in result
            ],
            [(1, 7, 8, 14)],
        )

    def test_three_copies_in_one_file_pair_the_first_with_each(self) -> None:
        sources = {"a": tokens(" ".join([LOGIC] * 3), per_line=9)}
        result = gate_duplication.detect_clones(sources, 20, 3)
        self.assertEqual(
            [(found.a_start, found.b_start, found.tokens) for found in result],
            [(1, 8, 63), (1, 15, 63)],
        )

    def test_shifted_matches_inside_one_copy_are_merged(self) -> None:
        header = "def N ( N , N , N ) : if N : return N"
        rows = " ".join(["self . N = N + 0"] * 6)
        block = tokens(header + " " + rows, per_line=8)
        result = gate_duplication.detect_clones({"a": block, "b": block}, 20, 3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].tokens, len(block))

    def test_short_repeating_pattern_is_not_a_clone(self) -> None:
        sources = {"a": tokens(" ".join(["+"] * 39))}
        self.assertEqual(gate_duplication.detect_clones(sources, 20, 2), [])

    def test_heavily_repeated_block_is_still_reported(self) -> None:
        block = "def N ( N ) : N = N + 0 if N else N - 0 return N * N"
        sources = {f"f{index:02}": tokens(block) for index in range(20)}
        result = gate_duplication.detect_clones(sources, 15, 2)
        self.assertEqual(
            [(found.a_path, found.b_path) for found in result],
            [(f"f{index:02}", f"f{index + 1:02}") for index in range(19)],
        )

    def test_alignments_over_the_same_lines_are_reported_once(self) -> None:
        sources = {
            "a": tokens(" ".join(["+"] * 60)),
            "b": tokens(" ".join(["+"] * 61), per_line=11),
        }
        result = gate_duplication.detect_clones(sources, 50, 2)
        self.assertEqual(
            [(found.a_path, found.b_path, found.tokens) for found in result],
            [("a", "b", 60)],
        )

    def test_long_periodic_file_is_fast_and_clean(self) -> None:
        rows = " ".join("[ 0 , N + 0 , N * 0 ] ," for _ in range(8000))
        started = time.monotonic()
        result = gate_duplication.detect_clones({"a": tokens(rows)}, 50, 5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result, [])

    def test_hash_collisions_are_not_clones(self) -> None:
        sources = {"a": tokens(LOGIC), "b": tokens(" ".join(reversed(LOGIC.split())))}
        with mock.patch.object(gate_duplication, "_HASH_MODULUS", 1):
            self.assertEqual(gate_duplication.detect_clones(sources, 20, 3), [])

    def test_two_hundred_files_finish_quickly(self) -> None:
        sources = {
            f"file{index:03}": tokens(
                " ".join(f"k{index}_{step} N = N + {step % 7}" for step in range(400))
            )
            for index in range(200)
        }
        started = time.monotonic()
        result = gate_duplication.detect_clones(sources, 50, 5)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result, [])


class PygmentsDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_renamed_variable_copy_is_found(self) -> None:
        write(self.root, "src/orders.py", ORIGINAL)
        write(self.root, "src/invoices.py", RENAMED_COPY)
        write(self.root, "src/greeting.py", UNRELATED)
        result = clones_in(self.root)
        self.assertEqual(len(result), 1)
        found = result[0]
        self.assertEqual(found.a_path, "src/invoices.py")
        self.assertEqual((found.a_start, found.a_end), (4, 12))
        self.assertEqual(found.b_path, "src/orders.py")
        self.assertEqual((found.b_start, found.b_end), (5, 13))

    def test_copy_with_blank_lines_and_comments_is_found(self) -> None:
        write(self.root, "web/shipping.js", JS_ORIGINAL)
        write(self.root, "web/parcels.js", JS_COMMENTED_COPY)
        result = clones_in(self.root)
        self.assertEqual(len(result), 1)
        found = result[0]
        self.assertEqual(found.a_path, "web/parcels.js")
        self.assertEqual((found.a_start, found.a_end), (2, 18))
        self.assertEqual((found.b_start, found.b_end), (1, 11))

    def test_identical_import_blocks_are_not_flagged(self) -> None:
        write(self.root, "one.py", PY_IMPORTS + "\nVALUE = 1\n")
        write(self.root, "two.py", PY_IMPORTS + "\ndef run():\n    return 2\n")
        write(self.root, "one.js", JS_IMPORTS + "\nexport const a = 1;\n")
        write(self.root, "two.js", JS_IMPORTS + "\nexport function b() {}\n")
        write(self.root, "one.go", GO_IMPORTS + "\nfunc one() {}\n")
        write(self.root, "two.go", GO_IMPORTS + "\nfunc two() int { return 2 }\n")
        self.assertEqual(clones_in(self.root, min_tokens=10, min_lines=2), [])

    def test_more_import_forms_are_not_flagged(self) -> None:
        write(self.root, "a/mod.rs", RUST_USES + "\nfn a() {}\n")
        write(self.root, "b/mod.rs", RUST_USES + "\nfn b() -> i32 { 2 }\n")
        write(self.root, "a/index.ts", TS_REEXPORTS + "\nconst a = 1;\n")
        write(self.root, "b/index.ts", TS_REEXPORTS + "\nlet b = 2;\n")
        write(self.root, "a/Global.cs", CS_USINGS)
        write(self.root, "b/Global.cs", CS_USINGS)
        self.assertEqual(clones_in(self.root, min_tokens=10, min_lines=2), [])

    def test_whole_module_copy_is_one_clone(self) -> None:
        module = (SCRIPTS / "gate_duplication.py").read_text(encoding="utf-8")
        write(self.root, "orig/dup.py", module)
        write(self.root, "copy/dup.py", module)
        result = clones_in(self.root)
        self.assertEqual(
            [(found.a_path, found.b_path) for found in result],
            [("copy/dup.py", "orig/dup.py")],
        )
        self.assertGreater(result[0].tokens, 1000)

    def test_call_heavy_copies_are_found(self) -> None:
        customer = JAVA_MAPPER.replace("User", "Customer").replace("user", "client")
        write(self.root, "UserMapper.java", JAVA_MAPPER)
        write(self.root, "CustomerMapper.java", customer)
        write(self.root, "report.py", PY_CALLS)
        write(self.root, "invoice.py", PY_CALLS.replace("report", "invoice"))
        result = clones_in(self.root)
        self.assertEqual(
            [(found.a_path, found.b_path) for found in result],
            [("CustomerMapper.java", "UserMapper.java"), ("invoice.py", "report.py")],
        )

    def test_long_runs_of_similar_statements_in_one_file_are_not_clones(self) -> None:
        fields = [f"field_{index}" for index in range(24)]
        many_fields = (
            "class Settings:\n"
            f"    def __init__(self, {', '.join(fields)}):\n"
            + "".join(f"        self.{field} = {field}\n" for field in fields)
        )
        write(self.root, "settings.py", many_fields)
        write(self.root, "cli.py", ARGPARSE)
        write(self.root, "sym.go", GO_SWITCH)
        self.assertEqual(clones_in(self.root), [])

    def test_shared_constructor_is_one_clone(self) -> None:
        write(self.root, "a.py", CONSTRUCTOR)
        write(self.root, "b.py", "\n" + CONSTRUCTOR.replace("Settings", "Options"))
        result = clones_in(self.root)
        self.assertEqual(
            [
                (found.a_start, found.a_end, found.b_start, found.b_end)
                for found in result
            ],
            [(1, 14, 2, 15)],
        )

    def test_code_on_an_import_line_is_compared(self) -> None:
        write(self.root, "a.js", "import fs from 'fs';\n\nexport " + JS_ORIGINAL[7:])
        write(self.root, "b.js", "import fs from 'fs'; export " + JS_ORIGINAL[7:])
        result = clones_in(self.root)
        self.assertEqual([(found.a_start, found.b_start) for found in result], [(3, 1)])

    def test_renamed_solidity_copy_is_found(self) -> None:
        write(self.root, "Ledger.sol", SOLIDITY)
        renamed = SOLIDITY.replace("counter", "balance").replace("total", "amount")
        write(self.root, "Vault.sol", renamed.replace("Ledger", "Vault"))
        self.assertEqual(len(clones_in(self.root, min_tokens=30)), 1)

    def test_repository_files_cannot_replace_pygments_or_the_stdlib(self) -> None:
        write(self.root, "json.py", "raise ImportError('planted json')\n")
        write(self.root, "token.py", "raise ImportError('planted token')\n")
        write(self.root, "pygments/__init__.py", "raise ImportError('planted')\n")
        write(self.root, "web/shipping.js", JS_ORIGINAL)
        write(self.root, "web/parcels.js", JS_COMMENTED_COPY)
        files = [self.root / "web/shipping.js", self.root / "web/parcels.js"]
        result = gate_duplication.find_clones(ToolPython(), self.root, files, 50, 5)
        self.assertEqual(len(result), 1)

    def test_top_level_statement_runs_are_shapes_not_copies(self) -> None:
        constants = "".join(
            f"PATTERN_{name} = re.compile(r'^\\s*\\[{name}\\]', re.MULTILINE)\n"
            for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
        )
        other = constants.replace("PATTERN_", "RULE_").replace("alpha", "one")
        files = [
            write(self.root, "a.py", "import re\n\n" + constants),
            write(self.root, "b.py", "import re\n\n" + other),
        ]
        outcome = gate_duplication.run_duplication(
            ToolPython(), self.root, files, diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, PASS)

    def test_the_same_run_inside_a_function_is_still_a_copy(self) -> None:
        body = "".join(
            f"    total += values[{index}] * weights[{index}] - offsets[{index}]\n"
            for index in range(8)
        )
        files = [
            write(
                self.root,
                "a.py",
                "def score(values, weights, offsets):\n"
                "    total = 0\n" + body + "    return total\n",
            ),
            write(
                self.root,
                "b.py",
                "def rank(items, scale, shift):\n"
                "    total = 0\n"
                + body.replace("values", "items")
                + "    return total\n",
            ),
        ]
        outcome = gate_duplication.run_duplication(
            ToolPython(), self.root, files, diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, FAIL)

    def test_indented_lines_and_body_check(self) -> None:
        indented = gate_duplication_tokens.indented_lines("a = 1\n  b\n\n\tc\n")
        self.assertEqual(indented, frozenset({2, 4}))
        self.assertTrue(gate_duplication_tokens.has_body_line(indented, 1, 2))
        self.assertFalse(gate_duplication_tokens.has_body_line(indented, 1, 1))

    def test_relative_paths_are_resolved(self) -> None:
        write(self.root, "web/shipping.js", JS_ORIGINAL)
        write(self.root, "web/parcels.js", JS_COMMENTED_COPY)
        root = Path(os.path.relpath(self.root))
        files = [root / "web/shipping.js", root / "web/parcels.js"]
        outcome = gate_duplication.run_duplication(
            ToolPython(), root, files, diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(outcome.items[0].path, "web/parcels.js")

    def test_unreadable_file_blocks_the_gate(self) -> None:
        write(self.root, "a.py", ORIGINAL)
        files = [self.root / "a.py", self.root / "missing.py"]
        outcome = gate_duplication.run_duplication(
            ToolPython(), self.root, files, diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, BLOCKED)
        self.assertIn("missing.py", outcome.summary)
        self.assertIn("could not be read", outcome.summary)

    def test_no_tokenized_file_blocks_the_gate(self) -> None:
        files = [write(self.root, "notes.unknownext", "plain words\n")]
        outcome = gate_duplication.run_duplication(
            ToolPython(), self.root, files, diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, BLOCKED)
        self.assertIn("no Pygments lexer", outcome.summary)

    def test_files_without_a_lexer_or_unreadable_are_skipped(self) -> None:
        notes = write(self.root, "notes.unknownext", "plain words\n")
        missing = self.root / "missing.py"
        result = gate_duplication_tokens.tokenize_sources(
            ToolPython(), self.root, [notes, missing]
        )
        self.assertEqual(result.tokens, {})
        reasons = dict(result.skipped)
        self.assertEqual(reasons["notes.unknownext"], "no Pygments lexer")
        self.assertTrue(reasons["missing.py"].startswith("unreadable"))

    def test_no_files_needs_no_helper(self) -> None:
        broken = ToolPython(executable=str(self.root / "missing-python"))
        result = gate_duplication_tokens.tokenize_sources(broken, self.root, [])
        self.assertEqual((result.tokens, result.skipped), ({}, []))


class IncrementalScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        write(self.root, "src/orders.py", ORIGINAL)
        write(self.root, "src/legacy.py", RENAMED_COPY)
        write(self.root, "src/shipping.js", JS_ORIGINAL)
        commit_all(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def files(self) -> list[Path]:
        return sorted((self.root / "src").iterdir())

    def outcome(self, changes: diff_scope.ChangeSet) -> GateOutcome:
        return gate_duplication.run_duplication(
            ToolPython(), self.root, self.files(), changes, {"duplication": {}}
        )

    def test_new_copy_is_reported_and_old_clone_is_not(self) -> None:
        write(self.root, "src/parcels.js", JS_COMMENTED_COPY)
        with (self.root / "src/orders.py").open("a", encoding="utf-8") as handle:
            handle.write("\n\nLIMIT = 3\n")
        outcome = self.outcome(diff_scope.local_changes(self.root))
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(len(outcome.items), 1)
        item = outcome.items[0]
        self.assertEqual((item.path, item.line), ("src/parcels.js", 2))
        self.assertEqual(item.name, "repeats src/shipping.js:1-11")
        self.assertEqual(outcome.measurements["clones"], 1)
        self.assertEqual(outcome.measurements["clones_total"], 2)
        self.assertIn("touching the change", outcome.summary)

    def test_change_away_from_the_old_clone_passes(self) -> None:
        with (self.root / "src/orders.py").open("a", encoding="utf-8") as handle:
            handle.write("\n\nLIMIT = 3\n")
        outcome = self.outcome(diff_scope.local_changes(self.root))
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.summary, "The change adds no duplicated blocks.")

    def test_repository_scope_reports_every_clone(self) -> None:
        write(self.root, "src/parcels.js", JS_COMMENTED_COPY)
        outcome = self.outcome(diff_scope.repository_changes())
        self.assertEqual(outcome.status, FAIL)
        self.assertEqual(
            sorted(item.path for item in outcome.items),
            ["src/legacy.py", "src/parcels.js"],
        )
        self.assertIn("in the repository (limit 0)", outcome.summary)


class EvaluationTests(unittest.TestCase):
    def test_clone_is_named_from_the_touched_side(self) -> None:
        changes = local_change(**{"b.py": frozenset({22})})
        outcome = gate_duplication.evaluate_duplication([clone()], changes, {})
        item = outcome.items[0]
        self.assertEqual((item.path, item.line), ("b.py", 21))
        self.assertEqual(item.name, "repeats a.py:1-9")
        pair = outcome.measurements["pairs"][0]
        self.assertEqual(pair["first"]["path"], "b.py")
        self.assertEqual(pair["second"]["start_line"], 1)
        self.assertEqual(pair["line_count"], 9)

    def test_clone_touching_only_unchanged_lines_is_ignored(self) -> None:
        changes = local_change(**{"a.py": frozenset({50})})
        outcome = gate_duplication.evaluate_duplication([clone()], changes, {})
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(outcome.items, [])

    def test_repository_scope_without_clones_passes(self) -> None:
        outcome = gate_duplication.evaluate_duplication(
            [], diff_scope.repository_changes(), {"min_tokens": 40, "min_lines": 4}
        )
        self.assertEqual(outcome.status, PASS)
        self.assertEqual(
            outcome.summary, "No duplicated blocks of 40+ tokens across 4+ lines."
        )

    def test_allowed_clones_pass_with_details(self) -> None:
        outcome = gate_duplication.evaluate_duplication(
            [clone()],
            diff_scope.repository_changes(),
            {"duplication": {"max_new_clones": 1}},
        )
        self.assertEqual(outcome.status, PASS)
        self.assertIn("within the limit of 1", outcome.summary)
        self.assertTrue(outcome.details[0].endswith("(allowed by max_new_clones)"))

    def test_items_are_capped(self) -> None:
        clones = [
            clone(start=index * 100 + 1, end=index * 100 + 9) for index in range(55)
        ]
        outcome = gate_duplication.evaluate_duplication(
            clones, diff_scope.repository_changes(), {}
        )
        self.assertEqual(len(outcome.items), gate_duplication.ITEM_LIMIT)
        self.assertIn("... and 5 more clones.", outcome.details)
        self.assertEqual(outcome.measurements["clones"], 55)
        self.assertEqual(outcome.measurements["duplicated_lines"], 55 * 9)

    def test_skipped_files_are_listed(self) -> None:
        outcome = gate_duplication.evaluate_duplication(
            [],
            diff_scope.repository_changes(),
            {},
            skipped=[("notes.xyz", "no Pygments lexer")],
        )
        self.assertIn("Not analyzed (no Pygments lexer): notes.xyz", outcome.details)

    def test_item_describes_the_duplicate(self) -> None:
        outcome = gate_duplication.evaluate_duplication(
            [clone()], diff_scope.repository_changes(), {}
        )
        self.assertEqual(
            outcome.details[0],
            "a.py:1 repeats b.py:21-29: duplicate block 60 tokens over 9 lines "
            "(limit under 50 tokens or 5 lines)",
        )


class ToolFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source = write(self.root, "a.py", ORIGINAL)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def script(self, body: str) -> ToolPython:
        path = self.root / "fake-python"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return ToolPython(executable=str(path))

    def assert_tool_error(self, tool: ToolPython, message: str, **kwargs: int) -> None:
        with self.assertRaises(gate_duplication.DuplicationToolError) as caught:
            gate_duplication_tokens.tokenize_sources(
                tool, self.root, [self.source], **kwargs
            )
        self.assertIn(message, str(caught.exception))

    def test_missing_interpreter(self) -> None:
        tool = ToolPython(executable=str(self.root / "missing-python"))
        self.assert_tool_error(tool, "could not start")

    def test_failing_helper(self) -> None:
        tool = self.script("echo 'No module named pygments' >&2\nexit 1\n")
        self.assert_tool_error(tool, "No module named pygments")

    def test_unreadable_output(self) -> None:
        self.assert_tool_error(self.script("echo not-json\n"), "unreadable output")

    def test_missing_keys_in_output(self) -> None:
        tool = self.script("echo '{\"files\": {}}'\n")
        self.assert_tool_error(tool, "unreadable output")

    def test_timeout(self) -> None:
        tool = self.script("sleep 5\n")
        self.assert_tool_error(tool, "timed out", timeout_seconds=1)

    def test_tool_environment_is_passed(self) -> None:
        tool = self.script(
            'echo "{\\"files\\": {}, \\"skipped\\": {\\"$MARK\\": \\"x\\"}}"\n'
        )
        tool = ToolPython(tool.executable, {"MARK": str(self.root / "b.py")})
        result = gate_duplication_tokens.tokenize_sources(
            tool, self.root, [self.source]
        )
        self.assertEqual(result.skipped, [("b.py", "x")])

    def test_run_duplication_is_blocked_when_pygments_fails(self) -> None:
        tool = self.script("exit 3\n")
        outcome = gate_duplication.run_duplication(
            tool, self.root, [self.source], diff_scope.repository_changes(), {}
        )
        self.assertEqual(outcome.status, BLOCKED)
        self.assertIn("exit 3", outcome.summary)
        self.assertIn("Duplication could not be measured", outcome.summary)
        self.assertFalse(outcome.passed)


if __name__ == "__main__":
    unittest.main()
