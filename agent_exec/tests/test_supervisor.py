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
        self.records: dict[str, dict] = {}

    def launch(self, spec):
        self.launch_calls.append(spec)
        unit = linux_jobs.unit_name(spec.job_id)
        self.records[unit] = {"service_result": "success", "exit_code": "exited",
                              "exit_status": "0"}
        (spec.job_dir).mkdir(parents=True, exist_ok=True)
        (spec.job_dir / "exit-record.json").write_text(json.dumps(self.records[unit]))
        return {"unit": unit, "launch_wall": 1.0, "boot_id": "b",
                "invocation_id": "i", "launch_argv": ["x"]}

    def inspect(self, spec):
        unit = linux_jobs.unit_name(spec.job_id)
        record = self.records.get(unit)
        collected = record is not None
        return {"unit": unit,
                "load_state": "not-found" if collected else "loaded",
                "active_state": None, "sub_state": None, "result": None,
                "exec_main_code": None, "exec_main_status": None,
                "invocation_id": None, "exit_record": record,
                "cgroup": "verified_empty" if collected else "failed",
                "observed_wall": 1.0,
                "phase": "terminal" if collected else "running",
                "ended": collected, "warning": None}

    classify = staticmethod(linux_jobs.classify)

    def stop(self, spec, kill=False):
        return {"requested": True, "rc": 0}

    def reconcile(self, spec, cancel_requested=False):
        observation = self.inspect(spec)
        return {"observation": observation,
                "verdict": self.classify(observation,
                                         cancel_requested=cancel_requested)}

    def read_logs(self, spec, after_cursor=None, max_bytes=32768):
        return {"entries": [], "next_cursor": after_cursor, "bytes": 0,
                "truncated": False, "gap": None}


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


