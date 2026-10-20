"""Controller tests against a fake backend (mocked state, no real units).

The real Linux proof lives in `cli.py selftest --backend linux`, which the
plan's verification command runs; these tests pin the controller contract
without touching systemd.
"""

from __future__ import annotations

import json
import sys
import time
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.jobs import (  # noqa: E402
    JobAccessError, JobController, JobError, UnknownJob)
from agent_exec.store import Store  # noqa: E402


class FakeBackend:
    """In-memory stand-in reproducing the systemd observation contract."""

    def __init__(self):
        self.units: dict[str, dict] = {}
        self.launch_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.fail_next_launch = False
        self.fail_after_create = False
        self.manager_down = False
        self.log_entries = [{"cursor": "c1", "message": "line one",
                             "monotonic_ts": "1"}]

    def launch(self, spec):
        if self.fail_next_launch:
            self.fail_next_launch = False
            raise RuntimeError("simulated systemd-run failure")
        unit = linux_jobs.unit_name(spec.job_id)
        self.launch_calls.append(unit)
        self.units[unit] = {"phase": "running", "record": None,
                            "cgroup": "failed", "collected": False,
                            "stop_signal": None}
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("simulated failure after unit creation")
        return {"unit": unit, "launch_wall": 1.0, "boot_id": "fake-boot",
                "invocation_id": "fake-invocation", "launch_argv": ["x"]}

    def set_terminal(self, spec, record, cgroup="verified_empty"):
        unit = linux_jobs.unit_name(spec.job_id)
        self.units[unit].update({"phase": "terminal", "record": record,
                                 "cgroup": cgroup, "collected": True})

    def inspect(self, spec):
        # A unit systemd has never seen reports LoadState=not-found; mirror
        # that instead of raising on unknown units.
        state = self.units.get(linux_jobs.unit_name(spec.job_id))
        if state is None:
            return {"unit": linux_jobs.unit_name(spec.job_id),
                    "load_state": "not-found", "active_state": None,
                    "sub_state": None, "result": None, "exec_main_code": None,
                    "exec_main_status": None, "invocation_id": None,
                    "exit_record": None, "cgroup": "unknown",
                    "observed_wall": 2.0, "phase": "inconclusive",
                    "ended": True,
                    "warning": "unit collected without a usable exit record; "
                               "result unknown (never success)"}
        if self.manager_down:
            return {"unit": linux_jobs.unit_name(spec.job_id),
                    "load_state": "unknown", "active_state": None,
                    "sub_state": None, "result": None, "exec_main_code": None,
                    "exec_main_status": None, "invocation_id": None,
                    "exit_record": None, "cgroup": "unknown",
                    "observed_wall": 2.0, "phase": "unknown",
                    "ended": False, "warning": None}
        collected = state["collected"]
        record = state["record"]
        if collected and (record is None or record.get("corrupt")):
            phase = "inconclusive"
        elif collected:
            phase = "terminal"
        else:
            phase = "running"
        return {"unit": linux_jobs.unit_name(spec.job_id),
                "load_state": "not-found" if collected else "loaded",
                "active_state": None if collected else "active",
                "sub_state": None, "result": None, "exec_main_code": None,
                "exec_main_status": None, "invocation_id": None,
                "exit_record": record,
                "cgroup": state["cgroup"], "observed_wall": 2.0,
                "phase": phase, "ended": collected,
                "warning": ("unit collected without a usable exit record; result "
                            "unknown (never success)")
                if phase == "inconclusive" else None}

    classify = staticmethod(linux_jobs.classify)

    def stop(self, spec, kill=False):
        unit = linux_jobs.unit_name(spec.job_id)
        self.stop_calls.append(unit)
        self.units[unit]["stop_signal"] = "SIGKILL" if kill else "TERM"
        self.set_terminal(spec, {"service_result": "signal", "exit_code": "killed",
                                 "exit_status": "TERM" if not kill else "KILL"})
        return {"requested": True, "signal": "SIGKILL" if kill else "TERM",
                "rc": 0}

    def read_logs(self, spec, after_cursor=None, max_bytes=32768):
        return {"entries": self.log_entries, "next_cursor": "c1",
                "bytes": 20, "truncated": False, "gap": None}

    def reconcile(self, spec, cancel_requested=False):
        observation = self.inspect(spec)
        return {"observation": observation,
                "verdict": self.classify(observation,
                                         cancel_requested=cancel_requested)}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_request_id_idempotency_launches_once(self):
        first = self.controller.launch(["/bin/true"], request_id="r1",
                                       operation_id="op")
        second = self.controller.launch(["/bin/true"], request_id="r1",
                                        operation_id="op")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.backend.launch_calls), 1)

    def test_success_receipt_requires_record_and_empty_cgroup(self):
        job = self.controller.launch(["/bin/true"], request_id="ok",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"})
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "passed")
        self.assertEqual(receipt["cleanup"], "verified_empty")

    def test_collected_without_record_is_inconclusive_never_success(self):
        job = self.controller.launch(["/bin/true"], request_id="ghost",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.units[linux_jobs.unit_name(spec.job_id)]["collected"] = True
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "inconclusive")
        self.assertIsNotNone(receipt["inconclusive_reason"])

    def test_nonzero_and_timeout_outcomes(self):
        job = self.controller.launch(["/bin/false"], request_id="f",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "exit-code",
                                         "exit_code": "exited", "exit_status": "3"})
        self.assertEqual(self.controller.status(job)["outcome"], "failed")

        job2 = self.controller.launch(["/bin/sleep", "300"], request_id="t",
                                      operation_id="op")["job_id"]
        spec2 = self.controller._spec_for(self.controller.store.get_job(job2))
        self.backend.set_terminal(spec2, {"service_result": "timeout",
                                          "exit_code": "killed",
                                          "exit_status": "KILL"})
        self.assertEqual(self.controller.status(job2)["outcome"], "timed_out")

    def test_cancel_running_job(self):
        job = self.controller.launch(["/bin/sleep", "300"], request_id="c",
                                     operation_id="op")["job_id"]
        result = self.controller.cancel(job, reason="test")
        self.assertTrue(result["cancelled"])
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "cancelled")

    def test_cross_session_access_rejected(self):
        job = self.controller.launch(["/bin/true"], request_id="s",
                                     operation_id="op")["job_id"]
        other = JobController(self.root / "store.sqlite",
                              evidence_root=self.root / "evidence",
                              backend=self.backend)
        try:
            with self.assertRaises(JobAccessError):
                other.status(job)
        finally:
            other.close()

    def test_unknown_job(self):
        with self.assertRaises(UnknownJob):
            self.controller.status("job-does-not-exist")

    def test_launch_failure_blocks_and_replays(self):
        self.backend.fail_next_launch = True
        with self.assertRaises(JobError):
            self.controller.launch(["/x"], request_id="bad", operation_id="op")
        replay = self.controller.launch(["/x"], request_id="bad", operation_id="op")
        self.assertTrue(replay["replayed"])
        receipt = self.controller.status(replay["job_id"])
        self.assertEqual(receipt["outcome"], "blocked")

    def test_wait_elapsed_does_not_cancel(self):
        job = self.controller.launch(["/bin/sleep", "300"], request_id="w",
                                     operation_id="op")["job_id"]
        result = self.controller.wait(job, max_wait_seconds=0.3)
        self.assertTrue(result["elapsed"])
        self.assertEqual(self.controller.status(job)["phase"], "running")

    def test_wait_returns_terminal_immediately(self):
        job = self.controller.launch(["/bin/true"], request_id="wt",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"})
        result = self.controller.wait(job, max_wait_seconds=5)
        self.assertFalse(result["elapsed"])
        self.assertEqual(result["receipt"]["outcome"], "passed")

    def test_wait_after_revision_skips_status_noise(self):
        job = self.controller.launch(["/bin/true"], request_id="rev",
                                     operation_id="op")["job_id"]
        launched = self.controller.events(job)["events"][-1]
        self.controller.status(job)  # records a NULL-revision status event
        noise = self.controller.events(job)["events"][-1]
        self.assertIsNone(noise["revision"])
        result = self.controller.wait(job, after_revision=launched["revision"],
                                      max_wait_seconds=0.2)
        self.assertTrue(result["elapsed"],
                        "status-only events must not satisfy a revision wait")

    def test_reconcile_absorbs_terminal_after_restart(self):
        job = self.controller.launch(["/bin/true"], request_id="rec",
                                     operation_id="op")["job_id"]
        # Simulate controller loss + unit completion while it was away.
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"})
        result = self.controller.reconcile(job)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["receipt"]["outcome"], "passed")

    def test_reconcile_inconclusive_without_record(self):
        job = self.controller.launch(["/bin/true"], request_id="rec2",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.units[linux_jobs.unit_name(spec.job_id)]["collected"] = True
        result = self.controller.reconcile(job)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["receipt"]["outcome"], "inconclusive")

    def test_logs_pass_through(self):
        job = self.controller.launch(["/bin/true"], request_id="log",
                                     operation_id="op")["job_id"]
        logs = self.controller.logs(job)
        self.assertEqual(logs["next_cursor"], "c1")
        self.assertEqual(logs["entries"][0]["message"], "line one")


