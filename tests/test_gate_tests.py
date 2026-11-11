import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-discipline" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_tests  # noqa: E402
import gate_tests_command  # noqa: E402
import gate_tests_parsers  # noqa: E402
import gate_tests_parsers_other  # noqa: E402
import gate_types  # noqa: E402
from gate_types import CommandRun  # noqa: E402

# Recorded runner output, trimmed to the lines around each summary.
PYTEST_FAILED = """\
============================= test session starts ==============================
collected 9 items / 2 deselected / 7 selected

tests/test_app.py ..F.s.x                                                [100%]

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
E       assert 3 == 4
=========================== short test summary info ============================
FAILED tests/test_app.py::test_add - assert 3 == 4
== 1 failed, 4 passed, 1 skipped, 2 deselected, 1 xfailed, 1 error in 0.12s ===
"""

UNITTEST_MIXED = """\
test_a (tests.test_a.A.test_a) ... ok
----------------------------------------------------------------------
Ran 10 tests in 0.020s

FAILED (failures=1, errors=1, skipped=2, expected failures=1, unexpected successes=1)
"""

VITEST_COLORED = (
    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m3 passed\x1b[39m\x1b[22m"
    "\x1b[90m (3)\x1b[39m\n"
    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m12 passed\x1b[39m\x1b[22m"
    "\x1b[90m (12)\x1b[39m\n"
    "\x1b[2m   Start at \x1b[22m 10:00:00\n"
)

VITEST_WORKSPACES = """\
> @app/server@1.0.0 test
 Test Files  2 passed (2)
      Tests  5 passed | 1 todo (6)
> @app/web@1.0.0 test
 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 3 passed | 2 skipped (6)
"""

JEST_WORKSPACES = """\
Test Suites: 2 passed, 2 total
Tests:       1 skipped, 2 todo, 4 passed, 7 total
Snapshots:   0 total
Test Suites: 1 failed, 1 total
Tests:       1 failed, 2 passed, 3 total
"""

CARGO_WORKSPACE = """\
     Running unittests src/lib.rs (target/debug/deps/core-1)
test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 2 filtered out; \
finished in 0.00s
     Running unittests src/main.rs (target/debug/deps/cli-2)
test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; \
finished in 0.01s
   Doc-tests core
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; \
finished in 0.10s
"""

MOCHA_OUTPUT = """\
  Cart
    ✓ adds items
    - removes items

  12 passing (30ms)
  2 pending
  1 failing

  1) Cart
       totals:
     AssertionError: expected 3 to equal 4
"""

RSPEC_OUTPUT = """\
Finished in 0.0123 seconds (files took 0.1 seconds to load)
12 examples, 1 failure, 2 pending, 1 error occurred outside of examples
"""

PHPUNIT_FAILED = """\
PHPUnit 10.5.0 by Sebastian Bergmann and contributors.

..F.S.I                                                             7 / 7 (100%)

FAILURES!
Tests: 7, Assertions: 9, Failures: 1, Errors: 1, Skipped: 1, Incomplete: 1.
"""

MAVEN_MULTI_MODULE = """\
[INFO] Running com.example.core.CartTest
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.05 s \
- in com.example.core.CartTest
[INFO] Running com.example.core.PriceTest
[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 1, Time elapsed: 0.01 s \
<<< FAILURE! - in com.example.core.PriceTest
[INFO] Results:
[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 1
[INFO] Running com.example.web.ApiTest
[INFO] Tests run: 4, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.2 s \
- in com.example.web.ApiTest
[INFO] Results:
[INFO] Tests run: 4, Failures: 0, Errors: 1, Skipped: 0
"""

DOTNET_OUTPUT = """\
Failed!  - Failed:     1, Passed:     4, Skipped:     1, Total:     6, \
Duration: 50 ms - App.Tests.dll (net8.0)
Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3, \
Duration: 20 ms - Lib.Tests.dll (net8.0)
"""

XCTEST_OUTPUT = """\
Test Suite 'CartTests' passed at 2026-01-01 10:00:00.000.
\t Executed 3 tests, with 0 failures (0 unexpected) in 0.001 (0.002) seconds
Test Suite 'AppPackageTests.xctest' failed at 2026-01-01 10:00:00.000.
\t Executed 5 tests, with 1 test skipped and 1 failure (0 unexpected) in 0.01 \
(0.02) seconds
Test Suite 'All tests' failed at 2026-01-01 10:00:00.000.
\t Executed 5 tests, with 1 test skipped and 1 failure (0 unexpected) in 0.01 \
(0.02) seconds
✘ Test run with 4 tests in 2 suites failed after 0.003 seconds with 2 issues.
"""

DART_OUTPUT = """\
00:01 +3: test/cart_test.dart: adds items
00:02 +5 ~1 -2: Some tests failed.
"""

EXUNIT_OUTPUT = """\
Finished in 0.05 seconds (0.03s async, 0.02s sync)
1 doctest, 10 tests, 1 failure, 1 skipped, 3 excluded
"""

MINITEST_OUTPUT = """\
Finished in 0.001s, 1000.0 runs/s, 2000.0 assertions/s.
6 runs, 12 assertions, 1 failures, 1 errors, 1 skips
"""

NODE_TEST_SPEC = """\
✔ adds (0.5ms)
ℹ tests 6
ℹ suites 1
ℹ pass 3
ℹ fail 1
ℹ cancelled 1
ℹ skipped 0
ℹ todo 1
ℹ duration_ms 40.1
"""

TAP_LINES = """\
TAP version 13
1..4
ok 1 - adds
not ok 2 - removes
ok 3 - totals # SKIP not ready
not ok 4 - pays # TODO later
    ok 1 - nested subtest is not counted
"""

GO_PLAIN = """\
ok  \texample.com/app/cart\t0.012s
FAIL\texample.com/app/pay\t0.020s
ok  \texample.com/app/util\t(cached)
?   \texample.com/app/cmd\t[no test files]
"""

GO_JSON = (
    "noise\n"
    "[]\n"
    '{"Action":"run","Package":"example","Test":"TestOne"}\n'
    '{"Action":"pass","Package":"example","Test":"TestOne"}\n'
    '{"Action":"pass","Package":"example","Test":"TestOne"}\n'
    '{"Action":"fail","Package":"example","Test":"TestTwo"}\n'
    '{"Action":"skip","Package":"example","Test":"TestThree"}\n'
    '{"Action":"pass","Package":"example"}\n'
)

JUNIT_GRADLE = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.CartTest" tests="4" skipped="1" failures="1"
    errors="0" timestamp="2026-01-01T10:00:00" time="0.1">
  <testcase name="adds" classname="com.example.CartTest" time="0.01"/>
</testsuite>
"""

JUNIT_NESTED = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="99" failures="99">
  <testsuite name="outer" tests="99">
    <testsuite name="inner-a" tests="3" failures="0" errors="1" skipped="0"/>
    <testsuite name="inner-b" tests="2.0" failures="" disabled="1"/>
  </testsuite>
</testsuites>
"""


class ParseTestExecutionTests(unittest.TestCase):
    def assert_counts(self, output, **expected):
        execution = gate_tests.parse_test_execution(output)
        self.assertTrue(execution.measured, execution)
        actual = {name: getattr(execution, name) for name in expected}
        self.assertEqual(actual, expected)

    def test_unittest_summary(self):
        self.assert_counts(
            "Ran 17 tests in 0.123s\n\nOK (skipped=2)\n",
            executed=17,
            passed=15,
            failed=0,
            skipped=2,
            parser="unittest",
        )

    def test_unittest_expected_failures_are_not_failures(self):
        self.assert_counts(
            UNITTEST_MIXED,
            executed=10,
            passed=4,
            failed=3,
            skipped=3,
            xfailed=1,
            parser="unittest",
        )

    def test_unittest_sums_every_run(self):
        output = "Ran 2 tests in 0.1s\n\nOK\nRan 3 tests in 0.1s\n\nOK (skipped=1)\n"
        self.assert_counts(output, executed=5, passed=4, skipped=1)

    def test_unittest_block_without_a_readable_status_is_unmeasured(self):
        for output in (
            "Ran 2 tests in 0.1s\n\nOK\nRan 3 tests in 0.1s\n",
            "Ran 3 tests in 0.1s\n\nOK (skipped=1, mystery=2)\n",
        ):
            with self.subTest(output=output):
                execution = gate_tests.parse_test_execution(output)
                self.assertFalse(execution.measured)

    def test_unittest_no_tests_ran(self):
        self.assert_counts("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n", executed=0)

    def test_twisted_trial_status_keys(self):
        self.assert_counts(
            "Ran 5 tests in 0.010s\n\nPASSED (skips=2, successes=3)\n",
            executed=5,
            passed=3,
            skipped=2,
            parser="unittest",
        )
        self.assert_counts(
            "Ran 6 tests in 0.010s\n\nFAILED (failures=1, errors=1, "
            "expectedFailures=1, unexpectedSuccesses=1, successes=2)\n",
            executed=6,
            failed=3,
            skipped=1,
            xfailed=1,
        )

    def test_pytest_summary(self):
        self.assert_counts(
            "================ 12 passed, 2 skipped, 1 xfailed in 1.2s ================",
            executed=15,
            passed=12,
            skipped=3,
            xfailed=1,
            parser="pytest",
        )

    def test_pytest_counts_errors_as_failures_and_deselected_separately(self):
        self.assert_counts(
            PYTEST_FAILED,
            executed=8,
            passed=4,
            failed=2,
            skipped=2,
            deselected=2,
            parser="pytest",
        )

    def test_pytest_quiet_line_and_reruns(self):
        self.assert_counts(
            "3 passed, 1 xpassed, 2 rerun in 0.50s\n",
            executed=4,
            passed=3,
            retried=2,
            parser="pytest",
        )

    def test_pytest_9_subtests_are_counted_by_outcome(self):
        # Recorded from pytest 9.2.0: a TestCase using self.subTest plus a
        # test using the ``subtests`` fixture.
        self.assert_counts(
            ".uu.                                                [100%]\n"
            "2 passed, 5 subtests passed in 0.00s\n",
            executed=2,
            passed=2,
            failed=0,
            skipped=0,
            parser="pytest",
        )
        self.assert_counts(
            "2 passed, 1 skipped, 5 subtests passed in 0.01s", executed=3, skipped=1
        )
        self.assert_counts(
            "== 1 failed, 2 passed, 1 subtests failed, 4 subtests passed in 0.01s ==",
            executed=3,
            failed=2,
        )
        self.assert_counts(
            "3 passed, 1 subtests skipped, 1 subtests xfailed in 0.01s",
            executed=3,
            skipped=2,
        )

    def test_pytest_before_6_printed_seconds(self):
        self.assert_counts(
            "===== 3 passed in 0.01 seconds =====", executed=3, parser="pytest"
        )

    def test_pytest_sums_every_session(self):
        output = (
            "3 passed, 2 skipped, 4 deselected in 0.10s\n"
            "============ 5 passed in 0.20s ============\n"
        )
        self.assert_counts(output, executed=10, passed=8, skipped=2, deselected=4)

    def test_pytest_no_tests_ran(self):
        self.assert_counts(
            "============================ no tests ran in 0.01s ============",
            executed=0,
            parser="pytest",
        )

    def test_vitest_summary(self):
        self.assert_counts(
            "Tests  2 failed | 10 passed | 1 skipped (13)",
            executed=13,
            passed=10,
            failed=2,
            skipped=1,
            parser="vitest",
        )

    def test_vitest_all_pass_line_with_colors(self):
        self.assert_counts(
            VITEST_COLORED, executed=12, passed=12, failed=0, parser="vitest"
        )

    def test_vitest_sums_workspaces_and_counts_todo_as_skipped(self):
        self.assert_counts(
            VITEST_WORKSPACES,
            executed=12,
            passed=8,
            failed=1,
            skipped=3,
            parser="vitest",
        )

    def test_jest_summary(self):
        self.assert_counts(
            "Tests:       1 skipped, 4 passed, 5 total",
            executed=5,
            passed=4,
            skipped=1,
            parser="jest",
        )

    def test_jest_sums_workspaces_and_counts_todo_as_skipped(self):
        self.assert_counts(
            JEST_WORKSPACES,
            executed=10,
            passed=6,
            failed=1,
            skipped=3,
            parser="jest",
        )

    def test_jest_without_passed_count_derives_it(self):
        self.assert_counts(
            "Tests:       2 failed, 2 total\n", executed=2, passed=0, failed=2
        )

    def test_cargo_sums_test_binaries(self):
        self.assert_counts(
            "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured\n"
            "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured\n",
            executed=6,
            passed=5,
            skipped=1,
            parser="cargo",
        )

    def test_cargo_counts_filtered_out_as_deselected(self):
        self.assert_counts(
            CARGO_WORKSPACE,
            executed=8,
            passed=6,
            failed=1,
            skipped=1,
            deselected=2,
            parser="cargo",
        )

    def test_go_json_counts_terminal_test_events_once(self):
        self.assert_counts(
            GO_JSON, executed=3, passed=1, failed=1, skipped=1, parser="go-json"
        )

    def test_mocha(self):
        self.assert_counts(
            MOCHA_OUTPUT,
            executed=15,
            passed=12,
            failed=1,
            skipped=2,
            parser="mocha",
        )

    def test_rspec_counts_errors_outside_examples(self):
        self.assert_counts(
            RSPEC_OUTPUT,
            executed=12,
            passed=8,
            failed=2,
            skipped=2,
            parser="rspec",
        )

    def test_rspec_minimal_line(self):
        self.assert_counts("3 examples, 0 failures\n", executed=3, passed=3)

    def test_phpunit_ok_and_failure_summaries(self):
        self.assert_counts(
            "OK (5 tests, 10 assertions)\n", executed=5, passed=5, parser="phpunit"
        )
        self.assert_counts(
            PHPUNIT_FAILED,
            executed=7,
            passed=3,
            failed=2,
            skipped=2,
            parser="phpunit",
        )

    def test_maven_sums_module_aggregates_not_per_class_lines(self):
        self.assert_counts(
            MAVEN_MULTI_MODULE,
            executed=9,
            passed=6,
            failed=2,
            skipped=1,
            parser="maven",
        )

    def test_maven_falls_back_to_per_class_lines(self):
        output = (
            "Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1 s\n"
            "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 1 s\n"
        )
        self.assert_counts(output, executed=3, failed=1, parser="maven")

    def test_dotnet_sums_test_projects(self):
        self.assert_counts(
            DOTNET_OUTPUT,
            executed=9,
            passed=7,
            failed=1,
            skipped=1,
            parser="dotnet",
        )

    def test_swift_uses_outermost_xctest_suite_and_adds_swift_testing(self):
        self.assert_counts(
            XCTEST_OUTPUT,
            executed=9,
            passed=5,
            failed=3,
            skipped=1,
            parser="swift",
        )

    def test_swift_testing_alone(self):
        passed = "✔ Test run with 3 tests passed after 0.002 seconds.\n"
        self.assert_counts(passed, executed=3, passed=3, failed=0, parser="swift")
        failed = "✘ Test run with 3 tests failed after 0.002 seconds.\n"
        self.assert_counts(failed, executed=3, passed=2, failed=1)

    def test_xctest_alone(self):
        output = "Executed 4 tests, with 0 failures (0 unexpected) in 0.1 (0.1) seconds"
        self.assert_counts(output, executed=4, passed=4, parser="swift")

    def test_dart_uses_the_final_progress_line(self):
        self.assert_counts(
            DART_OUTPUT,
            executed=8,
            passed=5,
            failed=2,
            skipped=1,
            parser="dart",
        )
        self.assert_counts("00:01 +3: All tests passed!\n", executed=3, passed=3)

    def test_exunit_moves_excluded_tests_to_deselected(self):
        self.assert_counts(
            EXUNIT_OUTPUT,
            executed=8,
            passed=6,
            failed=1,
            skipped=1,
            deselected=3,
            parser="exunit",
        )

    def test_minitest(self):
        self.assert_counts(
            MINITEST_OUTPUT,
            executed=6,
            passed=3,
            failed=2,
            skipped=1,
            parser="minitest",
        )

    def test_minitest_sums_every_test_task(self):
        output = (
            "5 runs, 5 assertions, 0 failures, 0 errors, 0 skips\n"
            "4 runs, 4 assertions, 0 failures, 0 errors, 3 skips\n"
        )
        self.assert_counts(output, executed=9, skipped=3, parser="minitest")

    def test_minitest_reporters_say_tests(self):
        self.assert_counts(
            "6 tests, 9 assertions, 2 failures, 0 errors, 1 skips\n",
            executed=6,
            failed=2,
            skipped=1,
            parser="minitest",
        )

    def test_test_unit_pendings_and_omissions_are_skips(self):
        # Recorded from `ruby tu.rb` with test-unit: one pass, omit(), pend().
        output = (
            "Finished in 0.000712 seconds.\n"
            "-------------------------------------------------------------\n"
            "3 tests, 1 assertions, 0 failures, 0 errors, 1 pendings, "
            "1 omissions, 0 notifications\n"
            "50% passed\n"
        )
        self.assert_counts(
            output, executed=3, passed=1, failed=0, skipped=2, parser="test-unit"
        )

    def test_exunit_rejects_lines_with_counts_it_never_prints(self):
        line = "3 tests, 1 assertions, 0 failures, 1 omissions\n"
        self.assertIsNone(gate_tests_parsers_other.parse_exunit(line))

    def test_counts_from_several_runners_add_up(self):
        cases = [
            (
                " Test Files  1 passed (1)\n      Tests  4 passed (4)\n"
                "Tests:       3 skipped, 2 passed, 5 total\n",
                "vitest+jest",
                9,
                3,
            ),
            (
                "Ran 4 tests in 0.01s\n\nOK\n3 passed, 7 skipped in 0.12s\n",
                "unittest+pytest",
                14,
                7,
            ),
            (
                "Tests:       4 passed, 4 total\n# tests 6\n# pass 2\n# skipped 4\n",
                "jest+tap",
                10,
                4,
            ),
        ]
        for output, parser, executed, skipped in cases:
            with self.subTest(parser=parser):
                self.assert_counts(
                    output, parser=parser, executed=executed, skipped=skipped
                )

    def test_unmeasured_runner_does_not_hide_a_measured_one(self):
        self.assert_counts(GO_PLAIN + "Tests:       2 passed, 2 total\n", parser="jest")

    def test_one_runner_family_is_read_once(self):
        output = (
            "1..2\nok 1 - a\nok 2 - b # SKIP later\n# tests 2\n# pass 1\n# skipped 1\n"
        )
        self.assert_counts(output, parser="tap", executed=2, skipped=1)

    def test_node_test_spec_reporter_footer(self):
        self.assert_counts(
            NODE_TEST_SPEC,
            executed=6,
            passed=3,
            failed=2,
            skipped=1,
            parser="tap",
        )

    def test_tap_footer_without_pass_count(self):
        output = "# tests 4\n# fail 1\n# skipped 1\n"
        self.assert_counts(output, executed=4, passed=2, failed=1, skipped=1)

    def test_plain_tap_lines_with_directives(self):
        self.assert_counts(
            TAP_LINES,
            executed=4,
            passed=1,
            failed=1,
            skipped=2,
            parser="tap",
        )

    def test_tap_plan_without_results_is_unknown(self):
        execution = gate_tests.parse_test_execution("1..0\n")
        self.assertFalse(execution.measured)

    def test_plain_go_test_counts_packages_but_stays_unmeasured(self):
        execution = gate_tests.parse_test_execution(GO_PLAIN)
        self.assertFalse(execution.measured)
        self.assertEqual(execution.parser, "go-packages")
        self.assertEqual((execution.executed, execution.failed), (3, 1))

    def test_unknown_output_is_explicitly_unmeasured(self):
        execution = gate_tests.parse_test_execution("everything seems okay")
        self.assertFalse(execution.measured)
        self.assertEqual(execution.parser, "unknown")

    def test_carriage_returns_become_line_breaks(self):
        cleaned = gate_tests_parsers.clean_output("a\r\nb\rc\x1b]0;title\x07d")
        self.assertEqual(cleaned, "a\nb\ncd")

    def test_summary_counts_normalizes_error(self):
        counts = gate_tests_parsers.summary_counts("1 error, 2 Passed")
        self.assertEqual(counts, {"errors": 1, "passed": 2})


class RealRunnerOutputTests(unittest.TestCase):
    """Run the real unittest runner so the parser is checked against its
    actual output, not only recorded samples."""

    SUITE = """\
import unittest


class Sample(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_skipped(self):
        raise unittest.SkipTest("not ready")

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(1, 2)

    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass
"""

    def test_unittest_runner_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "test_sample.py").write_text(self.SUITE, encoding="utf-8")
            command = [sys.executable, "-m", "unittest", "discover", "-s", "."]
            run = gate_types.subprocess_runner(root)(command, 60)
        execution = gate_tests.parse_test_execution(run.output)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(
            (execution.executed, execution.passed, execution.failed),
            (5, 1, 2),
        )
        self.assertEqual((execution.skipped, execution.xfailed), (2, 1))


class JunitReportTests(unittest.TestCase):
    def write(self, root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_sums_leaf_suites_and_ignores_unreadable_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.write(root, "a.xml", JUNIT_GRADLE),
                self.write(root, "b.xml", JUNIT_NESTED),
                self.write(root, "broken.xml", "<testsuite"),
                self.write(root, "other.xml", "<coverage/>"),
                root / "missing.xml",
            ]
            execution = gate_tests.read_junit_reports(paths)
        self.assertIsNotNone(execution)
        self.assertEqual(
            (execution.executed, execution.failed, execution.skipped),
            (9, 2, 2),
        )
        self.assertEqual(execution.parser, "junit")

    def test_testsuites_root_with_totals_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write(
                Path(temporary), "r.xml", '<testsuites tests="3" failures="1"/>'
            )
            execution = gate_tests.read_junit_reports([path])
        self.assertEqual((execution.executed, execution.failed), (3, 1))

    def test_non_numeric_attributes_count_as_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write(
                Path(temporary), "r.xml", '<testsuite tests="2" failures="n/a"/>'
            )
            execution = gate_tests.read_junit_reports([path])
        self.assertEqual((execution.executed, execution.failed), (2, 0))

    def test_no_suites_is_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write(Path(temporary), "r.xml", "<testsuites/>")
            self.assertIsNone(gate_tests.read_junit_reports([path]))

    def test_fresh_reports_skip_old_files_and_dependency_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh = self.write(root, "build/test-results/test/TEST-a.xml", "x")
            module = self.write(root, "api/target/surefire-reports/TEST-b.xml", "x")
            old = self.write(root, "target/surefire-reports/TEST-old.xml", "x")
            self.write(root, "node_modules/x/build/test-results/TEST-c.xml", "x")
            self.write(root, "build/other.txt", "x")
            os.utime(old, (1, 1))
            found = gate_tests.fresh_junit_reports(root, time.time() - 60)
        self.assertEqual(found, [module, fresh])

    def test_configured_globs_reach_skipped_output_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jest = self.write(root, "coverage/junit.xml", JUNIT_GRADLE)
            gradle = self.write(root, "out/test-results/TEST-a.xml", JUNIT_GRADLE)
            self.write(root, "node_modules/x/coverage/junit.xml", JUNIT_GRADLE)
            since = time.time() - 60
            self.assertEqual(gate_tests.fresh_junit_reports(root, since), [])
            self.assertEqual(
                gate_tests.fresh_junit_reports(root, since, ["**/coverage/junit.xml"]),
                [jest],
            )
            self.assertEqual(
                gate_tests.fresh_junit_reports(root, since, ["out/test-results/*.xml"]),
                [gradle],
            )
            measured = gate_tests.measure_test_run(
                root, run_of(""), since, ["coverage/junit.xml"]
            )
        self.assertEqual((measured.parser, measured.measured), ("junit", True))

    def test_missing_file_is_not_fresh(self):
        self.assertFalse(gate_tests._modified_since(Path("/nonexistent/x.xml"), 0))

    def test_measure_prefers_console_then_fresh_junit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = time.time() - 60
            console = run_of("Ran 2 tests in 0.1s\n\nOK\n")
            self.assertEqual(
                gate_tests.measure_test_run(root, console, started).parser,
                "unittest",
            )
            silent = run_of("")
            self.assertFalse(
                gate_tests.measure_test_run(root, silent, started).measured
            )
            self.write(root, "reports/r.xml", JUNIT_GRADLE)
            measured = gate_tests.measure_test_run(
                root, silent, started, ["reports/*.xml"]
            )
        self.assertEqual((measured.parser, measured.executed), ("junit", 4))

    def test_configured_junit_patterns(self):
        self.assertEqual(
            gate_tests.junit_patterns({"junit": "out/*.xml"}), ["out/*.xml"]
        )
        self.assertEqual(gate_tests.junit_patterns({"junit": ["a", "b"]}), ["a", "b"])
        self.assertEqual(gate_tests.junit_patterns({"junit": 3}), [])
        self.assertEqual(gate_tests.junit_patterns({}), [])


def run_of(output, returncode=0, timed_out=False, command=("npm", "test")):
    return CommandRun(tuple(command), returncode, output, 1.25, timed_out)


def execution_of(**counts):
    return gate_tests_parsers.measured_execution("unittest", **counts)


THRESHOLDS = {"min_executed": 1, "max_skipped": 0, "max_failures": 0}


class EvaluateTestsTests(unittest.TestCase):
    def evaluate(self, run, execution, thresholds=THRESHOLDS, **options):
        return gate_tests.evaluate_tests(run, execution, thresholds, **options)

    def test_clean_run_passes_with_measurements(self):
        outcome = self.evaluate(run_of("ok"), execution_of(executed=5))
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.key, "tests")
        self.assertEqual(outcome.title, "Tests")
        self.assertIn("5 tests ran (unittest): 5 passed", outcome.summary)
        self.assertEqual(outcome.measurements["executed"], 5)
        self.assertEqual(outcome.measurements["duration_seconds"], 1.25)
        self.assertEqual(outcome.items, [])

    def test_failures_fail_with_output_tail(self):
        outcome = self.evaluate(
            run_of("boom\n\nAssertionError\n", returncode=1),
            execution_of(executed=3, failed=1),
        )
        self.assertEqual(outcome.status, "fail")
        self.assertIn("exited with code 1", outcome.summary)
        self.assertEqual(outcome.items[0].metric, "failed tests")
        self.assertIn("boom\nAssertionError", outcome.details[-1])

    def test_skipped_and_deselected_tests_fail(self):
        execution = execution_of(executed=4, skipped=2, xfailed=1, deselected=3)
        outcome = self.evaluate(run_of("ok"), execution)
        self.assertEqual(outcome.status, "fail")
        item = outcome.items[0]
        self.assertEqual(item.metric, "skipped/deselected tests")
        self.assertIn("including 1 expected failures", item.value)
        self.assertIn("3 deselected or filtered out", item.value)
        self.assertIn("broke 1 threshold(s)", outcome.summary)

    def test_skip_value_without_expected_failures(self):
        outcome = self.evaluate(run_of("ok"), execution_of(executed=2, skipped=1))
        self.assertEqual(
            outcome.items[0].value, "1 (1 skipped, 0 deselected or filtered out)"
        )

    def test_too_few_tests_fail(self):
        outcome = self.evaluate(
            run_of("ok"),
            execution_of(executed=3),
            {"tests": {"min_executed": 4, "max_skipped": 0}},
        )
        self.assertEqual([item.metric for item in outcome.items], ["executed tests"])
        self.assertEqual(outcome.items[0].limit, ">= 4")

    def test_invalid_limits_fall_back_to_defaults(self):
        outcome = self.evaluate(
            run_of("ok"), execution_of(executed=0), {"min_executed": "many"}
        )
        self.assertEqual([item.metric for item in outcome.items], ["executed tests"])

    def test_dropped_test_count_needs_approval(self):
        outcome = self.evaluate(
            run_of("ok"), execution_of(executed=8), previous_executed=10
        )
        self.assertEqual(outcome.status, "fail")
        self.assertIn("dropped from 10 to 8", outcome.items[0].value)
        approved = self.evaluate(
            run_of("ok"),
            execution_of(executed=8),
            previous_executed=10,
            allow_test_removal=True,
        )
        self.assertEqual(approved.status, "pass")
        self.assertEqual(approved.measurements["previous_executed"], 10)

    def test_timeout_fails_without_judging_partial_counts(self):
        outcome = self.evaluate(
            run_of("partial", returncode=124, timed_out=True),
            execution_of(executed=0),
        )
        self.assertEqual(outcome.status, "fail")
        self.assertIn("timed out", outcome.summary)
        self.assertEqual(outcome.items, [])

    def test_missing_executable_blocks(self):
        outcome = self.evaluate(
            run_of("No such file", returncode=127), gate_tests.TestExecution()
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertIn("could not start", outcome.summary)

    def test_recognized_prerequisite_blocks_when_no_test_ran(self):
        for execution in (gate_tests.TestExecution(), execution_of(executed=0)):
            with self.subTest(execution=execution):
                outcome = self.evaluate(
                    run_of("ModuleNotFoundError", returncode=1),
                    execution,
                    blocked_reason="project dependencies are missing",
                )
                self.assertEqual(outcome.status, "blocked")
                self.assertIn("dependencies are missing", outcome.summary)
                self.assertEqual(outcome.items, [])

    def test_measured_failures_fail_despite_a_prerequisite_hint(self):
        output = (
            "E   FileNotFoundError: [Errno 2] No such file or directory: 'x.json'\n"
            "=== 1 failed, 9 passed in 0.3s ===\n"
        )
        outcome = self.evaluate(
            run_of(output, returncode=1),
            gate_tests.parse_test_execution(output),
            blocked_reason="the required runtime or test command is missing",
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual([item.metric for item in outcome.items], ["failed tests"])
        self.assertIn("may also point to a missing prerequisite", outcome.summary)

    def test_failed_unmeasured_run_fails(self):
        outcome = self.evaluate(
            run_of("Segmentation fault", returncode=139), gate_tests.TestExecution()
        )
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.items, [])

    def test_success_with_no_count_is_blocked(self):
        outcome = self.evaluate(run_of("success"), gate_tests.TestExecution())
        self.assertEqual(outcome.status, "blocked")
        self.assertFalse(outcome.passed)
        self.assertIn("no recognizable summary", outcome.summary)

    def test_plain_go_test_block_explains_json(self):
        execution = gate_tests.parse_test_execution(GO_PLAIN.replace("FAIL", "ok"))
        outcome = self.evaluate(run_of(GO_PLAIN), execution)
        self.assertEqual(outcome.status, "blocked")
        self.assertIn("lists packages, not tests", outcome.summary)

    def test_output_tail_keeps_last_non_empty_lines(self):
        output = "\n".join(f"line {number}" for number in range(60)) + "\n\n"
        tail = gate_tests.output_tail("\x1b[31m" + output, limit=3)
        self.assertEqual(tail, "line 57\nline 58\nline 59")


def make_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def which_of(*available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class InferTestCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def infer(self, *available):
        return gate_tests.infer_test_command(self.root, which_of(*available))

    def test_nothing_declared(self):
        self.assertIsNone(self.infer("npm", "python3", "go"))

    def test_package_json_test_script_uses_the_lockfile_manager(self):
        self.write("package.json", '{"scripts": {"test": "vitest run"}}')
        self.assertEqual(self.infer("npm"), ["npm", "test"])
        self.write("pnpm-lock.yaml")
        self.assertEqual(self.infer("pnpm"), ["pnpm", "test"])
        self.assertIsNone(self.infer("npm"))

    def test_bun_runs_the_script_not_its_own_runner(self):
        self.write("package.json", '{"scripts": {"test": "jest"}}')
        self.write("bun.lock")
        self.assertEqual(self.infer("bun"), ["bun", "run", "test"])

    def test_yarn_and_local_executables(self):
        self.write("package.json", '{"scripts": {"test": "jest"}}')
        self.write("yarn.lock")
        yarn = make_executable(self.root / "node_modules/.bin/yarn")
        self.assertEqual(self.infer(), [str(yarn), "test"])

    def test_npm_placeholder_script_is_not_a_test_command(self):
        self.write(
            "package.json",
            '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        )
        self.assertIsNone(self.infer("npm"))

    def test_npm_workspaces(self):
        self.write("package.json", '{"workspaces": {"packages": ["packages/*"]}}')
        self.write("packages/api/package.json", "{}")
        (self.root / "packages/stray").mkdir()
        self.assertEqual(
            self.infer("npm"), ["npm", "test", "--workspaces", "--if-present"]
        )

    def test_invalid_package_json_is_ignored(self):
        self.write("package.json", "{not json")
        self.assertIsNone(self.infer("npm"))
        self.write("package.json", "[]")
        self.assertIsNone(self.infer("npm"))
        self.write("package.json", '{"workspaces": "packages/*"}')
        self.assertIsNone(self.infer("npm"))

    def test_python_unittest_discovers_the_tests_folder(self):
        self.write("pyproject.toml", "[project]\nname = 'app'\n")
        self.write("tests/test_app.py")
        self.assertEqual(
            self.infer("python3"),
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
        )
        self.write("tests/__init__.py")
        self.assertEqual(
            self.infer("python3")[-2:],
            ["-t", "."],
        )

    def test_python_without_test_folder_and_without_python(self):
        self.write("setup.py")
        self.assertEqual(self.infer("python"), ["python", "-m", "unittest", "discover"])
        self.assertIsNone(self.infer())

    def test_test_folder_with_python_files_marks_a_python_project(self):
        self.write("test/test_x.py")
        self.assertEqual(self.infer("python3")[-2:], ["-s", "test"])

    def test_pytest_when_configured_or_declared(self):
        self.write("tox.ini", "[pytest]\naddopts = -q\n")
        self.assertEqual(self.infer("python3"), ["python3", "-m", "pytest", "-q"])

    def test_pytest_conftest(self):
        self.write("conftest.py")
        self.assertEqual(self.infer("python3")[2], "pytest")

    def test_pytest_declared_as_dependency(self):
        self.write("requirements.txt", "pytest>=8\n")
        self.assertEqual(self.infer("python3")[2], "pytest")

    def test_pytest_installed_in_the_project_virtualenv(self):
        self.write("pyproject.toml")
        python = make_executable(self.root / ".venv/bin/python")
        self.assertEqual(self.infer("pytest")[2], "unittest")
        (self.root / ".venv/lib/python3.12/site-packages/pytest").mkdir(parents=True)
        self.assertEqual(self.infer(), [str(python), "-m", "pytest", "-q"])

    def test_pytest_on_path_for_a_system_python(self):
        self.write("pyproject.toml")
        self.assertEqual(self.infer("python3", "pytest")[2], "pytest")

    def test_toolchains(self):
        cases = [
            ("go.mod", "go", ["go", "test", "-json", "./..."]),
            ("Cargo.toml", "cargo", ["cargo", "test", "--workspace", "--offline"]),
            ("Package.swift", "swift", ["swift", "test"]),
            ("mix.exs", "mix", ["mix", "test"]),
            ("pubspec.yaml", "dart", ["dart", "test"]),
            ("App.csproj", "dotnet", ["dotnet", "test", "--no-restore"]),
            ("pom.xml", "mvn", ["mvn", "-o", "-q", "test"]),
        ]
        for manifest, tool, expected in cases:
            with self.subTest(manifest=manifest):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / manifest).write_text("", encoding="utf-8")
                    self.assertIsNone(gate_tests.infer_test_command(root, which_of()))
                    self.assertEqual(
                        gate_tests.infer_test_command(root, which_of(tool)), expected
                    )

    def test_ruby_prefers_rspec_when_specs_exist(self):
        self.write("Gemfile")
        self.assertIsNone(self.infer())
        self.assertEqual(self.infer("bundle"), ["bundle", "exec", "rake", "test"])
        self.write(".rspec")
        self.assertEqual(self.infer("bundle"), ["bundle", "exec", "rspec"])

    def test_php_uses_the_vendored_phpunit(self):
        phpunit = make_executable(self.root / "vendor/bin/phpunit")
        self.assertEqual(self.infer(), [str(phpunit)])

    def test_jvm_wrappers(self):
        self.write("pom.xml")
        mvnw = make_executable(self.root / "mvnw")
        self.assertEqual(self.infer(), [str(mvnw), "-o", "-q", "test"])
        (self.root / "pom.xml").unlink()
        self.write("gradlew")
        # cleanTest: an UP-TO-DATE test task would write no fresh report.
        self.assertEqual(
            self.infer(),
            [str(self.root / "gradlew"), "cleanTest", "test", "--offline"],
        )

    def test_cargo_freezes_only_a_committed_lockfile(self):
        self.write("Cargo.toml")
        self.assertEqual(
            self.infer("cargo"), ["cargo", "test", "--workspace", "--offline"]
        )
        self.write("Cargo.lock")
        self.assertEqual(
            self.infer("cargo"), ["cargo", "test", "--workspace", "--frozen"]
        )

    def test_flutter_projects_use_flutter_when_installed(self):
        self.write("pubspec.yaml", "dependencies:\n  flutter:\n    sdk: flutter\n")
        self.assertEqual(self.infer("flutter", "dart"), ["flutter", "test"])
        self.assertEqual(self.infer("dart"), ["dart", "test"])
        self.assertIsNone(self.infer())

    def test_unreadable_manifest_counts_as_empty(self):
        (self.root / "pyproject.toml").write_bytes(b"\xff\xfe")
        self.assertEqual(self.infer("python3")[2], "unittest")

    def test_project_python_prefers_the_virtualenv(self):
        python = make_executable(self.root / "venv/bin/python")
        self.assertEqual(
            gate_tests.project_python(self.root, which_of("python3")), str(python)
        )

    def test_package_manager_default_and_find_executable(self):
        self.assertEqual(gate_tests.package_manager(self.root), "npm")
        self.assertIsNone(
            gate_tests_command.find_executable(self.root, "x", which_of())
        )


if __name__ == "__main__":
    unittest.main()
