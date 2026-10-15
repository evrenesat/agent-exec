"""P0-1 integration: a launch paused mid-flight must survive concurrent
reconciliation; the lease protects it and adoption adopts the real unit.
Runs the supervisor over real HTTP (POST /jobs + GET /events race)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.harness.opencode.supervisor import Supervisor, make_handler  # noqa: E402
from agent_exec.jobs import JobController  # noqa: E402


class GatedBackend:
    """Fake backend: `release` gates systemd-run itself; `post_launch` gates
    the return (simulating the crash window between a successful unit start
    and the journal recording the unit identity)."""

    def __init__(self):
        self.release = threading.Event()
        self.post_launch = threading.Event()
        self.post_launch.set()
        self.launched: list[str] = []
        self.units: dict[str, dict] = {}

    def launch(self, spec):
        self.release.wait(timeout=30)
        unit = linux_jobs.unit_name(spec.job_id)
        self.launched.append(unit)
        self.units[unit] = {"phase": "running", "record": None,
                             "cgroup": "failed", "collected": False}
        self.post_launch.wait(timeout=30)
        return {"unit": unit, "launch_wall": time.time(), "boot_id": "b",
                "invocation_id": "i", "launch_argv": ["x"]}

    def set_terminal(self, spec, record, cgroup="verified_empty"):
        unit = linux_jobs.unit_name(spec.job_id)
        self.units[unit].update({"phase": "terminal", "record": record,
                                 "cgroup": cgroup, "collected": True})

    def inspect(self, spec):
        unit = linux_jobs.unit_name(spec.job_id)
        state = self.units.get(unit)
        if state is None:
            return {"unit": unit, "load_state": "not-found",
                    "active_state": None, "sub_state": None, "result": None,
                    "exec_main_code": None, "exec_main_status": None,
                    "invocation_id": None, "exit_record": None,
                    "cgroup": "unknown", "observed_wall": 2.0,
                    "phase": "inconclusive", "ended": True,
                    "warning": "no usable exit evidence"}
        if state["collected"]:
            phase = "terminal"
        else:
            phase = "running"
        return {"unit": unit, "load_state": "loaded" if not state["collected"]
                else "not-found", "active_state": "active",
                "sub_state": None, "result": None, "exec_main_code": None,
                "exec_main_status": None, "invocation_id": None,
                "exit_record": state["record"], "cgroup": state["cgroup"],
                "observed_wall": 2.0, "phase": phase, "ended": state["collected"],
                "warning": None}

    classify = staticmethod(linux_jobs.classify)

    def stop(self, spec, kill=False):
        unit = linux_jobs.unit_name(spec.job_id)
        self.units.get(unit, {}).update({"collected": True, "record": {
            "service_result": "signal", "exit_code": "killed",
            "exit_status": "TERM"}, "cgroup": "verified_empty"})
        return {"requested": True, "rc": 0}

    def read_logs(self, spec, after_cursor=None, max_bytes=32768):
        return {"entries": [], "next_cursor": after_cursor, "bytes": 0,
                "truncated": False, "gap": None}

    def reconcile(self, spec, cancel_requested=False):
        observation = self.inspect(spec)
        return {"observation": observation,
                "verdict": self.classify(observation,
                                         cancel_requested=cancel_requested)}


class LaunchRaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = GatedBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "ev",
                                        session_id="sess", backend=self.backend)
        self.supervisor = Supervisor(self.controller, {
            "demo.op": {"argv": ["/bin/true"], "deadline_seconds": 60},
        }, token="tok")
        from agent_exec.harness.opencode.supervisor import make_handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.supervisor))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.controller.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=15)
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"X-Agent-Exec-Token": "tok",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_events_during_paused_launch_does_not_finalize_blocked(self):
        # Thread A: launch (paused inside systemd-run).
        launch_result = {}
        def do_launch():
            status, body = self.call("POST", "/jobs",
                                     {"operation": "demo.op",
                                      "request_id": "race1"})
            launch_result.update({"status": status, "body": body})
        thread = threading.Thread(target=do_launch)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.controller.store.find_by_request("sess", "race1")
            if row:
                break
            time.sleep(0.02)
        self.assertIsNotNone(row, "reservation must be visible while paused")
        # Thread B: GET /events triggers reconcile_all mid-launch.
        status, events = self.call("GET", "/events?cursor=0")
        self.assertEqual(status, 200)
        # No terminal/blocked event may exist for the racing job.
        blocked = [e for e in events.get("events", [])
                   if e.get("job_id") == row["job_id"] and e.get("terminal")]
        self.assertEqual(blocked, [],
                         "a paused in-flight launch must never be finalized")
        # Release the launch; the job must become running and completable.
        self.backend.release.set()
        thread.join(timeout=30)
        self.assertEqual(launch_result.get("status"), 200)
        job_id = launch_result["body"]["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job_id))
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited",
                                         "exit_status": "0"})
        status, receipt = self.call("GET", f"/jobs/{job_id}")
        self.assertEqual(receipt.get("outcome"), "passed")
        self.assertEqual(receipt.get("cleanup"), "verified_empty")

    def post_async(self, request_id):
        result = {}

        def run():
            try:
                result["response"] = self.call("POST", "/jobs",
                                               {"operation": "demo.op",
                                                "request_id": request_id})
            except Exception as error:  # noqa: BLE001
                result["error"] = error

        thread = threading.Thread(target=run)
        thread.start()
        return thread, result

    def wait_for_row(self, request_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.controller.store.find_by_request("sess", request_id)
            if row:
                return row
            time.sleep(0.02)
        raise AssertionError(f"reservation never appeared: {request_id}")

    def test_adopted_collected_unit_after_lease_expiry(self):
        # True crash window: systemd-run succeeded (unit exists, job running)
        # but the controller died before storing the unit identity. The row
        # stays "(unlaunched)"; lease expiry + deterministic unit inspection
        # must ADOPT the unit (never block it), and the late launch-claim
        # must not stop the adopted unit.
        self.backend.post_launch.clear()  # hold the launch return
        thread, _result = self.post_async("adopt1")
        row = self.wait_for_row("adopt1")
        job_id = row["job_id"]
        self.backend.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.backend.launched:
                break
            time.sleep(0.02)
        # Crash simulation: unit exists and runs, journal row still "(unlaunched)".
        self.controller.store.update_job(job_id, launch_lease_until=time.time() - 1)
        adopted = self.controller.reconcile(job_id)
        self.assertEqual(adopted.get("reason"), "adopted launched unit; still running")
        row_now = self.controller.store.get_job(job_id)
        self.assertEqual(row_now["state"], "running")
        self.assertTrue(row_now["unit"].endswith(".service"),
                        "adoption must store the deterministic unit identity")
        # The late launch return loses its CAS claim but must NOT stop the
        # adopted unit and must not error the caller.
        self.backend.post_launch.set()
        thread.join(timeout=30)
        self.assertIsNone(_result.get("error"))
        self.assertEqual(_result["response"][0], 200)
        self.assertEqual(_result["response"][1].get("replayed"), True)
        # The adopted job is still fully usable.
        spec = self.controller._spec_for(row_now)
        self.backend.set_terminal(spec, {"service_result": "success",
                                         "exit_code": "exited",
                                         "exit_status": "0"})
        receipt = self.controller.status(job_id)
        self.assertEqual(receipt["outcome"], "passed")
        self.assertEqual(receipt["cleanup"], "verified_empty")

    def test_abandoned_reservation_blocks_only_after_lease(self):
        # A reservation whose launch never happens: blocked only after lease.
        thread, _result = self.post_async("gone1")
        row = self.wait_for_row("gone1")
        job_id = row["job_id"]
        # While the lease is active: reconcile must NOT finalize.
        early = self.controller.reconcile(job_id)
        self.assertEqual(early.get("reconciled"), False,
                         "in-lease reservations are never finalized")
        # Expire the lease; the unit never appears => blocked.
        self.controller.store.update_job(job_id, launch_lease_until=time.time() - 1)
        result2 = self.controller.reconcile(job_id)
        self.assertTrue(result2["reconciled"])
        self.assertEqual(result2["receipt"]["outcome"], "blocked")
        # Release the (now pointless) gate so the server thread can exit.
        self.backend.release.set()
        thread.join(timeout=30)
        # The late launch failed its CAS claim: blocked must stand.
        self.assertEqual(self.controller.store.get_job(job_id)["outcome"], "blocked")


if __name__ == "__main__":
    unittest.main()