class SessionAndLifecycleTests(unittest.TestCase):
    """Regression tests for the review fixes: session identity, truthful
    classification, inconclusive finalization, concurrent duplicates."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        session_id="session-A",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_two_controllers_same_session_share_jobs(self):
        started = self.controller.launch(["/bin/true"], request_id="s1",
                                         operation_id="op")
        other = JobController(self.root / "store.sqlite",
                              evidence_root=self.root / "evidence",
                              session_id="session-A", backend=self.backend)
        try:
            receipt = other.status(started["job_id"])
            self.assertEqual(receipt["job_id"], started["job_id"])
            # ...and can wait/cancel/reconcile it after "restart".
            self.backend.set_terminal(
                self.controller._spec_for(self.controller.store.get_job(started["job_id"])),
                {"service_result": "success", "exit_code": "exited",
                 "exit_status": "0"})
            result = other.wait(started["job_id"], max_wait_seconds=5)
            self.assertFalse(result["elapsed"])
            self.assertEqual(result["receipt"]["outcome"], "passed")
        finally:
            other.close()

    def test_success_with_failed_cleanup_is_never_passed(self):
        job = self.controller.launch(["/bin/true"], request_id="c1",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"},
                                  cgroup="failed")
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "inconclusive")
        self.assertIsNotNone(receipt["inconclusive_reason"])

    def test_success_with_unknown_cleanup_is_incomplete_evidence(self):
        job = self.controller.launch(["/bin/true"], request_id="c2",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"},
                                  cgroup="unknown")
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "inconclusive")

    def test_collected_missing_record_finalizes_inconclusive(self):
        job = self.controller.launch(["/bin/true"], request_id="g1",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.units[linux_jobs.unit_name(spec.job_id)]["collected"] = True
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "inconclusive")
        row = self.controller.store.get_job(job)
        self.assertEqual(row["state"], "completed")
        self.assertIsNotNone(row["terminal_wall"])
        # Idempotent: repeated status/reconcile stays terminal, never relaunches.
        again = self.controller.status(job)
        self.assertEqual(again["outcome"], "inconclusive")
        self.assertEqual(len(self.backend.launch_calls), 1)

    def test_collected_corrupt_record_finalizes_inconclusive(self):
        job = self.controller.launch(["/bin/true"], request_id="g2",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"corrupt": True})
        self.backend.units[linux_jobs.unit_name(spec.job_id)]["collected"] = True
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "inconclusive")
        self.assertIn("corrupt", receipt["inconclusive_reason"])

    def test_temporary_manager_failure_never_finalizes(self):
        job = self.controller.launch(["/bin/true"], request_id="m1",
                                     operation_id="op")["job_id"]
        self.backend.manager_down = True
        result = self.controller.status(job)
        self.assertEqual(result["phase"], "unknown")
        row = self.controller.store.get_job(job)
        self.assertNotEqual(row["state"], "completed")
        # Recovery: manager back, job completes normally.
        self.backend.manager_down = False
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited", "exit_status": "0"})
        receipt = self.controller.status(job)
        self.assertEqual(receipt["outcome"], "passed")

class UnlaunchedRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        session_id="session-A",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_reconcile_unlaunched_row_finalizes_blocked(self):
        from agent_exec.store import Store
        store = Store(self.root / "store.sqlite")
        job_id = store.create_job(session_id="session-A", request_id="ghost",
                                  operation_id="op", profile_id=None,
                                  purpose="verify", unit="(unlaunched)",
                                  job_dir=self.root / "ghost",
                                  deadline_seconds=10, stop_grace_seconds=2)
        store.close()
        result = self.controller.reconcile(job_id)
        self.assertTrue(result["reconciled"])
        receipt = result["receipt"]
        self.assertEqual(receipt["outcome"], "blocked")
        self.assertIsNotNone(receipt["inconclusive_reason"])

    def test_concurrent_duplicate_requests_produce_one_job(self):
        import threading
        results = []
        barrier = threading.Barrier(4)

        def launch():
            barrier.wait()
            results.append(self.controller.launch(["/bin/true"], request_id="race",
                                                  operation_id="op"))

        threads = [threading.Thread(target=launch) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = {r["job_id"] for r in results}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.backend.launch_calls), 1)
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 1)

    def test_launch_env_never_persisted_to_journal(self):
        self.controller.launch(["/bin/true"], request_id="env",
                               operation_id="op",
                               env={"SECRET_TOKEN": "super-secret-value"})
        row = self.controller.store.get_job(
            self.controller.store.find_by_request("session-A", "env")["job_id"])
        self.assertNotIn("super-secret-value", row["launch_argv_json"] or "")


class RealNativeTest(unittest.TestCase):
    """Actual systemd proof; opt-in via XTX_EXEC_REAL=1 (bounded, ~30 s)."""

    def setUp(self):
        if __import__("os").environ.get("XTX_EXEC_REAL") != "1":
            self.skipTest("native systemd proof is opt-in (XTX_EXEC_REAL=1)")

    def test_true_passes_and_deadline_times_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                       session_id="native-session")
            try:
                ok = controller.launch(["/usr/bin/true"], request_id="native-ok",
                                       operation_id="native")
                receipt = controller.wait(ok["job_id"], max_wait_seconds=15).get("receipt") \
                    or controller.status(ok["job_id"])
                self.assertEqual(receipt["outcome"], "passed")
                slow = controller.launch(["/usr/bin/sleep", "300"],
                                         request_id="native-slow",
                                         operation_id="native",
                                         deadline_seconds=2, stop_grace_seconds=2)
                receipt = controller.wait(slow["job_id"], max_wait_seconds=30).get("receipt") \
                    or controller.status(slow["job_id"])
                self.assertEqual(receipt["outcome"], "timed_out")
                self.assertEqual(receipt["cleanup"], "verified_empty")
            finally:
                controller.close()

    def test_cross_invocation_native_flow(self):
        """Launch in one controller; wait+status from a fresh one, same session."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                  session_id="native-shared")
            try:
                started = first.launch(["/usr/bin/true"], request_id="xinv",
                                       operation_id="native")
            finally:
                first.close()
            second = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                   session_id="native-shared")
            try:
                receipt = second.wait(started["job_id"], max_wait_seconds=15).get("receipt") \
                    or second.status(started["job_id"])
                self.assertEqual(receipt["outcome"], "passed")
                self.assertEqual(receipt["cleanup"], "verified_empty")
            finally:
                second.close()

    def test_descendant_cgroup_population_blocks_cleanup_verification(self):
        """Regression (review P1-1): a unit whose own cgroup.procs is EMPTY
        but whose CHILD cgroup holds a process must NOT be verified_empty.
        The old cgroup.procs-only check passed exactly this case."""
        import shutil
        import subprocess as sp
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                       session_id="native-cgroup")
            try:
                started = controller.launch(["/bin/sleep", "60"],
                                            request_id="cg", operation_id="native",
                                            deadline_seconds=120,
                                            stop_grace_seconds=30)
                job_id = started["job_id"]
                unit = linux_jobs.unit_name(job_id)
                unit_cgroup = Path("/sys/fs/cgroup/system.slice", unit)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if unit_cgroup.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(unit_cgroup.exists(), "unit cgroup never appeared")
                # Active job => populated => failed, sanity.
                self.assertEqual(linux_jobs.cgroup_state(unit), "failed")
                # Reproduce the bug shape: move the MAIN process into a child
                # cgroup so the unit's own cgroup.procs becomes empty while a
                # descendant stays populated.
                main_pid = (unit_cgroup / "cgroup.procs").read_text().split()[0]
                child = unit_cgroup / "child"
                child.mkdir(exist_ok=True)
                (child / "cgroup.procs").write_text(main_pid)
                self.assertEqual((unit_cgroup / "cgroup.procs").read_text().split(),
                                 [], "precondition: unit-level procs now empty")
                # THE regression assertion: cgroup.events must see the child.
                self.assertEqual(linux_jobs.cgroup_state(unit), "failed",
                                 "populated descendant must fail verified_empty")
                # Stop the unit: systemd's control-group sweep must remove the
                # child too, after which cleanup verifies empty.
                controller.cancel(job_id, reason="test teardown")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if linux_jobs.cgroup_state(unit) == "verified_empty":
                        break
                    time.sleep(0.2)
                self.assertEqual(linux_jobs.cgroup_state(unit), "verified_empty")
                shutil.rmtree(child, ignore_errors=True)
            finally:
                controller.close()

    def test_missing_exit_record_finalizes_inconclusive_on_real_units(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                       session_id="native-session")
            try:
                ok = controller.launch(["/usr/bin/true"], request_id="native-ok2",
                                       operation_id="native")
                job_dir = Path(controller.store.get_job(ok["job_id"])["job_dir"])
                record = job_dir / "exit-record.json"
                # Poll the FILESYSTEM for completion; any controller status()
                # call would absorb the receipt before we can remove evidence.
                import time as _time
                deadline = _time.monotonic() + 15
                while _time.monotonic() < deadline:
                    if record.is_file():
                        break
                    _time.sleep(0.2)
                self.assertTrue(record.is_file())
                record.unlink()
                revived = JobController(root / "s.sqlite", evidence_root=root / "ev",
                                        session_id="native-session")
                try:
                    receipt = revived.status(ok["job_id"])
                    self.assertEqual(receipt["outcome"], "inconclusive")
                    self.assertIsNotNone(receipt["inconclusive_reason"])
                    # Idempotent: a second reconcile stays terminal.
                    again = revived.reconcile(ok["job_id"])
                    self.assertEqual(again["receipt"]["outcome"], "inconclusive")
                finally:
                    revived.close()
            finally:
                controller.close()


