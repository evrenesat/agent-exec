"""Verification ledger and manifest tests."""

from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.jobs import JobController  # noqa: E402
from agent_exec.tests.test_jobs import FakeBackend  # noqa: E402
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
        led.observe_from_store(
            FakeStoreController({
                "j1": {"job_id": "j1", "operation_id": "aflow.web.unit",
                       "outcome": "passed", "cleanup": "verified_empty"}}),
            requirement_id="unit", job_id="j1")
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])  # lint + visual still missing
        led.observe_from_store(
            FakeStoreController({
                "j2": {"job_id": "j2", "operation_id": "aflow.web.lint",
                       "outcome": "passed", "cleanup": "verified_empty"}}),
            requirement_id="lint", job_id="j2")
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
                    receipt={"outcome": "passed", "cleanup": "verified_empty"},
                    input_fingerprint="abc")
        self.assertEqual(led.invalidate_stale("xyz"), ["unit"])
        self.assertEqual(led.manifest()["checks"]["unit"]["state"], "stale")
        self.assertFalse(led.manifest()["complete"])

    def test_unknown_fingerprint_invalidates_nothing(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                    receipt={"outcome": "passed", "cleanup": "verified_empty"},
                    input_fingerprint="abc")
        self.assertEqual(led.invalidate_stale(None), [])
        self.assertEqual(led.manifest()["checks"]["unit"]["state"], "passed")

    def test_manual_acceptance_is_declared_not_inferred(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                    receipt={"outcome": "passed", "cleanup": "verified_empty"})
        led.observe(requirement_id="lint", job_id="j2", outcome="passed",
                    receipt={"outcome": "passed", "cleanup": "verified_empty"})
        led.observe(requirement_id="visual", job_id=None, outcome="passed")
        manifest = led.manifest()
        self.assertTrue(manifest["checks"]["visual"]["manual"])
        self.assertTrue(any("independent product review" in note
                            for note in [manifest["note"]]))

    def test_reobserve_replaces_receipt(self):
        led = ledger()
        led.observe(requirement_id="unit", job_id="j1", outcome="failed")
        led.observe(requirement_id="unit", job_id="j2", outcome="passed",
                    receipt={"outcome": "passed", "cleanup": "verified_empty"},
                    input_fingerprint="f1")
        entry = led.entries["unit"]
        self.assertEqual(entry.state, "passed")
        self.assertEqual(entry.job_id, "j2")


class ReceiptDerivedPassTests(unittest.TestCase):
    """P1#7: an automated check cannot pass on a bare status string."""

    def setUp(self):
        self.led = ledger()

    def test_pass_without_job_id_rejected(self):
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id=None, outcome="passed")

    def test_pass_without_validating_receipt_rejected(self):
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id="j9", outcome="passed",
                             receipt={"outcome": "failed",
                                      "cleanup": "verified_empty"})
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id="j9", outcome="passed",
                             receipt={"outcome": "passed", "cleanup": "unknown"})

    def test_pass_with_full_receipt_accepted(self):
        entry = self.led.observe(requirement_id="unit", job_id="j9",
                                 outcome="passed",
                                 receipt={"outcome": "passed",
                                          "cleanup": "verified_empty"})
        self.assertEqual(entry.state, "passed")

    def test_manual_requirement_still_accepts_declared_pass(self):
        self.led.observe(requirement_id="visual", job_id=None, outcome="passed")
        self.assertEqual(self.led.manifest()["checks"]["visual"]["state"], "passed")

    def test_manual_declaration_marks_observed_via_manual(self):
        entry = self.led.observe(requirement_id="visual", job_id=None,
                                 outcome="passed")
        self.assertEqual(entry.observed_via, "manual")
        check = self.led.manifest()["checks"]["visual"]
        self.assertEqual(check["observed_via"], "manual")


class LedgerOperationBindingTests(unittest.TestCase):
    """P1-3 round 2: receipts must belong to the requirement's operation."""

    def setUp(self):
        self.led = ledger()

    def test_foreign_operation_receipt_rejected(self):
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                             receipt={"outcome": "passed",
                                      "cleanup": "verified_empty",
                                      "operation_id": "other.op"})

    def test_matching_operation_receipt_accepted(self):
        entry = self.led.observe(requirement_id="unit", job_id="j1",
                                 outcome="passed",
                                 receipt={"outcome": "passed",
                                          "cleanup": "verified_empty",
                                          "operation_id": "aflow.web.unit"})
        self.assertEqual(entry.state, "passed")


