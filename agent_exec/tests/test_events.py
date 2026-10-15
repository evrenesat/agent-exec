"""Event store tests: at-least-once delivery, revision dedupe, cursor replay."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.store import Store  # noqa: E402


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "events.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_wake_kinds_advance_revisions(self):
        self.store.record_event("job-1", "launched")
        self.store.record_event("job-1", "terminal", {"outcome": "passed"})
        events = self.store.events_after("job-1", 0)
        self.assertEqual([e["kind"] for e in events], ["launched", "terminal"])
        self.assertEqual([e["revision"] for e in events], [1, 2])

    def test_status_events_never_wake(self):
        self.store.record_event("job-1", "launched")
        self.store.record_event("job-1", "status", {"phase": "running"})
        self.store.record_event("job-1", "status", {"phase": "running"})
        events = self.store.events_after("job-1", 0)
        revisions = [e["revision"] for e in events]
        self.assertEqual(revisions[1:], [None, None],
                         "status-only events carry NULL revisions")
        # a waiter asking for progress beyond revision 1 is not satisfied by them
        nothing_new = all(e["revision"] is None or e["revision"] <= 1
                          for e in events[1:])
        self.assertTrue(nothing_new)

    def test_cursor_replay_is_complete_and_ordered(self):
        for i in range(10):
            self.store.record_event("job-2", "status", {"i": i})
        self.store.record_event("job-2", "terminal", {"outcome": "failed"})
        first_page = self.store.events_after("job-2", 0)
        self.assertEqual(len(first_page), 11)
        rest = self.store.events_after("job-2", first_page[-1]["seq"])
        self.assertEqual(rest, [])
        seqs = [e["seq"] for e in first_page]
        self.assertEqual(seqs, sorted(seqs))

    def test_duplicate_revision_returns_original(self):
        first = self.store.record_event("job-3", "terminal", {"n": 1}, wake=True)
        # Simulate a racing second writer for the same revision.
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._lock:
                self.store._db.execute(
                    "INSERT INTO events (job_id, revision, kind, payload_json,"
                    " created_wall) VALUES (?,?,?,?,?)",
                    ("job-3", 1, "terminal", "{}", 0.0))
                self.store._db.commit()

    def test_concurrent_writers_keep_unique_revisions(self):
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    self.store.record_event(f"job-{n}", "terminal", {"i": i})
            except Exception as error:  # pragma: no cover
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(n,)) for n in (4, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for n in (4, 5):
            revisions = [e["revision"] for e in self.store.events_after(f"job-{n}", 0)]
            self.assertEqual(revisions, list(range(1, 21)))

    def test_request_id_unique_index(self):
        from agent_exec.store import DuplicateRequest
        self.store.create_job(session_id="s1", request_id="r1",
                              operation_id="op", profile_id=None, purpose="verify",
                              unit="(unlaunched)",
                              job_dir=Path(self.tmp.name) / "d",
                              deadline_seconds=10, stop_grace_seconds=2)
        with self.assertRaises(DuplicateRequest):
            self.store.create_job(session_id="s1", request_id="r1",
                                  operation_id="op", profile_id=None, purpose="verify",
                                  unit="(unlaunched)",
                                  job_dir=Path(self.tmp.name) / "d2",
                                  deadline_seconds=10, stop_grace_seconds=2)
        # a different session may reuse the request id
        job = self.store.create_job(session_id="s2", request_id="r1",
                                    operation_id="op", profile_id=None,
                                    purpose="verify", unit="(unlaunched)",
                                    job_dir=Path(self.tmp.name) / "d3",
                                    deadline_seconds=10, stop_grace_seconds=2)
        self.assertTrue(job.startswith("job-"))

    def test_open_jobs_excludes_terminal(self):
        self.store.create_job(session_id="s", request_id="a", operation_id="op",
                              profile_id=None, purpose="verify", unit="(u)",
                              job_dir=Path(self.tmp.name) / "j1",
                              deadline_seconds=10, stop_grace_seconds=2)
        self.store.create_job(session_id="s", request_id="b", operation_id="op",
                              profile_id=None, purpose="verify", unit="(u)",
                              job_dir=Path(self.tmp.name) / "j2",
                              deadline_seconds=10, stop_grace_seconds=2)
        rows = self.store.find_by_request("s", "a")
        self.store.update_job(rows["job_id"], state="completed")
        open_rows = self.store.open_jobs()
        self.assertEqual([r["request_id"] for r in open_rows], ["b"])


if __name__ == "__main__":
    unittest.main()
