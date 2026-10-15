"""Offline Pi-harness bridge tests: core contract over the fixture fake.

No pi, no node, no inference, no systemd — these tests drive the fixture
driver's FakeSupervisor over real loopback HTTP with a small pure-python
mirror of the pi-bridge-core.mjs poll/dedupe/wait semantics. The mirror pins
the JS core's contract (cursor monotonicity, job:revision dedupe, transient
poll-error tolerance, inconclusive elapsed wait) for deterministic offline
verification; the JS core itself is exercised under the real pinned pi
runtime by agent_exec/harness/pi/pi_fixture.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

_FIXTURE_PATH = HERE.parent / "harness" / "pi" / "pi_fixture.py"
_spec = importlib.util.spec_from_file_location("pi_fixture_under_test", _FIXTURE_PATH)
pi_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi_fixture)

TRANSIENT = (429, 502, 503, 504)


class BridgeMirror:
    """Pure-python mirror of pi-bridge-core.mjs poll/dedupe/wait semantics."""

    def __init__(self, base_url: str, poll_interval: float = 0.02):
        self.base = base_url.rstrip("/")
        self.cursor = 0
        self.seen = set()  # "job_id:revision" — at-least-once delivery dedupe
        self.polls = 0
        self.poll_errors = 0
        self.poll_interval = poll_interval

    def absorb(self, payload: dict) -> list:
        # Cursor is monotonic: never move backwards on a misbehaving response.
        cursor = payload.get("cursor")
        if isinstance(cursor, int) and cursor > self.cursor:
            self.cursor = cursor
        fresh = []
        for ev in payload.get("events", []):
            key = f"{ev.get('job_id')}:{ev.get('revision')}"
            if key in self.seen:
                continue
            self.seen.add(key)
            fresh.append(ev)
        return fresh

    def poll_once(self) -> list:
        self.polls += 1
        with urllib.request.urlopen(
                f"{self.base}/events?cursor={self.cursor}", timeout=5) as res:
            payload = json.loads(res.read())
        return self.absorb(payload)

    def wait_terminal(self, max_wait_s: float) -> dict:
        deadline = time.monotonic() + max_wait_s
        collected, terminal = [], None
        while time.monotonic() < deadline:
            try:
                fresh = self.poll_once()
            except urllib.error.HTTPError as error:
                if error.code in TRANSIENT:
                    # Transient supervisor failures never cancel or relaunch.
                    self.poll_errors += 1
                    time.sleep(self.poll_interval)
                    continue
                raise
            except urllib.error.URLError:
                self.poll_errors += 1
                time.sleep(self.poll_interval)
                continue
            collected.extend(fresh)
            terminal = next((e for e in collected if e.get("terminal")), None)
            if terminal:
                break
            time.sleep(self.poll_interval)
        return {"events": collected, "poll_errors": self.poll_errors,
                "terminal": terminal, "inconclusive": terminal is None}

    def launch(self, operation: str, request_id: str) -> dict:
        body = json.dumps({"operation": operation, "request_id": request_id,
                           "deadline_seconds": 120}).encode()
        req = urllib.request.Request(f"{self.base}/jobs", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read())


class FakeSupervisorTestBase(unittest.TestCase):
    MODE = "immediate"

    def setUp(self):
        self.supervisor = pi_fixture.FakeSupervisor(self.MODE)
        self.base = self.supervisor.server.url

    def tearDown(self):
        self.supervisor.stop()


class RevisionDedupeTests(FakeSupervisorTestBase):
    MODE = "redelivery"

    def test_duplicate_redelivery_yields_exactly_one_terminal_receipt(self):
        mirror = BridgeMirror(self.base)
        launch = mirror.launch("fixture.op", "dedupe-req")
        self.assertFalse(launch["replayed"])
        receipt = mirror.wait_terminal(5)
        job_id = launch["job_id"]
        terminals = [e for e in receipt["events"]
                     if e["terminal"] and e["job_id"] == job_id]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(receipt["inconclusive"], False)
        # Every event was served multiple times (at-least-once redelivery),
        # but only two distinct job:revision pairs exist.
        # Keep polling: the fake re-serves ALL events on every poll, so each
        # additional poll redelivers both events; dedupe must collapse them
        # to zero fresh receipts.
        extra = [mirror.poll_once() for _ in range(3)]
        self.assertEqual(extra, [[], [], []],
                         "redelivered revisions must not appear fresh again")
        snapshot = self.supervisor.snapshot()
        self.assertGreaterEqual(snapshot["event_deliveries"], 8)
        self.assertEqual(len(mirror.seen), 2)
        # A second wait (new terminal marker reset) must deliver no new
        # receipts for already-seen revisions.
        second = mirror.wait_terminal(0.15)
        self.assertEqual([e for e in second["events"]
                          if f"{e['job_id']}:{e['revision']}" not in mirror.seen], [])


class CursorMonotonicityTests(FakeSupervisorTestBase):
    MODE = "delayed"

    def test_cursor_never_regresses_across_polls(self):
        mirror = BridgeMirror(self.base)
        mirror.launch("fixture.op", "cursor-req")
        previous = mirror.cursor
        observed = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            fresh = mirror.poll_once()
            observed.append(mirror.cursor)
            self.assertGreaterEqual(mirror.cursor, previous,
                                    "cursor must never move backwards")
            previous = mirror.cursor
            if any(e.get("terminal") for e in fresh):
                break
            time.sleep(0.02)
        self.assertTrue(observed, "at least one poll must occur")
        self.assertEqual(observed, sorted(observed),
                         "observed cursors must be non-decreasing")
        self.assertGreater(observed[-1], 0)

    def test_absorb_ignores_regressed_cursor(self):
        mirror = BridgeMirror(self.base)
        self.assertEqual(mirror.cursor, 0)
        mirror.absorb({"events": [], "cursor": 5})
        self.assertEqual(mirror.cursor, 5)
        mirror.absorb({"events": [], "cursor": 2})
        self.assertEqual(mirror.cursor, 5, "cursor must never move backwards")
        mirror.absorb({"events": [], "cursor": "9"})
        self.assertEqual(mirror.cursor, 5, "non-integer cursor must be ignored")


class DisconnectRetryTests(FakeSupervisorTestBase):
    MODE = "one_503"

    def test_one_503_then_success_delivers_terminal_exactly_once(self):
        mirror = BridgeMirror(self.base)
        launch = mirror.launch("fixture.op", "disconnect-req")
        receipt = mirror.wait_terminal(5)
        self.assertGreaterEqual(receipt["poll_errors"], 1)
        self.assertFalse(receipt["inconclusive"])
        terminals = [e for e in receipt["events"]
                     if e["terminal"] and e["job_id"] == launch["job_id"]]
        self.assertEqual(len(terminals), 1)
        snapshot = self.supervisor.snapshot()
        self.assertEqual(snapshot["launches"], [
            ("disconnect-req", launch["job_id"], False)],
            "a transient 503 must not relaunch work")


class ElapsedWaitInconclusiveTests(FakeSupervisorTestBase):
    MODE = "hold_then_complete"

    def test_elapsed_wait_is_inconclusive_and_never_cancels(self):
        mirror = BridgeMirror(self.base)
        launch = mirror.launch("fixture.op", "hold-req")
        started = time.monotonic()
        receipt = mirror.wait_terminal(0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertTrue(receipt["inconclusive"])
        self.assertIsNone(receipt["terminal"])
        # The elapsed wait must not cancel or relaunch the underlying job.
        with urllib.request.urlopen(
                f"{self.base}/jobs/{launch['job_id']}", timeout=5) as res:
            job = json.loads(res.read())
        self.assertEqual(job["state"], "running")
        self.assertEqual(len(self.supervisor.snapshot()["launches"]), 1)
        # A completion after the elapsed wait is still recovered via cursor.
        self.supervisor.trigger_completion()
        recovered = mirror.wait_terminal(5)
        self.assertFalse(recovered["inconclusive"])
        self.assertEqual(recovered["terminal"]["outcome"], "passed")


class RequestIdempotencyTests(FakeSupervisorTestBase):
    MODE = "delayed"

    def test_duplicate_request_id_returns_same_job_without_relaunch(self):
        first = BridgeMirror(self.base).launch("fixture.op", "same-req")
        second = BridgeMirror(self.base).launch("fixture.op", "same-req")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["state"], second["state"])
        snapshot = self.supervisor.snapshot()
        self.assertEqual(len(snapshot["launches"]), 2)
        self.assertEqual(
            [entry for entry in snapshot["launches"] if entry[2] is False],
            [("same-req", first["job_id"], False)],
            "exactly one actual launch for a retransmitted request id")
        # One job only: single launched event for this request id.
        launched_events = [e for e in self.supervisor.events
                           if e["kind"] == "launched"]
        self.assertEqual(len(launched_events), 1)

    def test_admission_rejects_unknown_operation_and_missing_request_id(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            BridgeMirror(self.base).launch("nope.op", "reject-1")
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read())
        self.assertIn("unknown operation", body["error"])
        req = urllib.request.Request(
            f"{self.base}/jobs",
            data=json.dumps({"operation": "fixture.op"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("request_id", json.loads(caught.exception.read())["error"])


class TerminalThenNoiseTests(FakeSupervisorTestBase):
    MODE = "hold_then_complete"

    def test_status_noise_after_terminal_cannot_add_receipts(self):
        mirror = BridgeMirror(self.base)
        launch = mirror.launch("fixture.op", "noise-req")
        self.supervisor.trigger_completion()
        first = mirror.wait_terminal(5)
        self.assertFalse(first["inconclusive"])
        job_id = launch["job_id"]
        self.supervisor.inject_status(job_id)
        self.supervisor.inject_status(job_id)
        second = mirror.wait_terminal(0.3)
        noise = [e for e in second["events"]
                 if e["job_id"] == job_id and not e["terminal"]]
        self.assertEqual(len(noise), 2, "noise events are visible as fresh")
        terminals = [e for e in second["events"] if e["terminal"]]
        self.assertEqual(terminals, [], "terminal is never re-delivered as new")
        total_receipts = sum(
            1 for e in first["events"] + second["events"]
            if e["terminal"] and e["job_id"] == job_id)
        self.assertEqual(total_receipts, 1)


class WireShapeTests(FakeSupervisorTestBase):
    MODE = "immediate"

    def test_contract_shapes_match_opencode_supervisor(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as res:
            self.assertEqual(json.loads(res.read())["ready"], True)
        mirror = BridgeMirror(self.base)
        launch = mirror.launch("fixture.op", "shape-req")
        self.assertEqual(sorted(launch), ["job_id", "replayed", "state"])
        receipt = mirror.wait_terminal(5)
        for event in receipt["events"]:
            self.assertEqual(
                sorted(event),
                ["cleanup", "job_id", "kind", "operation_id", "outcome",
                 "revision", "status", "terminal"])
        with urllib.request.urlopen(
                f"{self.base}/jobs/{launch['job_id']}", timeout=5) as res:
            job = json.loads(res.read())
        for field in ("job_id", "request_id", "operation_id", "state",
                      "outcome", "exit_code", "exit_status", "cleanup",
                      "report"):
            self.assertIn(field, job)
        self.assertEqual(job["report"],
                         {"total": 3, "passed": 3, "failed": 0, "complete": True})
        try:
            urllib.request.urlopen(f"{self.base}/jobs/unknown-job", timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 404)


if __name__ == "__main__":
    unittest.main()