class LedgerStoreBindingTests(unittest.TestCase):
    """P1-3: required checks observed from the trusted store only."""

    def store_ledger(self) -> VerificationLedger:
        return VerificationLedger(
            [Requirement("unit", "aflow.web.unit", scope="full")])

    def store_receipt(self, job_id="j1", operation_id="aflow.web.unit",
                      **overrides):
        receipt = {"job_id": job_id, "operation_id": operation_id,
                   "outcome": "passed", "cleanup": "verified_empty"}
        receipt.update(overrides)
        return receipt

    def test_passed_store_receipt_completes_manifest(self):
        controller = FakeStoreController({"j1": self.store_receipt()})
        led = self.store_ledger()
        entry = led.observe_from_store(controller, requirement_id="unit",
                                       job_id="j1")
        self.assertEqual(entry.state, "passed")
        self.assertEqual(entry.job_id, "j1")
        self.assertEqual(controller.requested, ["j1"])
        self.assertTrue(led.manifest()["complete"])

    def test_running_store_status_maps_to_running(self):
        # REAL running shape from JobController.status(): only
        # job_id/state/phase/unit — no operation_id, no outcome.
        controller = FakeStoreController({
            "j1": {"job_id": "j1", "state": "running",
                   "phase": "running", "unit": "u1"}})
        led = self.store_ledger()
        entry = led.observe_from_store(controller, requirement_id="unit",
                                       job_id="j1")
        self.assertEqual(entry.state, "running")
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["state"], "running")

    def test_inconclusive_receipt_maps_to_failed_with_visible_outcome(self):
        controller = FakeStoreController({
            "j1": self.store_receipt(outcome="inconclusive",
                                     cleanup="unknown",
                                     inconclusive_reason="exit evidence "
                                                         "missing")})
        led = self.store_ledger()
        entry = led.observe_from_store(controller, requirement_id="unit",
                                       job_id="j1")
        self.assertEqual(entry.state, "failed")
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["receipt_outcome"],
                         "inconclusive")
        self.assertEqual(entry.receipt["inconclusive_reason"],
                         "exit evidence missing")

    def test_all_non_pass_terminals_map_to_failed(self):
        outcomes = [("failed", "failed_exit"), ("timed_out", "deadline"),
                    ("cancelled", "cancel_requested"),
                    ("blocked", "unlaunched"),
                    ("passed", "cleanup_unknown")]
        for outcome, cleanup in outcomes:
            with self.subTest(outcome=outcome, cleanup=cleanup):
                controller = FakeStoreController({
                    "j1": self.store_receipt(outcome=outcome,
                                             cleanup=cleanup)})
                led = self.store_ledger()
                entry = led.observe_from_store(controller,
                                               requirement_id="unit",
                                               job_id="j1")
                self.assertEqual(entry.state, "failed")
                self.assertFalse(led.manifest()["complete"])

    def test_operation_mismatch_rejected(self):
        controller = FakeStoreController({
            "j1": self.store_receipt(operation_id="other.op")})
        with self.assertRaises(ValueError):
            self.store_ledger().observe_from_store(
                controller, requirement_id="unit", job_id="j1")

    def test_receipt_job_identity_mismatch_rejected(self):
        controller = FakeStoreController({
            "j1": self.store_receipt(job_id="other-job")})
        with self.assertRaises(ValueError):
            self.store_ledger().observe_from_store(
                controller, requirement_id="unit", job_id="j1")

    def test_unknown_job_raises_controller_error(self):
        controller = FakeStoreController({})
        with self.assertRaises((ValueError, KeyError)):
            self.store_ledger().observe_from_store(
                controller, requirement_id="unit", job_id="ghost")

    def test_cross_session_error_becomes_value_error(self):
        controller = CrossSessionController({})
        with self.assertRaises(ValueError):
            self.store_ledger().observe_from_store(
                controller, requirement_id="unit", job_id="j1")

    def test_no_receipt_parameter_cannot_be_bypassed(self):
        signature = inspect.signature(VerificationLedger.observe_from_store)
        self.assertNotIn("receipt", signature.parameters)
        self.assertNotIn("outcome", signature.parameters)
        with self.assertRaises(TypeError):
            self.store_ledger().observe_from_store(
                FakeStoreController({"j1": self.store_receipt()}),
                requirement_id="unit", job_id="j1",
                receipt={"outcome": "passed",
                         "cleanup": "verified_empty"})

    def test_store_pass_marks_observed_via_store_in_manifest(self):
        controller = FakeStoreController({"j1": self.store_receipt()})
        led = self.store_ledger()
        entry = led.observe_from_store(controller, requirement_id="unit",
                                       job_id="j1")
        self.assertEqual(entry.observed_via, "store")
        self.assertEqual(led.manifest()["checks"]["unit"]["observed_via"],
                         "store")

    def test_caller_pass_is_distinguishable_from_store_pass(self):
        led = self.store_ledger()
        entry = led.observe(requirement_id="unit", job_id="j2",
                            outcome="passed",
                            receipt={"outcome": "passed",
                                     "cleanup": "verified_empty"})
        # Caller-bound automated provenance is visible so a reviewer can
        # treat it as unverified and demand store re-observation.
        self.assertEqual(entry.observed_via, "caller")
        self.assertEqual(led.manifest()["checks"]["unit"]["observed_via"],
                         "caller")

    def test_observe_remains_independent_of_store_binding(self):
        # The observe() mechanics are unchanged: manual declarations and
        # caller-provided receipts still record exactly as before. Only the
        # completion policy changed: a caller-supplied automated pass is
        # completion-inert (visible with its provenance, not counted).
        led = self.store_ledger()
        entry = led.observe(requirement_id="unit", job_id="j2",
                            outcome="passed",
                            receipt={"outcome": "passed",
                                     "cleanup": "verified_empty"})
        self.assertEqual(entry.state, "passed")
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["observed_via"], "caller")


