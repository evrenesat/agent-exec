"""Regression tests for independent-review findings (P0-2, P0-4, P1-2).

P0-2: the CK3 driver (agent_exec/harness/opencode/run_real_validation.py)
builds its operation configuration in build_operations(); these tests launch
the driver's OWN dict — imported verbatim, never mirrored or hand-corrected —
through a real Supervisor with the REAL agent_exec/profiles directory, so the
scored, source-root-bound operation validates against the admitted
aflow-web-p100 profile before any backend launch. The historical broken shape
(a scored profile-bound operation without source_root, plus raw argv/cwd that
the supervisor rejects) is pinned as rejected.

P0-4: profile-bound operations must take their working directory from the
profile render (including {source_root}/subpath templates), not from the
caller, the evidence root, or a None default.

P1-2: wait() must finalize a collected unit whose exit record is missing as
inconclusive in the SAME wait call (the pre-fix bug returned elapsed=True and
left the caller with no receipt).

No systemd is touched: every backend here is an in-memory fake.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.contracts import render  # noqa: E402
from agent_exec.harness.opencode.supervisor import Supervisor  # noqa: E402
from agent_exec.harness.opencode import run_real_validation  # noqa: E402
from agent_exec.jobs import JobController, JobError  # noqa: E402
from agent_exec.tests.test_jobs import FakeBackend  # noqa: E402


class RecordingFakeBackend:
    """FakeBackend pattern from tests/test_supervisor.py: launch records the
    successful exit record, inspect reports the unit terminal/collected."""

    def __init__(self):
        self.launch_calls = []
        self.records: dict[str, dict] = {}

    def launch(self, spec):
        self.launch_calls.append(spec)
        unit = linux_jobs.unit_name(spec.job_id)
        self.records[unit] = {"service_result": "success", "exit_code": "exited",
                              "exit_status": "0"}
        spec.job_dir.mkdir(parents=True, exist_ok=True)
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


def write_legacy_receipt(path: Path, *, profile_id: str, source_root: str,
                         argv: list[str], passed=True, runs=3):
    """Legacy-bound admission receipt (pattern from
    tests/test_admission_enforcement.py: identity proven by recorded argv +
    source_root, re-checked against the current profile render)."""
    receipt = {
        "schema_version": 1, "admission": profile_id, "passed": passed,
        "repeats": runs, "source_root": source_root,
        "runs": [{"repeat": i + 1, "outcome": "passed" if passed else "failed",
                  "cleanup": "verified_empty", "argv": argv}
                 for i in range(runs)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2))


class ReviewP0_2DriverOperationTests(unittest.TestCase):
    """The driver's OWN build_operations() output must pass supervisor
    configuration validation (admission + source binding + profile render)
    BEFORE the model session starts; the historical broken shape (a scored
    profile-bound operation without source_root) is rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.backend = RecordingFakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.evidence,
                                        session_id="drv-session",
                                        backend=self.backend)
        self.profiles_dir = HERE.parent / "profiles"  # the real profiles dir
        self.session_home = self.root / "home"

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def driver_operations(self):
        """The driver's dict VERBATIM: build_operations() is imported and
        called with a session home — no mirror, no hand corrections."""
        return run_real_validation.build_operations(self.session_home)

    def make_supervisor(self, operations):
        return Supervisor(self.controller, operations,
                          profiles_dir=self.profiles_dir)

    def test_driver_operation_is_scored_profile_bound_shape(self):
        spec = self.driver_operations()["aflow.web.unit"]
        # No raw argv and no cwd: the admitted profile renders both.
        self.assertNotIn("argv", spec)
        self.assertNotIn("cwd", spec)
        self.assertTrue(spec["scored"])
        self.assertEqual(spec["profile_id"], "aflow-web-p100")
        self.assertEqual(spec["source_root"], run_real_validation.SOURCE)
        self.assertEqual(spec["deadline_seconds"], 180)
        self.assertEqual(spec["env"]["HOME"], str(self.session_home))

    def test_driver_operation_validates_and_renders_profile_command(self):
        supervisor = self.make_supervisor(self.driver_operations())
        result = supervisor.launch({"operation": "aflow.web.unit",
                                    "request_id": "drvcheck"})
        self.assertEqual(result["command_origin"], "admitted:aflow-web-p100")
        self.assertFalse(result["replayed"])
        spec = self.controller.backend.launch_calls[-1]
        # argv matches the admitted profile render: npm test flags for the
        # one-worker forks pool, with the per-job report path inside the
        # evidence root (never the driver's literal argv — it has none).
        argv = list(spec.argv)
        self.assertEqual(argv[0], "npm")
        for flag in ("test", "--run", "--maxWorkers=1", "--minWorkers=1",
                     "--pool=forks", "--reporter=json"):
            self.assertIn(flag, argv)
        report_dir = str(self.evidence / "jobs" / "drv-session-drvcheck"
                         / "reports")
        self.assertIn(report_dir, " ".join(argv))
        self.assertTrue(any(part.endswith("vitest-json.json") for part in argv))
        # The profile is the cwd authority; the driver's source root applies.
        self.assertEqual(spec.cwd, run_real_validation.SOURCE)
        # Deadline comes from the admitted profile (hard_seconds 180).
        self.assertEqual(spec.deadline_seconds, 180)

    def test_historical_broken_shape_without_source_root_rejected(self):
        """The historical broken shape: the driver once emitted this operation
        with raw argv + cwd and NO source_root, so the supervisor — which
        requires source_root for profile-bound operations — rejected it and
        the driver could never launch. A copy of build_operations' dict with
        source_root deleted must still be rejected before any backend launch."""
        operations = run_real_validation.build_operations(self.session_home)
        del operations["aflow.web.unit"]["source_root"]  # the historical bug
        supervisor = self.make_supervisor(operations)
        with self.assertRaises(JobError) as caught:
            supervisor.launch({"operation": "aflow.web.unit",
                               "request_id": "drvcheck"})
        self.assertIn("no source_root", str(caught.exception))
        # Nothing reached the backend: the model must never start on an
        # operation that fails configuration validation.
        self.assertEqual(len(self.controller.backend.launch_calls), 0)


