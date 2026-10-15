"""Unit tests for the Go test2json event-stream parser (pure, no execution).

NOTE (checkpoint 6 admission): the parser `parse_go_test_json_stream` is
defined HERE, in the test file, on purpose. Admission integration is expected
to MOVE this function into a `go_reports.py` module next to `reports.py`;
that module is deliberately NOT created by this checkpoint (module layout is
owned by the admission step). These tests define the contract that module
must keep.

Contract highlights, all verified against the pinned go1.24.13 toolchain on
p100 (see F-go receipt):

- Packages with no test files surface as package-level `skip` actions. They
  are counted separately and never constitute whole-project test success.
- Failures observed before the stream ends are preserved even when the stream
  is truncated or contains non-JSON lines; a truncated stream is incomplete
  evidence, never a pass.
- Cached results: with go1.24.13, a cached `go test -json` run REPLAYS the
  stored stream including `=== RUN` events, so the absence of run events is
  NOT a valid cached signal. The only reliable marker is the package summary
  output line `ok <pkg> (cached)`. `-count=1` always executes; observed cdx
  streams never contain `(cached)`.
- A package-level fail with no test events is a package failure (compile or
  setup), distinct from a failed test assertion.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.go_reports import parse_go_test_json_stream  # noqa: E402


# ---------------------------------------------------------------------------
# Parser under test (scheduled to move into agent_exec/go_reports.py)
# ---------------------------------------------------------------------------

PACKAGE_TERMINAL = {"pass", "fail", "skip"}
TEST_TERMINAL = {"pass", "fail", "skip"}


# Parser moved to agent_exec/go_reports.py at admission.


# ---------------------------------------------------------------------------
# Realistic fixture streams (shapes captured from go1.24.13 on p100)
# ---------------------------------------------------------------------------

def _event(action, package=None, test=None, elapsed=None, output=None):
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


PKG_A = "example.com/app/internal/cli"
PKG_B = "example.com/app/cmd/app"

VALID_STREAM = "\n".join(json.dumps(e) for e in [
    _event("start", package=PKG_A),
    _event("run", package=PKG_A, test="TestAlpha"),
    _event("output", package=PKG_A, test="TestAlpha", output="=== RUN   TestAlpha\n"),
    _event("output", package=PKG_A, test="TestAlpha", output="--- PASS: TestAlpha (0.00s)\n"),
    _event("pass", package=PKG_A, test="TestAlpha", elapsed=0.0),
    _event("run", package=PKG_A, test="TestBeta"),
    _event("pass", package=PKG_A, test="TestBeta", elapsed=0.01),
    _event("output", package=PKG_A, output="ok  \texample.com/app/internal/cli\t0.157s\n"),
    _event("pass", package=PKG_A, elapsed=0.157),
    _event("start", package=PKG_B),
    _event("output", package=PKG_B, output="?   \texample.com/app/cmd/app\t[no test files]\n"),
    _event("skip", package=PKG_B, elapsed=0),
]) + "\n"

FAILING_STREAM = "\n".join(json.dumps(e) for e in [
    _event("start", package=PKG_A),
    _event("run", package=PKG_A, test="TestGood"),
    _event("pass", package=PKG_A, test="TestGood", elapsed=0.0),
    _event("run", package=PKG_A, test="TestBad"),
    _event("output", package=PKG_A, test="TestBad", output="=== RUN   TestBad\n"),
    _event("output", package=PKG_A, test="TestBad",
           output="    stream_test.go:9: assertion failed: expected 2 got 3\n"),
    _event("output", package=PKG_A, test="TestBad", output="--- FAIL: TestBad (0.00s)\n"),
    _event("fail", package=PKG_A, test="TestBad", elapsed=0.0),
    _event("output", package=PKG_A, output="FAIL\texample.com/app/internal/cli\t0.002s\n"),
    _event("fail", package=PKG_A, elapsed=0.002),
]) + "\n"

# Go 1.24.13 evidence: a cached replay includes === RUN events; the summary
# line carries "(cached)".
CACHED_REPLAY_STREAM = "\n".join(json.dumps(e) for e in [
    _event("start", package=PKG_A),
    _event("run", package=PKG_A, test="TestAlpha"),
    _event("output", package=PKG_A, test="TestAlpha", output="=== RUN   TestAlpha\n"),
    _event("output", package=PKG_A, test="TestAlpha", output="--- PASS: TestAlpha (0.00s)\n"),
    _event("pass", package=PKG_A, test="TestAlpha", elapsed=0.0),
    _event("output", package=PKG_A, output="ok  \texample.com/app/internal/cli\t(cached)\n"),
    _event("pass", package=PKG_A, elapsed=0),
]) + "\n"

EXECUTED_SUMMARY_EVENTS = [
    _event("start", package=PKG_A),
    _event("run", package=PKG_A, test="TestAlpha"),
    _event("output", package=PKG_A, test="TestAlpha", output="=== RUN   TestAlpha\n"),
    _event("output", package=PKG_A, test="TestAlpha", output="--- PASS: TestAlpha (0.00s)\n"),
    _event("pass", package=PKG_A, test="TestAlpha", elapsed=0.0),
    _event("output", package=PKG_A, output="ok  \texample.com/app/internal/cli\t0.157s\n"),
    _event("pass", package=PKG_A, elapsed=0.157),
]
EXECUTED_SUMMARY_STREAM = "\n".join(json.dumps(e) for e in EXECUTED_SUMMARY_EVENTS)

COMPILE_FAILURE_STREAM = "\n".join(json.dumps(e) for e in [
    _event("start", package=PKG_A),
    _event("output", package=PKG_A,
           output="# example.com/app/internal/cli\n"),
    _event("output", package=PKG_A,
           output="stream_test.go:9:2: undefined: helperFunc\n"),
    _event("fail", package=PKG_A, elapsed=0),
]) + "\n"


class ValidStreamTests(unittest.TestCase):
    def test_valid_stream_parses(self):
        result = parse_go_test_json_stream(VALID_STREAM)
        self.assertEqual(result["integrity"], "complete")
        self.assertEqual(result["overall"], "passed")
        self.assertEqual(result["counts"]["packages_total"], 2)
        self.assertEqual(result["counts"]["packages_passed"], 1)
        self.assertEqual(result["counts"]["packages_no_tests"], 1)
        self.assertEqual(result["counts"]["tests_passed"], 2)
        self.assertEqual(result["counts"]["tests_executed"], 2)

    def test_passing_package_is_distinguishable_from_no_test_files(self):
        result = parse_go_test_json_stream(VALID_STREAM)
        self.assertEqual(result["packages"][PKG_A]["status"], "passed")
        self.assertEqual(result["packages"][PKG_B]["status"], "skipped")


class NoTestFilesTests(unittest.TestCase):
    ALL_SKIP_STREAM = "\n".join(json.dumps(e) for e in [
        _event("start", package=PKG_B),
        _event("output", package=PKG_B,
               output="?   \texample.com/app/cmd/app\t[no test files]\n"),
        _event("skip", package=PKG_B, elapsed=0),
    ]) + "\n"

    def test_no_test_files_package_is_not_success(self):
        result = parse_go_test_json_stream(self.ALL_SKIP_STREAM)
        self.assertEqual(result["counts"]["packages_no_tests"], 1)
        self.assertNotEqual(result["overall"], "passed")
        self.assertEqual(result["overall"], "no_tests")

    def test_no_test_files_do_not_mask_full_success(self):
        result = parse_go_test_json_stream(VALID_STREAM)
        # One real executed passing package plus one no-test package is still
        # a pass, but the no-test package stays separately counted.
        self.assertEqual(result["overall"], "passed")
        self.assertEqual(result["counts"]["packages_no_tests"], 1)


class FailingPackageTests(unittest.TestCase):
    def test_failure_before_stream_end_is_preserved(self):
        result = parse_go_test_json_stream(FAILING_STREAM)
        self.assertEqual(result["overall"], "failed")
        self.assertEqual(result["counts"]["packages_failed"], 1)
        self.assertEqual(result["counts"]["tests_failed"], 1)
        self.assertEqual(result["failures"][0]["test"], "TestBad")
        self.assertIn("expected 2 got 3", result["failures"][0]["output"])

    def test_package_failure_class_distinct_from_test_failure(self):
        result = parse_go_test_json_stream(COMPILE_FAILURE_STREAM)
        self.assertEqual(result["overall"], "failed")
        self.assertEqual(result["counts"]["tests_failed"], 0)
        self.assertEqual(result["packages"][PKG_A]["failure_class"],
                         "package_failure")


class TruncatedStreamTests(unittest.TestCase):
    def test_truncated_last_line_keeps_earlier_failures(self):
        cut = FAILING_STREAM.rsplit("\n", 2)[0]  # drop final package fail event
        truncated = cut[:len(cut) - 20] + "MID-JSON-TRUNCATION"
        result = parse_go_test_json_stream(truncated)
        self.assertEqual(result["integrity"], "truncated")
        self.assertNotEqual(result["overall"], "failed")
        self.assertNotEqual(result["overall"], "passed")
        self.assertEqual(result["overall"], "incomplete")
        # The test-level failure observed before truncation survives.
        self.assertEqual(result["counts"]["tests_failed"], 1)
        self.assertEqual(result["failures"][0]["test"], "TestBad")

    def test_garbage_interleaved_line_marks_incomplete(self):
        poisoned = FAILING_STREAM.replace(
            json.dumps(_event("run", package=PKG_A, test="TestGood")),
            "go: downloading example.com/dep v1.2.3")
        result = parse_go_test_json_stream(poisoned)
        self.assertEqual(result["integrity"], "truncated")
        self.assertEqual(result["overall"], "incomplete")
        self.assertEqual(len(result["bad_lines"]), 1)


class CachedVersusExecutedTests(unittest.TestCase):
    def test_cached_summary_line_labels_package_cached(self):
        result = parse_go_test_json_stream(CACHED_REPLAY_STREAM)
        self.assertTrue(result["packages"][PKG_A]["cached"])
        self.assertEqual(result["counts"]["packages_cached"], 1)

    def test_run_events_present_do_not_clear_cached_label(self):
        # go1.24.13 replays === RUN events on cache hits; only the summary
        # line decides. This locks the corrected heuristic in place.
        result = parse_go_test_json_stream(CACHED_REPLAY_STREAM)
        self.assertGreater(result["counts"]["tests_executed"], 0)
        self.assertTrue(result["packages"][PKG_A]["cached"])

    def test_executed_summary_is_not_cached(self):
        result = parse_go_test_json_stream(EXECUTED_SUMMARY_STREAM)
        self.assertFalse(result["packages"][PKG_A]["cached"])
        self.assertEqual(result["counts"]["packages_cached"], 0)

    def test_count_equals_one_stream_observed_executed(self):
        # Real cdx evidence: -count=1 streams carry explicit elapsed times in
        # summary lines and never "(cached)".
        self.assertIn("0.157s", EXECUTED_SUMMARY_STREAM)
        self.assertNotIn("(cached)", EXECUTED_SUMMARY_STREAM)
        result = parse_go_test_json_stream(EXECUTED_SUMMARY_STREAM)
        self.assertFalse(result["packages"][PKG_A]["cached"])


if __name__ == "__main__":
    unittest.main()