class ManifestProvenanceTests(unittest.TestCase):
    """P1-3: an automated requirement counts toward manifest complete ONLY
    on a pass whose receipt the controller itself fetched (observed_via
    == 'store'); a caller-supplied pass stays visible but is inert."""

    def setUp(self):
        self.led = VerificationLedger(
            [Requirement("unit", "aflow.web.unit", scope="full")])

    def test_forged_caller_pass_is_visible_but_not_counted(self):
        entry = self.led.observe(requirement_id="unit", job_id="j1",
                                 outcome="passed",
                                 receipt={"outcome": "passed",
                                          "cleanup": "verified_empty"})
        self.assertEqual(entry.state, "passed")
        manifest = self.led.manifest()
        self.assertFalse(manifest["complete"],
                         "a caller-supplied automated pass must not complete")
        check = manifest["checks"]["unit"]
        self.assertEqual(check["state"], "passed")
        self.assertEqual(check["observed_via"], "caller")
        self.assertEqual(check["receipt_outcome"], "passed")

    def test_store_observance_of_same_requirement_completes(self):
        self.led.observe(requirement_id="unit", job_id="j1", outcome="passed",
                         receipt={"outcome": "passed",
                                  "cleanup": "verified_empty"})
        self.assertFalse(self.led.manifest()["complete"])
        controller = FakeStoreController({
            "j1": {"job_id": "j1", "operation_id": "aflow.web.unit",
                   "outcome": "passed", "cleanup": "verified_empty"}})
        entry = self.led.observe_from_store(controller,
                                            requirement_id="unit",
                                            job_id="j1")
        self.assertEqual(entry.observed_via, "store")
        manifest = self.led.manifest()
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["observed_via"], "store")

    def test_note_documents_store_only_completion(self):
        note = self.led.manifest()["note"]
        self.assertIn("observed_via='store'", note)
        self.assertIn("does not complete", note)


