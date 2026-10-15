"""Controller tests against a fake backend (mocked state, no real units).

The real Linux proof lives in `cli.py selftest --backend linux`, which the
plan's verification command runs; these tests pin the controller contract
without touching systemd.
"""

from __future__ import annotations

import sys
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
        self.fail_next_launch = False
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
        return {"unit": unit, "launch_wall": 1.0, "boot_id": "fake-boot",
                "invocation_id": "fake-invocation", "launch_argv": ["x"]}

    def set_terminal(self, spec, record, cgroup="verified_empty"):
        unit = linux_jobs.unit_name(spec.job_id)
        self.units[unit].update({"phase": "terminal", "record": record,
                                 "cgroup": cgroup, "collected": True})

    def inspect(self, spec):
        state = self.units[linux_jobs.unit_name(spec.job_id)]
        return {"unit": linux_jobs.unit_name(spec.job_id),
                "load_state": "not-found" if state["collected"] else "loaded",
                "active_state": None if state["collected"] else "active",
                "sub_state": None, "result": None, "exec_main_code": None,
                "exec_main_status": None, "invocation_id": None,
                "exit_record": state["record"],
                "cgroup": state["cgroup"], "observed_wall": 2.0,
                "phase": "terminal" if state["collected"] else "running",
                "warning": ("unit collected without an exit record; result unknown "
                            "(never success)")
                if state["collected"] and state["record"] is None else None}

    classify = staticmethod(linux_jobs.classify)

    def stop(self, spec, kill=False):
        unit = linux_jobs.unit_name(spec.job_id)
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


class RealNativeTest(unittest.TestCase):
    """Actual systemd proof; opt-in via XTX_EXEC_REAL=1 (bounded, ~10 s)."""

    def setUp(self):
        if __import__("os").environ.get("XTX_EXEC_REAL") != "1":
            self.skipTest("native systemd proof is opt-in (XTX_EXEC_REAL=1)")

    def test_true_passes_and_deadline_times_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = JobController(root / "s.sqlite", evidence_root=root / "ev")
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


if __name__ == "__main__":
    unittest.main()
