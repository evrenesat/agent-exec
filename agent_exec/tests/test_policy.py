"""Unit tests for the deterministic recovery policy (Design C, first version)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Running via `python3 -m unittest` from the project root puts the root on
# sys.path; resolve the package through its full path to avoid a top-level
# module name collision with benchmarks.opencode or stdlib names.
sys.path.insert(0, str(HERE.parents[1]))

from agent_exec.policy import (  # noqa: E402
    LAUNCH_PREVENTING, LaunchRequest, PolicyError, RecoveryPolicy)


def req(**overrides) -> LaunchRequest:
    base = dict(
        request_id="r1", project="aflow", component="web", operation="unit",
        operation_family="unit", variant="default", purpose="verify",
        selectors=(), intended_scope="full-suite",
        source_fingerprint="meta-src-A",
        relevant_inputs_fingerprint="meta-rel-A",
        environment_fingerprint="meta-env-1", environment_class="p100-web",
        test_seed=None, semantic_flags=(), parent_job_id=None,
        registered_recovery_variant=False, wrapper_tags=(),
        source_snapshot=None)
    base.update(overrides)
    return LaunchRequest(**base)


class DefaultsAndRecordTests(unittest.TestCase):
    def test_default_flags_are_off_and_observe(self):
        policy = RecoveryPolicy()
        self.assertFalse(policy.coalescing)
        self.assertFalse(policy.reuse)
        self.assertEqual(policy.enforcement, "observe")
        with self.assertRaises(PolicyError):
            RecoveryPolicy(enforcement="polite")

    def test_every_decision_carries_the_full_record(self):
        decision = RecoveryPolicy().evaluate_launch(req())
        for name in ("prior_job_ids", "lineage", "input_evidence",
                     "reason_code", "proposed_action", "actual_action",
                     "limitations"):
            self.assertTrue(hasattr(decision, name), name)
        self.assertEqual(decision.reason_code, "fresh_execution")
        self.assertEqual(decision.actual_action, "launch_new")
        self.assertFalse(decision.prevents_launch())
        self.assertIn("meta-src-A", decision.input_evidence["source_fingerprint"])
        self.assertTrue(decision.job_id)
        self.assertTrue(decision.execution_key)
        self.assertTrue(decision.goal_key)
        self.assertEqual(decision.to_dict()["mode"], "observe")

    def test_malformed_requests_rejected(self):
        policy = RecoveryPolicy()
        with self.assertRaises(PolicyError):
            policy.evaluate_launch(req(request_id=""))
        with self.assertRaises(PolicyError):
            policy.evaluate_launch(req(purpose="ship"))


class IdempotencyAndCoalescingTests(unittest.TestCase):
    def test_request_retransmission_is_idempotent_never_a_block(self):
        policy = RecoveryPolicy()
        first = policy.evaluate_launch(req())
        second = policy.evaluate_launch(req(request_id="r1"))
        self.assertEqual(second.reason_code, "request_retransmission")
        self.assertEqual(second.actual_action, "return_existing_job")
        self.assertEqual(second.job_id, first.job_id)
        self.assertIn(first.job_id, second.prior_job_ids)
        self.assertFalse(second.prevents_launch())
        self.assertEqual(len(policy.jobs), 1)

    def test_duplicate_running_without_failure_evidence_abstains(self):
        # Empty log twice while the job runs: no proof of a loop or stall.
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.note_log_observation("r1", 0)
        second = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(second.reason_code,
                         "duplicate_running_no_failure_evidence")
        self.assertTrue(second.is_abstention())
        self.assertEqual(second.actual_action, "launch_new")
        self.assertFalse(second.prevents_launch())
        self.assertEqual(len(policy.jobs), 2)

    def test_coalescing_enabled_merges_identical_in_flight_work(self):
        policy = RecoveryPolicy(coalescing=True)
        first = policy.evaluate_launch(req())
        merged = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(merged.reason_code, "in_flight_coalesced")
        self.assertEqual(merged.actual_action, "return_existing_job")
        self.assertEqual(merged.job_id, first.job_id)
        self.assertEqual(len(policy.jobs), 1)

    def test_coalescing_disabled_by_default_even_for_identical_work(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        merged = policy.evaluate_launch(req(request_id="r2"))
        self.assertNotEqual(merged.reason_code, "in_flight_coalesced")

    def test_unknown_inputs_disable_coalescing_and_restriction(self):
        policy = RecoveryPolicy(coalescing=True)
        first = policy.evaluate_launch(req(request_id="r1",
                                           source_fingerprint="unknown",
                                           relevant_inputs_fingerprint="unknown"))
        second = policy.evaluate_launch(req(request_id="r2",
                                            source_fingerprint="unknown",
                                            relevant_inputs_fingerprint="unknown"))
        self.assertEqual(first.reason_code, "unknown_input_state_abstention")
        self.assertEqual(second.reason_code, "unknown_input_state_abstention")
        self.assertEqual(len(policy.jobs), 2)


class ReuseTests(unittest.TestCase):
    def test_reuse_off_by_default_completed_pass_runs_fresh(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="passed")
        again = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(again.reason_code, "fresh_execution")
        self.assertEqual(again.actual_action, "launch_new")

    def test_reuse_enabled_returns_labeled_reused_receipt(self):
        policy = RecoveryPolicy(reuse=True)
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="passed")
        again = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(again.reason_code, "completed_pass_reused")
        self.assertEqual(again.actual_action, "return_reused_receipt")
        self.assertIn("reused", again.limitations)

    def test_concurrent_edit_stales_the_result_and_blocks_reuse(self):
        policy = RecoveryPolicy(reuse=True)
        policy.evaluate_launch(req(source_snapshot={"status": "ok"}))
        policy.report_completion("r1", outcome="passed", concurrent_edit=True)
        self.assertTrue(policy.get_job("r1").stale)
        again = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(again.reason_code, "stale_result_abstention")
        self.assertEqual(again.actual_action, "launch_new")
        self.assertNotEqual(again.actual_action, "return_reused_receipt")

    def test_post_fingerprint_mismatch_stales_without_explicit_flag(self):
        policy = RecoveryPolicy(reuse=True)
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="passed",
                                 post_source_fingerprint="meta-src-B")
        again = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(again.reason_code, "stale_result_abstention")

    def test_external_state_uncertainty_declines_reuse(self):
        policy = RecoveryPolicy(reuse=True)
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="passed")
        again = policy.evaluate_launch(
            req(request_id="r2", semantic_flags=("external_state",)))
        self.assertEqual(again.reason_code,
                         "external_state_uncertainty_abstention")
        self.assertEqual(again.actual_action, "launch_new")


class RecoveryBudgetTests(unittest.TestCase):
    def test_first_timeout_allows_exactly_one_unchanged_recovery(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        retry = policy.evaluate_launch(req(request_id="r2"))
        self.assertEqual(retry.reason_code, "recovery_attempt")
        self.assertEqual(retry.actual_action, "launch_new")
        self.assertIn("job-0001", retry.prior_job_ids)
        policy.report_completion("r2", outcome="timed_out")
        third = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(third.reason_code, "recovery_budget_exhausted")
        self.assertEqual(third.proposed_action, "block")
        self.assertEqual(policy.task_recovery_attempts, 1)

    def test_observe_mode_proposes_but_launches(self):
        policy = RecoveryPolicy(enforcement="observe")
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        policy.evaluate_launch(req(request_id="r2"))
        policy.report_completion("r2", outcome="timed_out")
        third = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(third.proposed_action, "block")
        self.assertEqual(third.actual_action, "launch_new")
        self.assertNotEqual(third.job_id, "(no job launched)")
        self.assertIn("observe mode", third.limitations)

    def test_enforce_mode_blocks_and_launches_nothing(self):
        policy = RecoveryPolicy(enforcement="enforce")
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        policy.evaluate_launch(req(request_id="r2"))
        policy.report_completion("r2", outcome="timed_out")
        third = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(third.proposed_action, "block")
        self.assertEqual(third.actual_action, "block")
        self.assertEqual(third.job_id, "(no job launched)")
        self.assertTrue(third.prevents_launch())

    def test_wrapper_change_is_not_progress_and_does_not_reset_budget(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req(wrapper_tags=("log-filter:tail",)))
        policy.report_completion("r1", outcome="timed_out")
        retry = policy.evaluate_launch(req(request_id="r2",
                                           wrapper_tags=("log-filter:grep",)))
        self.assertEqual(retry.reason_code, "recovery_attempt")
        # Same execution key: the wrapper is not part of any identity.
        self.assertEqual(retry.execution_key,
                         policy.jobs["job-0001"].execution_key)
        policy.report_completion("r2", outcome="timed_out")
        third = policy.evaluate_launch(req(request_id="r3",
                                           wrapper_tags=("nohup",)))
        self.assertEqual(third.reason_code, "recovery_budget_exhausted")
        self.assertEqual(third.goal_key, retry.goal_key)

    def test_registered_alternative_is_distinct_execution_same_goal(self):
        # singleFork -> isolated one-worker forks: same goal, meaningfully
        # different execution; the first variant's result is never reused.
        policy = RecoveryPolicy()
        policy.evaluate_launch(req(variant="singleFork"))
        policy.report_completion("r1", outcome="timed_out")
        alt = policy.evaluate_launch(
            req(request_id="r2", variant="one-worker-forks",
                registered_recovery_variant=True))
        self.assertEqual(alt.reason_code, "registered_alternative")
        self.assertNotEqual(alt.execution_key, policy.jobs["job-0001"].execution_key)
        self.assertEqual(alt.goal_key, policy.jobs["job-0001"].goal_key)
        self.assertNotIn("return_reused_receipt",
                         (alt.proposed_action, alt.actual_action))

    def test_unregistered_variant_is_proposed_blocked(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        rogue = policy.evaluate_launch(req(request_id="r2", variant="danger"))
        self.assertEqual(rogue.reason_code, "unregistered_recovery_variant")
        self.assertEqual(rogue.proposed_action, "block")
        # Same-variant clean retry stays allowed and shares the budget.
        retry = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(retry.reason_code, "recovery_attempt")

    def test_second_timeout_in_lineage_exhausts_despite_file_changes(self):
        # A focused diagnostic's timeout counts into the family lineage;
        # after two timeouts the unchanged full goal is blocked even though
        # no full recovery attempt was ever consumed.
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        child = policy.evaluate_launch(
            req(request_id="r2", intended_scope="focused:src/app.test.ts",
                purpose="diagnose", parent_job_id="job-0001"))
        self.assertEqual(child.reason_code, "fresh_execution")
        self.assertNotEqual(child.goal_key, policy.jobs["job-0001"].goal_key)
        self.assertEqual(child.lineage["association"], "explicit")
        self.assertTrue(child.lineage["validated"])
        policy.report_completion("r2", outcome="timed_out")
        retry = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(retry.reason_code, "recovery_budget_exhausted")
        self.assertEqual(policy.task_recovery_attempts, 0)

    def test_successful_focused_diagnostic_keeps_full_budget_usable(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        child = policy.evaluate_launch(
            req(request_id="r2", intended_scope="focused:src/a.test.ts",
                purpose="diagnose"))
        policy.report_completion("r2", outcome="passed")
        retry = policy.evaluate_launch(req(request_id="r3"))
        self.assertEqual(retry.reason_code, "recovery_attempt")


class AssertionFailureTests(unittest.TestCase):
    def test_readme_only_edit_does_not_reset_a_code_profile(self):
        # The whole-tree fingerprint changed, but the profile-declared
        # relevant-input generation did not: no reset.
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="failed",
                                 failure_class="assertion")
        repeat = policy.evaluate_launch(
            req(request_id="r2", source_fingerprint="meta-src-readme"))
        self.assertEqual(repeat.reason_code, "repeat_without_new_evidence")
        self.assertEqual(repeat.proposed_action, "return_prior_failure_receipt")
        self.assertIn("return_prior_failure_receipt", LAUNCH_PREVENTING)

    def test_relevant_source_repair_starts_a_new_generation(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="failed",
                                 failure_class="assertion")
        repair = policy.evaluate_launch(
            req(request_id="r2", source_fingerprint="meta-src-B",
                relevant_inputs_fingerprint="meta-rel-B"))
        self.assertEqual(repair.reason_code, "fresh_execution")
        self.assertNotEqual(repair.goal_key, policy.jobs["job-0001"].goal_key)
        self.assertEqual(policy.task_recovery_attempts, 0)

    def test_confirm_flake_allowed_once_then_receipt_returned(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="failed",
                                 failure_class="assertion")
        flake = policy.evaluate_launch(req(request_id="r2",
                                           purpose="confirm_flake"))
        self.assertEqual(flake.reason_code, "confirm_flake_attempt")
        self.assertEqual(flake.actual_action, "launch_new")
        again = policy.evaluate_launch(req(request_id="r3",
                                           purpose="confirm_flake"))
        self.assertEqual(again.reason_code, "confirm_flake_already_used")
        self.assertEqual(again.proposed_action, "return_prior_failure_receipt")


class UnknownStateAndLineageTests(unittest.TestCase):
    def test_unknown_fingerprint_abstains_even_with_exhausted_budget(self):
        policy = RecoveryPolicy(enforcement="enforce")
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="timed_out")
        policy.evaluate_launch(req(request_id="r2"))
        policy.report_completion("r2", outcome="timed_out")
        unknown = policy.evaluate_launch(
            req(request_id="r3", source_fingerprint="unknown",
                relevant_inputs_fingerprint="unknown"))
        self.assertEqual(unknown.reason_code, "unknown_input_state_abstention")
        self.assertEqual(unknown.actual_action, "launch_new")
        self.assertFalse(unknown.prevents_launch())

    def test_unknown_parent_lineage_stays_advisory(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="failed",
                                 failure_class="assertion")
        child = policy.evaluate_launch(
            req(request_id="r2", intended_scope="focused:src/b.test.ts",
                purpose="diagnose", parent_job_id="job-9999"))
        self.assertFalse(child.lineage["validated"])
        self.assertEqual(child.lineage["association"], "none")
        self.assertEqual(child.actual_action, "launch_new")

    def test_auto_associated_child_never_counts_as_full_coverage(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        policy.report_completion("r1", outcome="failed",
                                 failure_class="assertion")
        child = policy.evaluate_launch(
            req(request_id="r2", intended_scope="focused:src/c.test.ts",
                purpose="diagnose"))
        self.assertEqual(child.lineage["association"], "auto")
        self.assertTrue(child.lineage["validated"])
        self.assertNotEqual(child.goal_key, policy.jobs["job-0001"].goal_key)


class CompletionValidationTests(unittest.TestCase):
    def test_completion_errors(self):
        policy = RecoveryPolicy()
        with self.assertRaises(PolicyError):
            policy.report_completion("missing", outcome="passed")
        policy.evaluate_launch(req())
        with self.assertRaises(PolicyError):
            policy.report_completion("r1", outcome="quantum")
        policy.report_completion("r1", outcome="passed")
        with self.assertRaises(PolicyError):
            policy.report_completion("r1", outcome="failed")

    def test_log_observations_recorded_without_budget_effects(self):
        policy = RecoveryPolicy()
        policy.evaluate_launch(req())
        job = policy.note_log_observation("r1", 0)
        self.assertEqual(job.log_observations, [0])
        policy.note_log_observation("r1", 0)
        self.assertEqual(policy.get_job("r1").log_observations, [0, 0])
        self.assertEqual(policy.task_recovery_attempts, 0)


if __name__ == "__main__":
    unittest.main()