class LogPaginationTests(unittest.TestCase):
    """P1#8: bounded reads must not silently skip entries (cursor advances
    only past accepted entries). Uses the real read_logs against a fake
    journalctl -- replace _run to feed a canned stream."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.spec = linux_jobs.JobSpec(job_id="logp", argv=["/bin/true"],
                                       job_dir=Path(self.tmp.name))
        self.original_run = linux_jobs._run

    def tearDown(self):
        linux_jobs._run = self.original_run
        self.tmp.cleanup()

    def fake_journal(self, lines: str):
        class Out:
            returncode = 0
            stdout = lines
            stderr = ""
        linux_jobs._run = lambda argv, timeout=30: Out()

    def test_budget_exhaustion_stops_before_the_excluded_entry(self):
        e1 = json.dumps({"__CURSOR": "c1", "MESSAGE": "one",
                         "__MONOTONIC_TIMESTAMP": "1"})
        e2 = json.dumps({"__CURSOR": "c2", "MESSAGE": "two" * 40,
                         "__MONOTONIC_TIMESTAMP": "2"})
        e3 = json.dumps({"__CURSOR": "c3", "MESSAGE": "three",
                         "__MONOTONIC_TIMESTAMP": "3"})
        self.fake_journal(e1 + "\n" + e2 + "\n" + e3 + "\n")
        result = linux_jobs.read_logs(self.spec, max_bytes=250)
        cursors = [e["cursor"] for e in result["entries"]]
        self.assertEqual(cursors, ["c1"],
                         "entry 2 excluded by budget => cursor stays at c1")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["oversized_entries"], 0)
        # Sequential re-read with a bigger budget reconstructs in order.
        full = linux_jobs.read_logs(self.spec, max_bytes=100_000)
        self.assertEqual([e["cursor"] for e in full["entries"]], ["c1", "c2", "c3"])

    def test_oversized_entry_is_flagged_and_advanced(self):
        e1 = json.dumps({"__CURSOR": "c1", "MESSAGE": "one",
                         "__MONOTONIC_TIMESTAMP": "1"})
        e2 = json.dumps({"__CURSOR": "c2", "MESSAGE": "two" * 400,
                         "__MONOTONIC_TIMESTAMP": "2"})
        e3 = json.dumps({"__CURSOR": "c3", "MESSAGE": "three",
                         "__MONOTONIC_TIMESTAMP": "3"})
        self.fake_journal(e1 + "\n" + e2 + "\n" + e3 + "\n")
        result = linux_jobs.read_logs(self.spec, max_bytes=200)
        self.assertEqual(result["oversized_entries"], 1)
        self.assertFalse(result["truncated"])
        # c3 is reached only after c2 was explicitly accounted (marker entry).
        self.assertEqual([e["cursor"] for e in result["entries"]],
                         ["c1", "c2", "c3"])
        self.assertTrue(any("oversized" in e["message"] for e in result["entries"]))

    def test_window_cap_sets_explicit_gap(self):
        # Regression: journalctl -n 512 returns only the newest entries, so a
        # full window must surface an explicit gap instead of "gap": None
        # (entries older than the window were silently unread).
        lines = [json.dumps({"__CURSOR": f"c{index}", "MESSAGE": f"m{index}",
                             "__MONOTONIC_TIMESTAMP": str(index)})
                 for index in range(linux_jobs.JOURNAL_ENTRY_CAP + 1)]
        self.fake_journal("\n".join(lines) + "\n")
        result = linux_jobs.read_logs(self.spec, max_bytes=10_000_000)
        self.assertIsNotNone(result["gap"])
        self.assertIn("cap", result["gap"])
        self.assertIn(str(linux_jobs.JOURNAL_ENTRY_CAP), result["gap"])

    def test_window_below_cap_has_no_gap(self):
        lines = [json.dumps({"__CURSOR": f"c{index}", "MESSAGE": f"m{index}",
                             "__MONOTONIC_TIMESTAMP": str(index)})
                 for index in range(5)]
        self.fake_journal("\n".join(lines) + "\n")
        result = linux_jobs.read_logs(self.spec, max_bytes=10_000_000)
        self.assertEqual(len(result["entries"]), 5)
        self.assertIsNone(result["gap"])

class AdoptionHookBackend(FakeBackend):
    """FakeBackend that can run a hook inside launch() just before the
    simulated failure: used to land a concurrent adoption between the row
    reservation and the launch exception."""

    def __init__(self):
        super().__init__()
        self.pre_fail_hook = None

    def launch(self, spec):
        if self.fail_next_launch and self.pre_fail_hook is not None:
            hook, self.pre_fail_hook = self.pre_fail_hook, None
            hook()
        return super().launch(spec)


class ForcedInconclusiveBackend(FakeBackend):
    """inspect always reports an inconclusive observation whose unit has NOT
    definitively ended (ended=False). A phase-"inconclusive" observation
    normally implies ended=True (absorbed by an earlier reconcile branch),
    so this is the only way to drive the cancel_requested inconclusive
    branch. `before_inspect` optionally runs just before observing,
    simulating a concurrent writer between reconcile's top-of-function
    terminal check and the branch itself."""

    def __init__(self):
        super().__init__()
        self.before_inspect = None

    def inspect(self, spec):
        if self.before_inspect is not None:
            hook, self.before_inspect = self.before_inspect, None
            hook()
        observation = super().inspect(spec)
        observation["phase"] = "inconclusive"
        observation["ended"] = False
        observation["warning"] = "forced inconclusive observation (branch test)"
        return observation


class LaunchExceptionDisciplineTests(unittest.TestCase):
    """Review P2: a backend.launch exception must mirror the claimed=False
    discipline — never finalize a row another writer adopted, stop an
    orphaned unit it created, and never revert a running row to blocked."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = AdoptionHookBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        session_id="session-A",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_case_a_fail_before_creation_blocks_without_stop(self):
        self.backend.fail_next_launch = True
        with self.assertRaises(JobError):
            self.controller.launch(["/bin/true"], request_id="pre",
                                   operation_id="op")
        row = self.controller.store.find_by_request("session-A", "pre")
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["outcome"], "blocked")
        self.assertEqual(self.backend.stop_calls, [])

    def test_case_b_fail_after_creation_blocks_and_stops_orphan(self):
        self.backend.fail_after_create = True
        with self.assertRaises(JobError):
            self.controller.launch(["/bin/true"], request_id="orphan",
                                   operation_id="op")
        row = self.controller.store.find_by_request("session-A", "orphan")
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["outcome"], "blocked")
        self.assertIn(linux_jobs.unit_name(row["job_id"]),
                      self.backend.stop_calls,
                      "the unowned unit created before the failure must be stopped")

    def test_case_c_adopted_row_is_never_finalized_by_launch_error(self):
        holder = {}

        def adopt_during_launch():
            row = self.controller.store.find_by_request("session-A", "c3")
            holder["job_id"] = row["job_id"]
            self.controller.store.update_job(
                row["job_id"], unit=linux_jobs.unit_name(row["job_id"]),
                state="running")

        self.backend.fail_next_launch = True
        self.backend.pre_fail_hook = adopt_during_launch
        with self.assertRaises(JobError):
            self.controller.launch(["/bin/true"], request_id="c3",
                                   operation_id="op")
        row = self.controller.store.get_job(holder["job_id"])
        self.assertEqual(row["state"], "running",
                         "an adopted row must not be finalized by the launch error")
        self.assertIsNone(row["outcome"])
        self.assertTrue(row["unit"].endswith(".service"))
        kinds = [event["kind"]
                 for event in self.controller.store.events_after(holder["job_id"])]
        self.assertIn("launch_error_after_adoption", kinds)
        self.assertNotIn("terminal", kinds)
        self.assertEqual(self.backend.stop_calls, [])