class SupervisorMainArgparseTests(unittest.TestCase):
    """supervisor.main() must accept --profiles-dir and forward it to the
    Supervisor. Parse_args level only: controller, supervisor construction is
    real but the HTTP server is faked, so nothing binds or starts."""

    def run_main(self, extra_args):
        from agent_exec.harness.opencode import supervisor as supervisor_module

        class FakeController:
            def close(self):
                pass

        class FakeServer:
            def __init__(self, address, handler):
                self.server_port = 0  # main reads it for the startup line

            def serve_forever(self):
                raise KeyboardInterrupt  # leave main() before any real serving

            def server_close(self):
                pass

        captured = {}
        real_supervisor = supervisor_module.Supervisor

        def recording_supervisor(controller, operations, token=None,
                                 profiles_dir=None):
            captured["profiles_dir"] = profiles_dir
            return real_supervisor(controller, operations, token=token,
                                   profiles_dir=profiles_dir)

        argv = ["supervisor.py", "--store", "unused.sqlite",
                "--evidence-root", "unused-evidence", *extra_args]
        with mock.patch("sys.argv", argv), \
                mock.patch.object(supervisor_module, "JobController",
                                  lambda *a, **k: FakeController()), \
                mock.patch.object(supervisor_module, "Supervisor",
                                  recording_supervisor), \
                mock.patch.object(supervisor_module, "ThreadingHTTPServer",
                                  FakeServer), \
                mock.patch("builtins.print"):
            exit_code = supervisor_module.main()
        return exit_code, captured

    def test_profiles_dir_flag_is_parsed_and_forwarded(self):
        exit_code, captured = self.run_main(
            ["--profiles-dir", "/tmp/some-profiles"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["profiles_dir"], Path("/tmp/some-profiles"))

    def test_profiles_dir_defaults_to_none_when_omitted(self):
        exit_code, captured = self.run_main([])
        self.assertEqual(exit_code, 0)
        self.assertIsNone(captured["profiles_dir"])


class ReviewP0_4ProfileCwdTests(unittest.TestCase):
    """A profile-bound operation takes its working directory from the profile
    render. contracts.render() formats command.cwd with {source_root} and only
    requires the result to stay inside the source root, so {source_root}/subdir
    templates are valid; the supervisor must forward the rendered cwd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profiles_dir = self.root / "profiles"
        self.profiles_dir.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        self.backend = RecordingFakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.evidence,
                                        session_id="sess", backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def write_profile(self):
        profile = {
            "schema_version": 1, "profile_id": "cwdsub.web.unit",
            "project": "cwdsub", "component": "web", "operation": "unit",
            "status": "fixture_verified",
            "host": {"hostname": "any"},
            "command": {"argv_template": ["echo", "run", "--out",
                                          "{report_dir}/r.json"],
                        "cwd": "{source_root}/subdir"},
            "selectors": {"allowed_kinds": ["file"], "base": ""},
            "deadlines": {"diagnose_seconds": 30, "hard_seconds": 60},
            "capabilities": {},
            "inputs": {"fingerprints": [], "generated": []},
        }
        path = self.profiles_dir / "cwdsub.web.unit.json"
        path.write_text(json.dumps(profile, indent=2))
        receipt = self.root / "receipt.json"
        rendered = render(profile, source_root=str(self.src), job_id="admit",
                          report_dir="/ADMITEVT/jobs/admit/reports")
        write_legacy_receipt(receipt, profile_id="cwdsub.web.unit",
                             source_root=str(self.src),
                             argv=list(rendered.argv))
        profile = json.loads(path.read_text())
        profile["admission_receipt"] = str(receipt)
        path.write_text(json.dumps(profile, indent=2))

    def test_launch_cwd_is_profile_rendered_subpath(self):
        self.write_profile()
        supervisor = Supervisor(self.controller, {
            "cwdsub.web.unit": {"profile_id": "cwdsub.web.unit",
                                "scored": False,
                                "source_root": str(self.src)},
        }, profiles_dir=self.profiles_dir)
        result = supervisor.launch({"operation": "cwdsub.web.unit",
                                    "request_id": "cwd1"})
        self.assertFalse(result["replayed"])
        spec = self.controller.backend.launch_calls[-1]
        self.assertIsNotNone(spec.cwd)
        self.assertEqual(spec.cwd, str(self.src / "subdir"))
        self.assertNotEqual(spec.cwd, str(self.evidence))


class ReviewP1_2WaitMissingRecordTests(unittest.TestCase):
    """wait() must finalize a collected unit with a missing exit record as
    inconclusive within the same call — never return elapsed=True."""

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

    def test_wait_finalizes_collected_missing_record_inconclusive(self):
        job = self.controller.launch(["/bin/true"], request_id="ghost-wait",
                                     operation_id="op")["job_id"]
        spec = self.controller._spec_for(self.controller.store.get_job(job))
        # Collected with NO exit record (mirror of test_jobs'
        # collected-missing-record pattern): record stays None.
        self.backend.units[linux_jobs.unit_name(spec.job_id)]["collected"] = True
        result = self.controller.wait(job, max_wait_seconds=5)
        self.assertIs(result["elapsed"], False)
        self.assertIsNotNone(result["receipt"])
        self.assertEqual(result["receipt"]["outcome"], "inconclusive")
        self.assertIsNotNone(result["receipt"]["inconclusive_reason"])


if __name__ == "__main__":
    unittest.main()