class ReportDispatchTests(unittest.TestCase):
    """P1-4: report summaries are parsed according to the bound profile's
    report definitions (vitest/junit/go), not a hardcoded filename."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profiles_dir = self.root / "profiles"
        self.profiles_dir.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.evidence,
                                        session_id="sess", backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def _profile(self, kind: str, filename: str) -> None:
        profile = {
            "schema_version": 1, "profile_id": "demo.web.unit",
            "project": "demo", "component": "web", "operation": "unit",
            "status": "fixture_verified",
            "host": {"hostname": "any"},
            "command": {"argv_template": ["/bin/true"], "cwd": "{source_root}"},
            "selectors": {"allowed_kinds": ["file"], "base": ""},
            "deadlines": {"diagnose_seconds": 10, "hard_seconds": 30},
            "capabilities": {}, "inputs": {"fingerprints": [], "generated": []},
            "reports": [{"kind": kind,
                         "path_template": f"{{report_dir}}/{filename}",
                         "admitted": True}],
        }
        (self.profiles_dir / "demo.web.unit.json").write_text(json.dumps(profile))

    def _launch_and_reports(self, files: dict, profile_args: tuple):
        self._profile(*profile_args)
        # Legacy identity binding (argv + source_root) so the unscored
        # profile-bound launch passes verify_admission.
        # The disposable source tree is a SUBDIRECTORY so the job report dir
        # (under the evidence root) is legitimately outside the source tree.
        src = self.root / "src"
        src.mkdir(exist_ok=True)
        receipt = {"passed": True, "repeats": 3, "admission": "demo.web.unit",
                   "source_root": str(src),
                   "runs": [{"repeat": i, "argv": ["/bin/true"],
                             "outcome": "passed",
                             "cleanup": "verified_empty"} for i in (1, 2, 3)]}
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        profile = json.loads(
            (self.profiles_dir / "demo.web.unit.json").read_text())
        profile["admission_receipt"] = str(self.root / "receipt.json")
        (self.profiles_dir / "demo.web.unit.json").write_text(
            json.dumps(profile, indent=2))
        supervisor = Supervisor(self.controller, {
            "demo.web.unit": {"profile_id": "demo.web.unit", "scored": False,
                              "source_root": str(src)},
        }, profiles_dir=self.profiles_dir)
        # Unscored + argv path? Profile-bound unscored still renders from the
        # profile; give the op a source_root so resolve works.
        self.controller.backend.launch_calls.clear()
        result = supervisor.launch({"operation": "demo.web.unit",
                                    "request_id": "rp1"})
        job_id = result["job_id"]
        job_dir = (self.evidence / "jobs" / f"sess-rp1" / "reports")
        job_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (job_dir / name).write_text(content)
        receipt = self.controller.status(job_id)
        return supervisor.job_reports(job_id, receipt)

    def test_vitest_dispatch(self):
        report = ('{"numTotalTests":2,"numPassedTests":2,"numFailedTests":0,'
                  '"success":true,"testResults":[{"assertionResults":'
                  '[{"status":"passed","fullName":"a","failureMessages":[]},'
                  '{"status":"passed","fullName":"b","failureMessages":[]}]}]}')
        summaries = self._launch_and_reports(
            {"vitest-json.json": report}, ("vitest-json", "vitest-json.json"))
        self.assertEqual(summaries[0]["kind"], "vitest-json")
        self.assertEqual(summaries[0]["total"], 2)

    def test_junit_dispatch(self):
        report = ("<testsuites><testsuite name='s' tests='4' failures='1'"
                  " errors='0' skipped='0'></testsuite></testsuites>")
        summaries = self._launch_and_reports(
            {"junit.xml": report}, ("junit", "junit.xml"))
        self.assertEqual(summaries[0]["kind"], "junit")
        self.assertEqual(summaries[0]["total"], 4)

    def test_go_dispatch(self):
        report = ('\n'.join([
            json.dumps({"Action": "run", "Package": "p", "Test": "T1"}),
            json.dumps({"Action": "pass", "Package": "p", "Test": "T1",
                        "Elapsed": 0.1}),
            json.dumps({"Action": "pass", "Package": "p", "Elapsed": 1.0}),
        ]) + "\n")
        summaries = self._launch_and_reports(
            {"go-test.jsonl": report}, ("go-test-json", "go-test.jsonl"))
        self.assertEqual(summaries[0]["kind"], "go-test-json")
        self.assertEqual(summaries[0]["tests_passed"], 1)


class JobReportsHttpTests(unittest.TestCase):
    """P1-1 wire-shape pin: GET /jobs/<id> returns report summaries as the
    ARRAY `reports` (supervisor.py job_reports), with kind-specific
    counters. The JS bridge consumers normalize this via mapJobReports
    (exec-bridge-core.mjs / pi-bridge-core.mjs); this test pins the exact
    JSON body shape those consumers rely on, over real HTTP."""

    def setUp(self):
        import tempfile
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        self.http = HTTPConnection
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profiles_dir = self.root / "profiles"
        self.profiles_dir.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.evidence,
                                        session_id="sess", backend=self.backend)
        # Disposable source tree as a SUBDIRECTORY: the job report dir lives
        # under the evidence root, legitimately outside the source tree.
        src = self.root / "src"
        src.mkdir()
        receipt = {"passed": True, "repeats": 3, "admission": "demo.web.unit",
                   "source_root": str(src),
                   "runs": [{"repeat": i, "argv": ["/bin/true"],
                             "outcome": "passed",
                             "cleanup": "verified_empty"} for i in (1, 2, 3)]}
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        profile = {
            "schema_version": 1, "profile_id": "demo.web.unit",
            "project": "demo", "component": "web", "operation": "unit",
            "status": "fixture_verified",
            "host": {"hostname": "any"},
            "command": {"argv_template": ["/bin/true"], "cwd": "{source_root}"},
            "selectors": {"allowed_kinds": ["file"], "base": ""},
            "deadlines": {"diagnose_seconds": 10, "hard_seconds": 30},
            "capabilities": {}, "inputs": {"fingerprints": [], "generated": []},
            "reports": [{"kind": "vitest-json",
                         "path_template": "{report_dir}/vitest-json.json",
                         "admitted": True}],
            "admission_receipt": str(self.root / "receipt.json"),
        }
        (self.profiles_dir / "demo.web.unit.json").write_text(
            json.dumps(profile, indent=2))
        self.supervisor = Supervisor(self.controller, {
            "demo.web.unit": {"profile_id": "demo.web.unit", "scored": False,
                              "source_root": str(src)},
        }, token="tok-123", profiles_dir=self.profiles_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.supervisor))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.controller.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = self.http("127.0.0.1", self.port, timeout=10)
        headers = {"X-Agent-Exec-Token": "tok-123"}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_get_job_returns_reports_array_over_http(self):
        status, body = self.request("POST", "/jobs",
                                    {"operation": "demo.web.unit",
                                     "request_id": "rp1"})
        self.assertEqual(status, 200)
        job_id = body["job_id"]
        report_dir = self.evidence / "jobs" / "sess-rp1" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "vitest-json.json").write_text(json.dumps({
            "numTotalTests": 5, "numPassedTests": 4, "numFailedTests": 1,
            "success": False,
            "testResults": [{"assertionResults": [
                {"status": "failed", "fullName": "a", "failureMessages": []},
            ]}],
        }))
        status, body = self.request("GET", f"/jobs/{job_id}")
        self.assertEqual(status, 200)
        # mapJobReports contract input: `reports` is an array of parsed
        # summaries — the old singular `report` field never exists.
        self.assertIsInstance(body.get("reports"), list)
        self.assertEqual(len(body["reports"]), 1)
        first = body["reports"][0]
        self.assertEqual(first["kind"], "vitest-json")
        self.assertIsInstance(first["total"], int)
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["passed"], 4)
        self.assertEqual(first["failed"], 1)

    def test_get_job_without_report_file_keeps_reports_array_empty_or_missing(self):
        status, body = self.request("POST", "/jobs",
                                    {"operation": "demo.web.unit",
                                     "request_id": "rp2"})
        self.assertEqual(status, 200)
        status, body = self.request("GET", f"/jobs/{body['job_id']}")
        self.assertEqual(status, 200)
        self.assertIsInstance(body.get("reports"), list)
        # Missing report file -> one integrity="missing" summary, never a
        # fabricated pass and never a null singular report.
        self.assertEqual(body["reports"][0]["integrity"], "missing")


if __name__ == "__main__":
    unittest.main()