class ReconcileCancelCasTests(unittest.TestCase):
    """Review P2: the cancel_requested inconclusive finalize in reconcile()
    must be a CAS — an already-terminal row keeps its existing receipt."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        session_id="session-A",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_reconcile_after_cancelled_collection_is_idempotent(self):
        job = self.controller.launch(["/bin/true"], request_id="crc",
                                     operation_id="op")["job_id"]
        self.controller.cancel(job, reason="test")
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        unit = linux_jobs.unit_name(spec.job_id)
        # Collection lost the exit record: inconclusive evidence only.
        self.backend.units[unit].update({"collected": True, "record": None})
        first = self.controller.reconcile(job)
        self.assertTrue(first["reconciled"])
        self.assertEqual(first["receipt"]["outcome"], "inconclusive")
        second = self.controller.reconcile(job)
        self.assertEqual(second["receipt"], first["receipt"],
                         "a second reconcile must not overwrite the receipt")
        self.assertFalse(second["reconciled"])

    def test_cancel_inconclusive_branch_uses_cas(self):
        backend = ForcedInconclusiveBackend()
        controller = JobController(self.root / "store-b1.sqlite",
                                   evidence_root=self.root / "evidence-b1",
                                   session_id="session-A", backend=backend)
        try:
            job = controller.launch(["/bin/true"], request_id="cb1",
                                    operation_id="op")["job_id"]
            controller.cancel(job, reason="test")
            result = controller.reconcile(job)
            self.assertTrue(result["reconciled"])
            self.assertEqual(result["receipt"]["outcome"], "inconclusive")
            row = controller.store.get_job(job)
            self.assertEqual(row["state"], "completed")
        finally:
            controller.close()

    def test_cancel_inconclusive_branch_never_overwrites_terminal(self):
        backend = ForcedInconclusiveBackend()
        controller = JobController(self.root / "store-b2.sqlite",
                                   evidence_root=self.root / "evidence-b2",
                                   session_id="session-A", backend=backend)
        try:
            job = controller.launch(["/bin/true"], request_id="cb2",
                                    operation_id="op")["job_id"]
            controller.cancel(job, reason="test")
            concurrent = {"wall": time.time()}

            def concurrent_writer():
                concurrent["wall"] = time.time()
                controller.store.update_job(job, state="completed",
                                            outcome="cancelled",
                                            terminal_wall=concurrent["wall"])

            backend.before_inspect = concurrent_writer
            result = controller.reconcile(job)
            self.assertTrue(result["reconciled"])
            self.assertEqual(result["receipt"]["outcome"], "cancelled",
                             "the concurrent terminal receipt must stand")
            self.assertEqual(result["receipt"]["terminal_wall"],
                             concurrent["wall"])
        finally:
            controller.close()


class CgroupStateTests(unittest.TestCase):
    """Review P2: unreadable cgroup evidence must be "unknown", never a
    quiet "verified_empty" (Path.exists() is False on EACCES too)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.original_root = linux_jobs.CGROUP_ROOT
        linux_jobs.CGROUP_ROOT = Path(self.tmp.name)

    def tearDown(self):
        linux_jobs.CGROUP_ROOT = self.original_root
        self.tmp.cleanup()

    def test_absent_unit_is_verified_empty(self):
        self.assertEqual(linux_jobs.cgroup_state("xtx-exec-absent.service"),
                         "verified_empty")

    def test_present_dir_without_events_is_unknown(self):
        unit = "xtx-exec-noevents.service"
        (linux_jobs.CGROUP_ROOT / unit).mkdir(parents=True)
        self.assertEqual(linux_jobs.cgroup_state(unit), "unknown")

    def test_populated_zero_with_empty_procs_is_verified_empty(self):
        unit = "xtx-exec-clean.service"
        cgroup = linux_jobs.CGROUP_ROOT / unit
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.events").write_text("populated 0\n")
        (cgroup / "cgroup.procs").write_text("")
        self.assertEqual(linux_jobs.cgroup_state(unit), "verified_empty")

    def test_populated_one_is_failed(self):
        unit = "xtx-exec-busy.service"
        cgroup = linux_jobs.CGROUP_ROOT / unit
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.events").write_text("populated 1\n")
        self.assertEqual(linux_jobs.cgroup_state(unit), "failed")

    def test_unreadable_path_is_unknown_not_verified_empty(self):
        # Regression shape for the EACCES bug: when the unit path cannot be
        # stat-ed (here ENOTDIR because the cgroup root is a file), the old
        # exists()-check returned verified_empty; it must be unknown.
        fake_root = linux_jobs.CGROUP_ROOT / "root-file"
        fake_root.write_text("not a directory")
        linux_jobs.CGROUP_ROOT = fake_root
        self.assertEqual(linux_jobs.cgroup_state("xtx-exec-eacces.service"),
                         "unknown")


if __name__ == "__main__":
    unittest.main()
