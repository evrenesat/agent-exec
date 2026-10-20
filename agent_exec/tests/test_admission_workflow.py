"""P0-3 integration: the documented draft -> admitted workflow on a
disposable repository. Promoting the profile must not invalidate the
receipt; changing an execution-relevant field must."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.jobs import JobController  # noqa: E402
from agent_exec.profiles import admission_identity, verify_admission  # noqa: E402

VITEST_SNAPSHOT = ('{"numTotalTestSuites":1,"numPassedTestSuites":1,'
                   '"numFailedTestSuites":0,"numPendingTestSuites":0,'
                   '"numTotalTests":1,"numPassedTests":1,"numFailedTests":0,'
                   '"numPendingTests":0,"numTodoTests":0,"startTime":1,'
                   '"success":true,"testResults":[{"assertionResults":'
                   '[{"status":"passed","fullName":"demo works",'
                   '"failureMessages":[]}]}]}')


class DisposableRepo:
    """A tiny disposable 'project': a script that emits a valid vitest-json
    report. Exercises the full admission path without npm."""

    def __init__(self, root: Path):
        self.root = root
        (root / "reports").mkdir(parents=True, exist_ok=True)
        self.run_fake_test = root / "run_fake_test.sh"
        self.run_fake_test.write_text(
            "#!/bin/sh\n"
            f"echo '{VITEST_SNAPSHOT}' > \"$1\"\n")
        self.run_fake_test.chmod(0o755)

    def profile(self, *, status="draft", receipt=None):
        profile = {
            "schema_version": 1,
            "profile_id": "disposable.web.unit",
            "project": "disposable", "component": "web", "operation": "unit",
            "status": status,
            "host": {"hostname": "any"},
            "command": {"argv_template": [str(self.run_fake_test),
                                          "{report_dir}/vitest-json.json"],
                        "cwd": "{source_root}"},
            "selectors": {"allowed_kinds": ["file"], "base": ""},
            "deadlines": {"diagnose_seconds": 10, "hard_seconds": 30},
            "capabilities": {},
            "inputs": {"fingerprints": [], "generated": []},
            "reports": [{"kind": "vitest-json",
                         "path_template": "{report_dir}/vitest-json.json",
                         "admitted": True}],
        }
        if receipt:
            profile["admission_receipt"] = str(receipt)
        return profile


class AdmissionWorkflowTests(unittest.TestCase):
    """Draft -> admit (3 real runs) -> promote -> verify -> mutate => invalid."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = DisposableRepo(self.root / "repo")
        self.backend = FakeBackendSimple()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "ev",
                                        session_id="wf", backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def admit(self, profile: dict, profile_path: Path) -> dict:
        """Mirror admit.py's contract: 3 real runs through the job layer."""
        runs = []
        for index in (1, 2, 3):
            report_dir = self.root / f"adm-run-{index}" / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            argv = [part.replace("{report_dir}", str(report_dir))
                    for part in profile["command"]["argv_template"]]
            started = self.controller.launch(
                argv, request_id=f"wf-{profile['profile_id']}-{index}",
                operation_id=profile["profile_id"], deadline_seconds=30,
                stop_grace_seconds=2, cwd=str(self.repo.root))
            receipt = None
            deadline_at = time.monotonic() + 30
            while time.monotonic() < deadline_at:
                result = self.controller.wait(started["job_id"], max_wait_seconds=5)
                if not result["elapsed"]:
                    receipt = result.get("receipt") or self.controller.status(started["job_id"])
                    break
            receipt = receipt or self.controller.status(started["job_id"])
            runs.append({"repeat": index, "argv": argv,
                         "outcome": receipt.get("outcome"),
                         "cleanup": receipt.get("cleanup"),
                         "job_id": started["job_id"]})
        self.assertTrue(all(r["outcome"] == "passed" for r in runs))
        receipt = {
            "schema_version": 1, "admission": profile["profile_id"],
            "status_before": profile["status"], "repeats": 3,
            "profile_identity_sha256": admission_identity(profile),
            "source_root": str(self.repo.root),
            "runs": runs, "passed": True,
        }
        receipt_path = self.root / "admission-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2))
        return receipt

    def test_promotion_preserves_admission_command_change_invalidates(self):
        # 1. draft profile, admitted via 3 real runs.
        profile = self.repo.profile(status="draft")
        receipt = self.admit(profile, self.root / "profile.json")
        # 2. promote: the documented administrative edits.
        profile["status"] = "fixture_verified"
        profile["admission_receipt"] = str(self.root / "admission-receipt.json")
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(profile, indent=2))
        # 3. verification of the promoted profile must PASS.
        self.assertEqual(verify_admission(profile, scored=True,
                                          profile_path=profile_path), [])
        # 4. administrative churn (notes) still passes.
        profile["notes"] = ["touched by an operator"]
        self.assertEqual(verify_admission(profile, scored=True,
                                          profile_path=profile_path), [])
        # 5. an execution-relevant change INVALIDATES.
        profile["command"]["argv_template"].append("--strict")
        self.assertNotEqual(
            verify_admission(profile, scored=True, profile_path=profile_path), [])

    def test_fake_backend_runs_real_report_cycle(self):
        profile = self.repo.profile(status="draft")
        receipt = self.admit(profile, self.root / "p.json")
        self.assertEqual(receipt["passed"], True)
        self.assertEqual(receipt["repeats"], 3)
        self.assertTrue(all(r["cleanup"] == "verified_empty" for r in receipt["runs"]))


class FakeBackendSimple:
    """Executes the rendered command for real (subprocess), asserting the
    full admission workflow end-to-end including report parsing inputs."""

    def __init__(self):
        self.launch_calls = []

    def launch(self, spec):
        import subprocess as sp
        self.launch_calls.append(spec)
        proc = sp.run(spec.argv, capture_output=True,
                      cwd=spec.cwd, timeout=25)
        spec.job_dir.mkdir(parents=True, exist_ok=True)
        record_file = spec.job_dir / "exit-record.json"
        record = {"service_result": "success" if proc.returncode == 0
                  else "exit-code",
                  "exit_code": "exited", "exit_status": str(proc.returncode)}
        record_file.write_text(json.dumps(record))
        return {"unit": linux_jobs.unit_name(spec.job_id),
                "launch_wall": time.time(), "boot_id": "b",
                "invocation_id": "i", "launch_argv": spec.argv}

    def inspect(self, spec):
        record_file = spec.job_dir / "exit-record.json"
        record = json.loads(record_file.read_text()) if record_file.is_file() else None
        collected = record is not None
        return {"unit": linux_jobs.unit_name(spec.job_id),
                "load_state": "not-found" if collected else "loaded",
                "active_state": None, "sub_state": None, "result": None,
                "exec_main_code": None, "exec_main_status": None,
                "invocation_id": None, "exit_record": record,
                "cgroup": "verified_empty" if collected else "failed",
                "observed_wall": time.time(),
                "phase": "terminal" if collected else "running",
                "ended": collected, "warning": None}

    classify = staticmethod(linux_jobs.classify)

    def stop(self, spec, kill=False):
        return {"requested": True, "rc": 0}

    def read_logs(self, spec, after_cursor=None, max_bytes=32768):
        return {"entries": [], "next_cursor": after_cursor, "bytes": 0,
                "truncated": False, "gap": None}

    def reconcile(self, spec, cancel_requested=False):
        observation = self.inspect(spec)
        return {"observation": observation,
                "verdict": self.classify(observation,
                                         cancel_requested=cancel_requested)}


if __name__ == "__main__":
    unittest.main()
