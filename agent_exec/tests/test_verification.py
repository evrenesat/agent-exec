"""Verification ledger and manifest tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.verification import (  # noqa: E402
    LedgerEntry, Requirement, VerificationLedger)


def ledger() -> VerificationLedger:
    return VerificationLedger([
        Requirement("unit", "aflow.web.unit", scope="full"),
        Requirement("lint", "aflow.web.lint", scope="src"),
        Requirement("visual", "manual-review", manual=True),
    ])


class LedgerTests(unittest.TestCase):
    def test_manifest_starts_incomplete(self):
        manifest = ledger().manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["state"], "missing")

    def test_unknown_requirement_rejected(self):
        with self.assertRaises(KeyError):
            ledger().observe(requirement_id="nope", job_id=None, outcome="passed")

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            ledger().observe(requirement_id="unit", job_id="j1", outcome="green")

    def test_complete_requires_every_check_passed(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                    receipt={"outcome": "passed"})
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])  # lint + visual still missing
        led.observe(requirement_id="lint", job_id="j2", outcome="passed")
        led.observe(requirement_id="visual", job_id=None, outcome="passed")
        self.assertTrue(led.manifest()["complete"])

    def test_running_and_failed_block_completion(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="running")
        self.assertFalse(led.manifest()["complete"])
        led.observe(requirement_id="unit", job_id="j1", outcome="failed",
                    receipt={"outcome": "failed"})
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["receipt_outcome"], "failed")

    def test_fingerprint_change_stales_passed_checks(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                    input_fingerprint="abc")
        self.assertEqual(led.invalidate_stale("xyz"), ["unit"])
        self.assertEqual(led.manifest()["checks"]["unit"]["state"], "stale")
        self.assertFalse(led.manifest()["complete"])

    def test_unknown_fingerprint_invalidates_nothing(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                    input_fingerprint="abc")
        self.assertEqual(led.invalidate_stale(None), [])
        self.assertEqual(led.manifest()["checks"]["unit"]["state"], "passed")

    def test_manual_acceptance_is_declared_not_inferred(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed")
        led.observe(requirement_id="lint", job_id="j2", outcome="passed")
        led.observe(requirement_id="visual", job_id=None, outcome="passed")
        manifest = led.manifest()
        self.assertTrue(manifest["checks"]["visual"]["manual"])
        self.assertTrue(any("independent product review" in note
                            for note in [manifest["note"]]))

    def test_reobserve_replaces_receipt(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="failed")
        led.observe(requirement_id="unit", job_id="j2", outcome="passed",
                    input_fingerprint="f1")
        entry = led.entries["unit"]
        self.assertEqual(entry.state, "passed")
        self.assertEqual(entry.job_id, "j2")


if __name__ == "__main__":
    unittest.main()
