"""Admission-enforcement tests (P0#6): scored operations require an admitted
profile with an intact receipt; commands render from the profile; legacy argv
stays available for unscored calibration only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec import linux_jobs  # noqa: E402
from agent_exec.harness.opencode.supervisor import Supervisor  # noqa: E402
from agent_exec.jobs import JobController, JobError  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.launch_calls = []

    def launch(self, spec):
        self.launch_calls.append(spec)
        return {"unit": linux_jobs.unit_name(spec.job_id), "launch_wall": 1.0,
                "boot_id": "b", "invocation_id": "i", "launch_argv": ["x"]}

    classify = staticmethod(linux_jobs.classify)


def write_receipt(path: Path, *, passed=True, runs=3, profile_id="demo.web.unit",
                  profile_sha=None, argv=None, source_root="/own/src"):
    receipt = {
        "schema_version": 1, "admission": profile_id, "passed": passed,
        "repeats": runs, "runs": [
            {"repeat": i + 1, "outcome": "passed" if passed else "failed",
             "cleanup": "verified_empty", **({"argv": argv} if argv else {})}
            for i in range(runs)],
        "source_root": source_root,
    }
    if profile_sha:
        receipt["profile_sha256"] = profile_sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2))


class AdmissionEnforcementTests(unittest.TestCase):
    def setUp(self):
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

    def write_profile(self, *, status="fixture_verified", receipt=None,
                      profile_id="demo.web.unit"):
        profile = {
            "schema_version": 1, "profile_id": profile_id,
            "project": "demo", "component": "web", "operation": "unit",
            "status": status,
            "host": {"hostname": "any"},
            "command": {"argv_template": ["echo", "run", "--out",
                                          "{report_dir}/r.json"],
                        "cwd": "{source_root}"},
            "selectors": {"allowed_kinds": ["file"], "base": ""},
            "deadlines": {"diagnose_seconds": 30, "hard_seconds": 60},
            "capabilities": {},
            "inputs": {"fingerprints": [], "generated": []},
        }
        if receipt:
            profile["admission_receipt"] = str(receipt)
        path = self.profiles_dir / f"{profile_id}.json"
        path.write_text(json.dumps(profile, indent=2))
        return path

    def supervisor(self):
        return Supervisor(self.controller, {
            "demo.web.unit": {"profile_id": "demo.web.unit", "scored": True,
                              "source_root": str(self.root / "src")},
        }, profiles_dir=self.profiles_dir)

    def test_scored_admitted_profile_launches_profile_rendered_command(self):
        receipt = self.root / "receipt.json"
        self.write_profile(receipt=receipt)
        import hashlib
        write_receipt(receipt, profile_sha=hashlib.sha256(
            (self.profiles_dir / "demo.web.unit.json").read_bytes()).hexdigest())
        supervisor = self.supervisor()
        result = supervisor.launch({"operation": "demo.web.unit",
                                    "request_id": "ok1"})
        spec = self.controller.backend.launch_calls[-1]
        self.assertFalse(result["replayed"])
        self.assertIn("--out", spec.argv)
        self.assertIn(str(self.evidence / "jobs" / "sess-ok1" / "reports"),
                      " ".join(spec.argv))
        self.assertEqual(spec.deadline_seconds, 60)
        self.assertEqual(result["command_origin"], "admitted:demo.web.unit")

    def test_scored_draft_rejected(self):
        self.write_profile(status="draft")
        with self.assertRaises(JobError):
            self.supervisor().launch({"operation": "demo.web.unit",
                                      "request_id": "d1"})

    def test_zero_run_receipt_rejected(self):
        receipt = self.root / "receipt.json"
        write_receipt(receipt, runs=0)
        self.write_profile(receipt=receipt)
        with self.assertRaises(JobError) as caught:
            self.supervisor().launch({"operation": "demo.web.unit",
                                      "request_id": "z1"})
        self.assertIn("insufficient repeats", str(caught.exception))

    def test_single_run_receipt_rejected(self):
        receipt = self.root / "receipt.json"
        write_receipt(receipt, runs=1)
        self.write_profile(receipt=receipt)
        with self.assertRaises(JobError):
            self.supervisor().launch({"operation": "demo.web.unit",
                                      "request_id": "o1"})

    def test_missing_receipt_file_rejected(self):
        self.write_profile(receipt=self.root / "absent.json")
        with self.assertRaises(JobError):
            self.supervisor().launch({"operation": "demo.web.unit",
                                      "request_id": "m1"})

    def test_failing_receipt_rejected(self):
        receipt = self.root / "receipt.json"
        write_receipt(receipt, passed=False)
        self.write_profile(receipt=receipt)
        with self.assertRaises(JobError):
            self.supervisor().launch({"operation": "demo.web.unit",
                                      "request_id": "f1"})

    def test_changed_profile_invalidates_sha_bound_receipt(self):
        receipt = self.root / "receipt.json"
        import hashlib
        # The sha binds the FINAL profile bytes: add the receipt reference
        # first, hash, then record the hash in the receipt itself.
        self.write_profile(receipt=receipt)
        sha = hashlib.sha256((self.profiles_dir / "demo.web.unit.json")
                             .read_bytes()).hexdigest()
        write_receipt(receipt, profile_sha=sha)
        supervisor = self.supervisor()
        supervisor.launch({"operation": "demo.web.unit", "request_id": "s1"})
        # Now change the command; the sha no longer matches the receipt.
        profile = json.loads((self.profiles_dir / "demo.web.unit.json").read_text())
        profile["command"]["argv_template"] = ["echo", "changed"]
        (self.profiles_dir / "demo.web.unit.json").write_text(json.dumps(profile))
        with self.assertRaises(JobError) as caught:
            supervisor.launch({"operation": "demo.web.unit", "request_id": "s2"})
        self.assertIn("re-admission", str(caught.exception))

    def test_unscored_legacy_argv_still_allowed(self):
        supervisor = Supervisor(self.controller, {
            "calib.op": {"argv": ["/bin/true"], "scored": False,
                         "deadline_seconds": 30},
        }, profiles_dir=self.profiles_dir)
        result = supervisor.launch({"operation": "calib.op",
                                    "request_id": "cal1"})
        self.assertEqual(result["command_origin"], "unscored:legacy-argv")

    def test_scored_raw_argv_rejected(self):
        supervisor = Supervisor(self.controller, {
            "rogue.op": {"argv": ["/bin/true"], "scored": True},
        }, profiles_dir=self.profiles_dir)
        with self.assertRaises(JobError):
            supervisor.launch({"operation": "rogue.op", "request_id": "r1"})


if __name__ == "__main__":
    unittest.main()
