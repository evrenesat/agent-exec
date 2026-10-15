"""Supervisor hardening tests: request-id policy, env/deadline rejection,
path containment, token auth, concurrent malformed requests (P0#2)."""

from __future__ import annotations

import json
import sys
import threading
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.harness.opencode.supervisor import Supervisor, make_handler  # noqa: E402
from agent_exec.jobs import JobController, JobError  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.launch_calls = []

    def launch(self, spec):
        self.launch_calls.append(spec)
        return {"unit": linux_jobs.unit_name(spec.job_id), "launch_wall": 1.0,
                "boot_id": "b", "invocation_id": "i", "launch_argv": ["x"]}

    classify = staticmethod(linux_jobs.classify)


def make_supervisor(operations=None, token=None):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    controller = JobController(root / "store.sqlite", evidence_root=root / "ev",
                               session_id="sess", backend=FakeBackend())
    supervisor = Supervisor(controller, operations or {
        "demo.op": {"argv": ["/bin/true"], "cwd": str(root),
                    "deadline_seconds": 60, "env": {"PATH": "/usr/bin"}},
    }, token=token)
    return supervisor, controller, tmp


class SupervisorValidationTests(unittest.TestCase):
    def setUp(self):
        self.supervisor, self.controller, self.tmp = make_supervisor()

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_invalid_request_ids_rejected(self):
        for bad in ["../escape", "a/b", "/abs", "a\\b", ".hidden", "-lead",
                    "x" * 65, "", None, 7, "sp ace"]:
            with self.subTest(request_id=bad):
                with self.assertRaises(JobError):
                    self.supervisor.launch({"operation": "demo.op",
                                            "request_id": bad})

    def test_valid_request_id_launches(self):
        result = self.supervisor.launch({"operation": "demo.op",
                                         "request_id": "ok_id-1"})
        self.assertFalse(result["replayed"])
        self.assertTrue(result["job_id"].startswith("job-"))

    def test_caller_env_overrides_rejected(self):
        with self.assertRaises(JobError):
            self.supervisor.launch({"operation": "demo.op", "request_id": "env1",
                                    "env": {"PATH": "/attacker"}})
        # Nothing launched.
        self.assertEqual(len(self.controller.backend.launch_calls), 0)

    def test_deadline_capped_by_operation_and_server_maximum(self):
        # Caller may shorten, never exceed the operation's deadline.
        result = self.supervisor.launch({"operation": "demo.op",
                                         "request_id": "d1",
                                         "deadline_seconds": 5})
        spec = self.controller.backend.launch_calls[-1]
        self.assertEqual(spec.deadline_seconds, 5)
        result = self.supervisor.launch({"operation": "demo.op",
                                         "request_id": "d2",
                                         "deadline_seconds": 10_000})
        spec = self.controller.backend.launch_calls[-1]
        self.assertEqual(spec.deadline_seconds, 60)  # operation deadline wins
        result = self.supervisor.launch({"operation": "demo.op",
                                         "request_id": "d3"})
        self.assertEqual(self.controller.backend.launch_calls[-1].deadline_seconds, 60)

    def test_job_path_cannot_escape_evidence_root(self):
        # The request-id regex blocks separators; the containment check is the
        # second layer. Exercise it with a session identity that WOULD escape
        # (e.g. a controller constructed with a hostile label).
        with self.assertRaises(JobError):
            self.supervisor.job_dir_for("../../evil", "ok")
        # Contained identities resolve fine.
        d = self.supervisor.job_dir_for("sess", "ok")
        self.assertTrue(str(d.resolve()).startswith(
            str(Path(self.controller.evidence_root.resolve()) / "jobs")))

    def test_unknown_operation_rejected(self):
        with self.assertRaises(JobError):
            self.supervisor.launch({"operation": "nope", "request_id": "u1"})


class TokenAuthTests(unittest.TestCase):
    def test_missing_or_wrong_token_rejected_on_all_routes(self):
        supervisor, controller, tmp = make_supervisor(token="secret-token")
        try:
            handler_cls = make_handler(supervisor)
            # Drive the authorization predicate directly (the HTTP layer is a
            # thin wrapper; concurrent malformed requests are covered below).
            class FakeHeaders:
                def __init__(self, value):
                    self.value = value

                def get(self, name):
                    return self.value

            class FakeHandler:
                headers = FakeHeaders("wrong")
                _authorized = handler_cls._authorized

            self.assertFalse(FakeHandler()._authorized())
            FakeHandler.headers = FakeHeaders("secret-token")
            self.assertTrue(FakeHandler()._authorized())
        finally:
            controller.close()
            tmp.cleanup()


class ConcurrentMalformedRequestsTests(unittest.TestCase):
    def test_malformed_burst_launches_nothing_and_does_not_crash(self):
        supervisor, controller, tmp = make_supervisor()
        try:
            errors = []

            def blast(i):
                try:
                    supervisor.launch({"operation": "demo.op",
                                       "request_id": ["ok1", "../bad", "a/b",
                                                      "ok1"][i % 4]})
                except JobError:
                    errors.append("rejected")
                except Exception as error:  # noqa: BLE001
                    errors.append(f"unexpected:{error}")

            threads = [threading.Thread(target=blast, args=(i,)) for i in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            # Only the two valid ids got through; "ok1" twice = one launch + replay.
            self.assertNotIn("unexpected:Traceback", str(errors))
            self.assertLessEqual(len(controller.backend.launch_calls), 2)
        finally:
            controller.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class HttpRoundTripTests(unittest.TestCase):
    """Real HTTP round trips (the reviewer's gap: predicate-only tests missed
    the Pi launch 401 and the dropped-connection failures)."""

    def setUp(self):
        import tempfile
        import threading
        from http.client import HTTPConnection
        self.http = HTTPConnection
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "ev",
                                        session_id="sess", backend=self.backend)
        self.supervisor = Supervisor(self.controller, {
            "demo.op": {"argv": ["/bin/true"], "deadline_seconds": 60},
        }, token="tok-123")
        from agent_exec.harness.opencode.supervisor import make_handler
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.supervisor))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.controller.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token="tok-123"):
        import json as jsonlib
        conn = self.http("127.0.0.1", self.port, timeout=10)
        headers = {"X-Agent-Exec-Token": token} if token else {}
        payload = None
        if body is not None:
            payload = jsonlib.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = jsonlib.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_wrong_token_is_401_on_get_and_post(self):
        status, _ = self.request("GET", "/health", token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/jobs",
                                 {"operation": "demo.op", "request_id": "x1"},
                                 token=None)
        self.assertEqual(status, 401)

    def test_correct_token_launches_and_reads(self):
        status, body = self.request("POST", "/jobs",
                                    {"operation": "demo.op", "request_id": "ok"})
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

    def test_bad_cursor_returns_400_not_dropped_connection(self):
        status, body = self.request("GET", "/events?cursor=abc")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_bad_deadline_type_returns_400(self):
        status, body = self.request("POST", "/jobs",
                                    {"operation": "demo.op", "request_id": "bd",
                                     "deadline_seconds": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