class RealControllerRunningTests(unittest.TestCase):
    """Round-3 P1: the REAL JobController running payload (which carries no
    operation_id) must observe as running, not raise a spurious
    operation-mismatch ValueError like the old fake masked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.controller = JobController(self.root / "store.sqlite",
                                        evidence_root=self.root / "evidence",
                                        backend=self.backend)

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_live_job_observed_running_without_operation_id(self):
        launched = self.controller.launch(["/bin/true"], request_id="r1",
                                          operation_id="aflow.web.unit")
        self.assertFalse(launched["replayed"])
        led = VerificationLedger(
            [Requirement("unit", "aflow.web.unit", scope="full")])
        entry = led.observe_from_store(self.controller,
                                       requirement_id="unit",
                                       job_id=launched["job_id"])
        self.assertEqual(entry.state, "running")
        self.assertEqual(entry.observed_via, "store")
        self.assertNotIn("operation_id", entry.receipt)
        manifest = led.manifest()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["checks"]["unit"]["state"], "running")


class FakeStoreController:
    """Minimal duck-typed JobController for store-binding tests."""

    def __init__(self, receipts: dict):
        self.receipts = receipts
        self.requested: list[str] = []

    def status(self, job_id: str) -> dict:
        self.requested.append(job_id)
        if job_id not in self.receipts:
            raise KeyError(f"unknown job: {job_id}")
        return dict(self.receipts[job_id])


class CrossSessionController(FakeStoreController):
    """Stands in for the real JobAccessError path (a RuntimeError)."""

    def status(self, job_id: str) -> dict:
        raise RuntimeError("job belongs to a different session")


class StoreStampForgeryTests(unittest.TestCase):
    """Round-4 review P3 / P1-4: observe() must refuse to stamp 'store'
    provenance on caller-supplied receipts in EVERY argument shape; only
    observe_from_store() may, via the private _record() core."""

    def setUp(self):
        self.led = ledger()

    def test_caller_cannot_stamp_store_provenance(self):
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id="j1",
                             outcome="passed",
                             receipt={"outcome": "passed",
                                      "cleanup": "verified_empty"},
                             observed_via="store")

    def test_store_stamp_kwarg_no_longer_exists(self):
        signature = inspect.signature(VerificationLedger.observe)
        self.assertNotIn("_store_stamp", signature.parameters)

    def test_forged_store_stamp_kwarg_raises(self):
        # The old forgery shape (observed_via="store", _store_stamp=True)
        # must fail now: the parameter is gone (TypeError) or the provenance
        # is refused (ValueError).
        with self.assertRaises((TypeError, ValueError)):
            self.led.observe(requirement_id="unit", job_id="j1",
                             outcome="passed",
                             receipt={"outcome": "passed",
                                      "cleanup": "verified_empty"},
                             observed_via="store", _store_stamp=True)

    def test_store_rejected_for_every_observe_argument_shape(self):
        valid_receipt = {"outcome": "passed", "cleanup": "verified_empty"}
        shapes = [
            {"requirement_id": "unit", "job_id": "j1", "outcome": "passed",
             "observed_via": "store"},
            {"requirement_id": "unit", "job_id": "j1", "outcome": "passed",
             "receipt": valid_receipt, "observed_via": "store"},
            {"requirement_id": "unit", "job_id": "j1", "outcome": "passed",
             "receipt": valid_receipt, "input_fingerprint": "abc",
             "wall": 1.0, "observed_via": "store"},
            {"requirement_id": "unit", "job_id": "j1", "outcome": "failed",
             "receipt": {"outcome": "failed"}, "observed_via": "store"},
            {"requirement_id": "unit", "job_id": None, "outcome": "running",
             "observed_via": "store"},
            {"requirement_id": "visual", "job_id": None, "outcome": "passed",
             "observed_via": "store"},  # manual requirement: still refused
        ]
        for kwargs in shapes:
            with self.subTest(shape=sorted(kwargs)):
                with self.assertRaises(ValueError):
                    self.led.observe(**kwargs)
        # No forged attempt may leave ANY entry behind, let alone a
        # store-stamped one.
        self.assertNotIn("unit", self.led.entries)
        self.assertNotIn("visual", self.led.entries)
        self.assertFalse(any(entry.observed_via == "store"
                             for entry in self.led.entries.values()))

    def test_unknown_observed_via_still_rejected(self):
        with self.assertRaises(ValueError):
            self.led.observe(requirement_id="unit", job_id="j1",
                             outcome="failed", observed_via="whisper")

    def test_observe_from_store_still_stamps_store(self):
        class Ctl:
            def status(self, job_id):
                return {"job_id": job_id, "operation_id": "aflow.web.unit",
                        "outcome": "passed", "cleanup": "verified_empty"}
        entry = self.led.observe_from_store(Ctl(), requirement_id="unit",
                                            job_id="j1")
        self.assertEqual(entry.observed_via, "store")
        self.assertEqual(
            self.led.manifest()["checks"]["unit"]["observed_via"], "store")


if __name__ == "__main__":
    unittest.main()
