"""Report reader tests: interrupted/empty/truncated reports stay honest."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.reports import (  # noqa: E402
    read_go_test_json, read_junit_xml, read_vitest_json, summarize)


def valid_report(total=3, failed=0):
    assertions = [{"status": "passed", "fullName": "a works",
                   "failureMessages": []} for _ in range(total - failed)]
    assertions += [{"status": "failed", "fullName": "b breaks",
                    "failureMessages": ["AssertionError: expected 1 to be 2"]}] * failed
    return {
        "numTotalTestSuites": 1, "numPassedTestSuites": 1 if not failed else 0,
        "numFailedTestSuites": failed, "numPendingTestSuites": 0,
        "numTotalTests": total, "numPassedTests": total - failed,
        "numFailedTests": failed, "numPendingTests": 0, "numTodoTests": 0,
        "startTime": 1, "success": failed == 0,
        "testResults": [{"assertionResults": assertions}],
    }


class VitestJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload: bytes) -> Path:
        path = self.dir / "report.json"
        path.write_bytes(payload)
        return path

    def test_missing_report_is_incomplete(self):
        result = read_vitest_json(self.dir / "absent.json")
        self.assertFalse(result["complete"])
        self.assertEqual(result["integrity"], "missing")

    def test_empty_report_is_incomplete(self):
        result = read_vitest_json(self.write(b""))
        self.assertEqual(result["integrity"], "empty")
        self.assertFalse(result["complete"])

    def test_truncated_json_is_partial_evidence(self):
        good = json.dumps(valid_report(2, 1)).encode()
        result = read_vitest_json(self.write(good[: len(good) // 2]))
        self.assertEqual(result["integrity"], "truncated")
        self.assertFalse(result["complete"])

    def test_valid_pass(self):
        result = read_vitest_json(self.write(json.dumps(valid_report(4, 0)).encode()))
        self.assertTrue(result["complete"])
        self.assertEqual((result["total"], result["passed"], result["failed"]), (4, 4, 0))
        self.assertTrue(result["runner_success"])

    def test_valid_fail_preserves_failure_evidence(self):
        result = read_vitest_json(self.write(json.dumps(valid_report(3, 1)).encode()))
        self.assertTrue(result["complete"])
        self.assertEqual(result["failed"], 1)
        self.assertIn("AssertionError", result["failures"][0]["failureMessages"][0])

    def test_structurally_valid_empty_selection_is_not_success(self):
        payload = valid_report(0, 0)
        payload["testResults"] = []
        result = read_vitest_json(self.write(json.dumps(payload).encode()))
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["complete"])
        self.assertTrue(any("incomplete" in note for note in result["notes"]))

    def test_zero_tests_with_suites_flagged_as_incomplete_required_check(self):
        result = read_vitest_json(self.write(json.dumps(valid_report(0, 0)).encode()))
        self.assertFalse(result["complete"])
        self.assertTrue(any("never success" in note for note in result["notes"]))

    def test_zero_test_junit_is_not_complete(self):
        payload = (b"<testsuites><testsuite name='s' tests='0' failures='0'"
                   b" errors='0' skipped='0'></testsuite></testsuites>")
        result = read_junit_xml(self.write(payload))
        self.assertFalse(result["complete"])
        self.assertTrue(any("never success" in n for n in result["notes"]))


class JunitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_junit_counts(self):
        payload = (b"<testsuites><testsuite name='s' tests='3' failures='1'"
                   b" errors='1' skipped='1'></testsuite></testsuites>")
        path = self.dir / "junit.xml"
        path.write_bytes(payload)
        result = read_junit_xml(path)
        self.assertTrue(result["complete"])
        self.assertEqual((result["total"], result["failed"],
                          result["errors"], result["skipped"]), (3, 1, 1, 1))

    def test_truncated_junit(self):
        path = self.dir / "junit.xml"
        path.write_bytes(b"<testsuites><testsuite tests='3'")
        result = read_junit_xml(path)
        self.assertEqual(result["integrity"], "truncated")
        self.assertFalse(result["complete"])


class SummarizeTests(unittest.TestCase):
    def test_any_incomplete_keeps_check_incomplete(self):
        partial = {"kind": "vitest-json", "integrity": "truncated", "complete": False}
        good = {"kind": "vitest-json", "integrity": "valid", "complete": True}
        self.assertFalse(summarize([good, partial])["all_complete"])
        self.assertTrue(summarize([good, good])["all_complete"])
        self.assertFalse(summarize([])["all_complete"])


# ---------------------------------------------------------------------------
# read_go_test_json: real test2json event shapes (grammar per
# tests/test_go.py, captured from go1.24.13 on p100)
# ---------------------------------------------------------------------------

def _go_event(action, package=None, test=None, elapsed=None, output=None):
    event = {"Action": action, "Time": "2026-10-15T12:00:00.000000000Z"}
    if package is not None:
        event["Package"] = package
    if test is not None:
        event["Test"] = test
    if elapsed is not None:
        event["Elapsed"] = elapsed
    if output is not None:
        event["Output"] = output
    return event


GO_PKG = "example.com/app/internal/cli"

GO_PASS_STREAM = "\n".join(json.dumps(e) for e in [
    _go_event("start", package=GO_PKG),
    _go_event("run", package=GO_PKG, test="TestAlpha"),
    _go_event("output", package=GO_PKG, test="TestAlpha",
              output="=== RUN   TestAlpha\n"),
    _go_event("output", package=GO_PKG, test="TestAlpha",
              output="--- PASS: TestAlpha (0.00s)\n"),
    _go_event("pass", package=GO_PKG, test="TestAlpha", elapsed=0.0),
    _go_event("output", package=GO_PKG,
              output="ok  \texample.com/app/internal/cli\t0.157s\n"),
    _go_event("pass", package=GO_PKG, elapsed=0.157),
]) + "\n"

GO_ALL_NO_TEST_STREAM = "\n".join(json.dumps(e) for e in [
    _go_event("start", package=GO_PKG),
    _go_event("output", package=GO_PKG,
              output="?   \texample.com/app/internal/cli\t[no test files]\n"),
    _go_event("skip", package=GO_PKG, elapsed=0),
]) + "\n"

GO_FAIL_STREAM = "\n".join(json.dumps(e) for e in [
    _go_event("start", package=GO_PKG),
    _go_event("run", package=GO_PKG, test="TestBad"),
    _go_event("output", package=GO_PKG, test="TestBad",
              output="=== RUN   TestBad\n"),
    _go_event("output", package=GO_PKG, test="TestBad",
              output="    stream_test.go:9: assertion failed: expected 2 got 3\n"),
    _go_event("output", package=GO_PKG, test="TestBad",
              output="--- FAIL: TestBad (0.00s)\n"),
    _go_event("fail", package=GO_PKG, test="TestBad", elapsed=0.0),
    _go_event("output", package=GO_PKG,
              output="FAIL\texample.com/app/internal/cli\t0.002s\n"),
    _go_event("fail", package=GO_PKG, elapsed=0.002),
]) + "\n"


class GoTestJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, stream: str) -> dict:
        path = self.dir / "go-test.json"
        path.write_text(stream)
        return read_go_test_json(path)

    def test_all_no_test_packages_are_not_complete(self):
        # Complete stream, every package skipped for [no test files]: zero
        # collected tests is an incomplete required check, never success.
        result = self._read(GO_ALL_NO_TEST_STREAM)
        self.assertEqual(result["integrity"], "complete")
        self.assertEqual(result["overall"], "no_tests")
        self.assertFalse(result["complete"])
        self.assertEqual(result["packages_no_tests"], 1)
        self.assertEqual(result["tests_executed"], 0)
        self.assertTrue(any("never success" in note for note in result["notes"]))

    def test_passing_stream_with_executed_tests_is_complete(self):
        result = self._read(GO_PASS_STREAM)
        self.assertEqual(result["overall"], "passed")
        self.assertTrue(result["complete"])
        self.assertEqual((result["tests_passed"], result["tests_executed"]), (1, 1))

    def test_passing_stream_with_no_test_package_stays_complete(self):
        # One executed passing package plus one no-test package: still a
        # pass, with the no-test package separately counted and noted.
        mixed = GO_PASS_STREAM + "\n" + "\n".join(
            json.dumps(e) for e in [
                _go_event("start", package="example.com/app/cmd/app"),
                _go_event("output", package="example.com/app/cmd/app",
                          output="?   \texample.com/app/cmd/app\t[no test files]\n"),
                _go_event("skip", package="example.com/app/cmd/app", elapsed=0),
            ]) + "\n"
        result = self._read(mixed)
        self.assertEqual(result["overall"], "passed")
        self.assertTrue(result["complete"])
        self.assertEqual(result["packages_no_tests"], 1)
        self.assertTrue(any("counted separately" in note for note in result["notes"]))

    def test_failing_stream_is_incomplete_with_failures_preserved(self):
        result = self._read(GO_FAIL_STREAM)
        self.assertEqual(result["overall"], "failed")
        self.assertFalse(result["complete"])
        self.assertEqual(result["tests_failed"], 1)
        self.assertEqual(result["failures"][0]["test"], "TestBad")
        self.assertIn("expected 2 got 3", result["failures"][0]["output"])


if __name__ == "__main__":
    unittest.main()
