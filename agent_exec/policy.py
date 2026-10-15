"""Deterministic recovery policy: observe/enforce modes, abstain on unknown.

Implements "Deterministic policy for the first version" (Design C):

- transport request-ID idempotency always applies and is never a block;
- exact in-flight coalescing only when explicitly enabled (default OFF):
  same execution key + running job => return the existing job id;
- completed-pass reuse only when explicitly enabled (default OFF), with
  complete known input identity, no stale result and no uncontrolled
  external state;
- first timeout/infrastructure failure => bounded diagnostics and registered
  recovery variants only; at most ONE additional full recovery attempt per
  unchanged recovery goal (a clean retry and a registered alternative both
  count; wrapper/presentation changes do not create a new attempt);
- a second timeout in the same validated lineage exhausts the budget for
  unchanged attempts regardless of selected-file changes; a verified
  relevant-input change starts a new recovery generation (new family budget)
  but does not reset the task-level budget; source repair and unrelated
  requirements stay allowed;
- assertion failures allow a relevant repair and one explicit confirm_flake;
  an unchanged identical repeat returns the prior failure receipt;
- unknown input state abstains: advisory only, never a hard block, never a
  reuse.

Every decision returns a DecisionRecord carrying prior job ids, lineage,
input evidence, reason code, proposed action, actual action and limitations.
In ``observe`` mode every restrictive proposal is recorded but the launch
proceeds (the job is still registered); ``enforce`` makes proposed blocks
actual and no job is created. This module is pure: decisions need no
processes, files, sockets or clocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import fingerprints
from .fingerprints import UNKNOWN

POLICY_VERSION = 1
ENFORCEMENT_MODES = ("observe", "enforce")
PURPOSES = ("verify", "diagnose", "confirm_flake")
EXTERNAL_STATE_FLAG = "external_state"

# Actions. "block" and "return_prior_failure_receipt" prevent a new launch.
ACTIONS = ("launch_new", "return_existing_job", "return_reused_receipt",
           "return_prior_failure_receipt", "block")
LAUNCH_PREVENTING = ("block", "return_prior_failure_receipt")

ABSTENTION_REASONS = frozenset({
    "unknown_input_state_abstention",
    "stale_result_abstention",
    "duplicate_running_no_failure_evidence",
    "external_state_uncertainty_abstention",
})


class PolicyError(ValueError):
    """A policy request is malformed or inconsistent with recorded state."""


@dataclass(frozen=True)
class LaunchRequest:
    """One supervisor-validated launch request (metadata only).

    ``source_fingerprint`` is the conservative whole-component identity;
    ``relevant_inputs_fingerprint`` is the profile-declared relevant subset
    whose verified change starts a new recovery generation. Wrapper and
    presentation details travel in ``wrapper_tags`` and deliberately enter
    neither key. ``registered_recovery_variant`` is supervisor-owned profile
    truth: this variant is an approved alternative for the same goal.
    """
    request_id: str
    project: str
    component: str
    operation: str
    operation_family: str
    variant: str = "default"
    purpose: str = "verify"
    selectors: tuple = ()
    intended_scope: str = "full"
    source_fingerprint: str = UNKNOWN
    relevant_inputs_fingerprint: str = UNKNOWN
    environment_fingerprint: str = UNKNOWN
    environment_class: str = "default"
    test_seed: Optional[str] = None
    semantic_flags: tuple = ()
    parent_job_id: Optional[str] = None
    registered_recovery_variant: bool = False
    wrapper_tags: tuple = ()
    source_snapshot: Optional[dict] = None


@dataclass
class DecisionRecord:
    """One deterministic decision with its full evidence trail."""
    reason_code: str
    proposed_action: str
    actual_action: str
    limitations: str
    prior_job_ids: list
    lineage: dict
    input_evidence: dict
    job_id: str
    execution_key: str
    goal_key: str
    family_key: str
    mode: str

    def prevents_launch(self) -> bool:
        return self.proposed_action in LAUNCH_PREVENTING

    def is_abstention(self) -> bool:
        return self.reason_code in ABSTENTION_REASONS

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class JobRecord:
    job_id: str
    request_id: str
    execution_key: str
    goal_key: str
    family_key: str
    variant: str
    purpose: str
    intended_scope: str
    state: str  # running | completed | failed | timed_out | cancelled
    outcome: Optional[str] = None
    failure_class: Optional[str] = None  # timeout | infra | assertion | None
    source_fingerprint: str = UNKNOWN
    relevant_inputs_fingerprint: str = UNKNOWN
    source_snapshot: Optional[dict] = None
    post_source_fingerprint: Optional[str] = None
    post_snapshot: Optional[dict] = None
    stale: bool = False
    parent_job_id: Optional[str] = None
    lineage: dict = field(default_factory=dict)
    wrapper_tags: tuple = ()
    log_observations: list = field(default_factory=list)


@dataclass
class _FamilyState:
    """Budget neighborhood: one goal family per verified input generation."""
    timeouts: list = field(default_factory=list)      # job ids, in order
    failed_goal_keys: set = field(default_factory=set)
    full_recovery_used: bool = False
    flake_confirmed: bool = False
    primary_failed_goal: Optional[str] = None


class RecoveryPolicy:
    """Deterministic launch decisions over in-memory job/family state."""

    def __init__(self, *, coalescing: bool = False, reuse: bool = False,
                 enforcement: str = "observe", policy_id: str = "policy-v1"):
        if enforcement not in ENFORCEMENT_MODES:
            raise PolicyError(f"unknown enforcement mode: {enforcement!r}")
        self.coalescing = bool(coalescing)
        self.reuse = bool(reuse)
        self.enforcement = enforcement
        self.policy_id = policy_id
        self.jobs: dict[str, JobRecord] = {}
        self._by_request: dict[str, str] = {}
        self._families: dict[str, _FamilyState] = {}
        self._counter = 0
        self.task_recovery_attempts = 0

    # -- public API --------------------------------------------------------

    def evaluate_launch(self, request: LaunchRequest) -> DecisionRecord:
        self._validate(request)
        prior_job_id = self._by_request.get(request.request_id)
        if prior_job_id is not None:
            prior = self.jobs[prior_job_id]
            return self._decision(
                request, exec_key=prior.execution_key, goal_key=prior.goal_key,
                family_key=prior.family_key,
                reason_code="request_retransmission",
                proposed_action="return_existing_job",
                actual_action="return_existing_job",
                limitations=("transport request-ID idempotency: a "
                             "retransmitted request returns the original job; "
                             "never a block and never a second process"),
                prior_job_ids=[prior_job_id], lineage=dict(prior.lineage),
                job_id=prior_job_id)

        inputs_known = (fingerprints.known_fingerprint(request.source_fingerprint)
                        and fingerprints.known_fingerprint(
                            request.relevant_inputs_fingerprint))
        if not inputs_known:
            # Unknown state: abstention with explicit UNKNOWN identities.
            # Never a hard block, never a reuse, never a guessed key.
            return self._launch(
                request, UNKNOWN, UNKNOWN, UNKNOWN,
                reason_code="unknown_input_state_abstention",
                limitations=("source/relevant-input fingerprints unknown: "
                             "reuse and similarity rejection disabled, "
                             "advisory only"))
        exec_key = fingerprints.execution_key(
            request.project, request.component, request.operation,
            request.variant, request.source_fingerprint,
            request.environment_fingerprint, request.selectors,
            request.semantic_flags, request.test_seed)
        goal_key = fingerprints.recovery_goal_key(
            request.project, request.component, request.operation_family,
            request.relevant_inputs_fingerprint, request.environment_class,
            request.intended_scope)
        family_key = fingerprints.recovery_family_key(
            request.project, request.component, request.operation_family,
            request.relevant_inputs_fingerprint, request.environment_class)
        family = self._families.get(family_key)

        # Exact in-flight coalescing (feature-flagged). It returns an
        # existing job id and never creates work, so it runs before gating.
        if self.coalescing:
            running = self._find_running(exec_key)
            if running is not None:
                return self._decision(
                    request, exec_key=exec_key, goal_key=goal_key,
                    family_key=family_key,
                    reason_code="in_flight_coalesced",
                    proposed_action="return_existing_job",
                    actual_action="return_existing_job",
                    limitations=("exact in-flight coalescing: identical "
                                 "execution key already running; subscribers "
                                 "share its result"),
                    prior_job_ids=[running.job_id],
                    lineage=dict(running.lineage), job_id=running.job_id)

        restriction = self._recovery_gate(request, family, goal_key,
                                          exec_key, family_key)
        if restriction is not None:
            return restriction

        # Duplicate of a still-running execution with coalescing off: no
        # failure evidence exists, so no proof of a loop or stall.
        duplicate = self._find_running(exec_key)
        if duplicate is not None:
            return self._launch(
                request, exec_key, goal_key, family_key,
                reason_code="duplicate_running_no_failure_evidence",
                limitations=("identical execution already running and "
                             "coalescing disabled; empty or unchanged logs "
                             "are not proof of a stall; awaiting a real "
                             "state/progress event"))

        reuse_decision = self._reuse_gate(request, exec_key, goal_key,
                                          family_key)
        if reuse_decision is not None:
            return reuse_decision

        return self._launch(request, exec_key, goal_key, family_key,
                            reason_code="fresh_execution",
                            limitations="no prior work in this recovery family")

    def report_completion(self, job_ref: str, *, outcome: str,
                          failure_class: Optional[str] = None,
                          post_source_fingerprint: Optional[str] = None,
                          post_snapshot: Optional[dict] = None,
                          concurrent_edit: bool = False) -> JobRecord:
        """Record a terminal outcome; derive staleness from pre/post evidence.

        ``job_ref`` is the request id or job id. A concurrent edit, a post
        fingerprint differing from the launch fingerprint, or a snapshot
        mismatch marks the result stale: ineligible for reuse and for final
        validation. A later edit cannot retroactively validate an earlier
        pass.
        """
        job = self._resolve(job_ref)
        if job.state != "running":
            raise PolicyError(f"job {job.job_id} already terminal: {job.state}")
        if outcome not in ("passed", "failed", "timed_out", "cancelled",
                           "inconclusive"):
            raise PolicyError(f"unknown outcome: {outcome!r}")
        if failure_class is None:
            failure_class = {"timed_out": "timeout", "failed": "assertion",
                             "inconclusive": "infra"}.get(outcome)
        stale = bool(concurrent_edit)
        if fingerprints.known_fingerprint(post_source_fingerprint) and \
                post_source_fingerprint != job.source_fingerprint:
            stale = True
        if job.source_snapshot is not None or post_snapshot is not None:
            if fingerprints.snapshot_changed(job.source_snapshot, post_snapshot):
                stale = True
        job.outcome = outcome
        job.failure_class = failure_class
        job.post_source_fingerprint = post_source_fingerprint
        job.post_snapshot = post_snapshot
        job.stale = stale
        job.state = {"passed": "completed", "failed": "failed",
                     "timed_out": "timed_out", "cancelled": "cancelled",
                     "inconclusive": "failed"}[outcome]
        family = self._families.setdefault(job.family_key, _FamilyState())
        if failure_class in ("timeout", "infra"):
            family.timeouts.append(job.job_id)
        if failure_class is not None:
            family.failed_goal_keys.add(job.goal_key)
            if family.primary_failed_goal is None:
                family.primary_failed_goal = job.goal_key
        return job

    def note_log_observation(self, job_ref: str, new_bytes: int) -> JobRecord:
        """Record bounded log evidence; never resets budgets or wakes anyone."""
        job = self._resolve(job_ref)
        job.log_observations.append(int(new_bytes))
        return job

    def get_job(self, job_ref: str) -> JobRecord:
        return self._resolve(job_ref)

    # -- gates ---------------------------------------------------------------

    def _recovery_gate(self, request: LaunchRequest,
                       family: Optional[_FamilyState], goal_key: str,
                       exec_key: str, family_key: str) -> Optional[DecisionRecord]:
        if family is None:
            return None
        timeout_count = len(family.timeouts)
        goal_failed_before = goal_key in family.failed_goal_keys
        if timeout_count == 0 and not goal_failed_before:
            return None

        if timeout_count >= 1:
            if not goal_failed_before:
                # Focused/diagnostic child of the same validated lineage:
                # stays allowed; its own timeout will count into the family.
                return None
            same_variant = any(
                self.jobs[jid].variant == request.variant
                for jid in self._family_job_ids(family))
            if not (request.registered_recovery_variant or same_variant):
                return self._restrict(
                    request, exec_key, goal_key, family_key,
                    reason_code="unregistered_recovery_variant",
                    limitation=("after a failure only registered recovery "
                                "variants or a clean retry are offered; this "
                                "variant is not registered for the goal"))
            if timeout_count >= 2 or family.full_recovery_used:
                return self._restrict(
                    request, exec_key, goal_key, family_key,
                    reason_code="recovery_budget_exhausted",
                    limitation=("a second timeout in the same validated "
                                "lineage exhausts the unchanged-goal budget "
                                "regardless of selected-file or wrapper "
                                "changes; source repair (verified "
                                "relevant-input change) and unrelated "
                                "requirements stay allowed"))
            family.full_recovery_used = True
            self.task_recovery_attempts += 1
            alternative = not same_variant
            return self._launch(
                request, exec_key, goal_key, family_key,
                reason_code="registered_alternative" if alternative
                else "recovery_attempt",
                limitations=self._join(
                    "the one unchanged-goal recovery attempt: a clean retry "
                    "and a registered alternative count against the same "
                    "budget, even if the wrapper changes",
                    "wrapper/presentation changes do not reset this budget"
                    if request.wrapper_tags else "",
                    f"task-level budget not reset "
                    f"({self.task_recovery_attempts} recovery attempts "
                    f"consumed overall)"))

        # Assertion-failure rule (separate from the timeout budget).
        if goal_failed_before:
            if request.purpose == "confirm_flake":
                if family.flake_confirmed:
                    return self._restrict(
                        request, exec_key, goal_key, family_key,
                        reason_code="confirm_flake_already_used",
                        limitation=("one explicit flakiness confirmation is "
                                    "allowed per unchanged goal; a later "
                                    "pass never erases the earlier failure"),
                        action="return_prior_failure_receipt")
                family.flake_confirmed = True
                return self._launch(
                    request, exec_key, goal_key, family_key,
                    reason_code="confirm_flake_attempt",
                    limitations=("bounded confirm_flake: both outcomes stay "
                                 "visible; acceptance follows the "
                                 "predeclared flaky-test policy"))
            return self._restrict(
                request, exec_key, goal_key, family_key,
                reason_code="repeat_without_new_evidence",
                limitation=("identical failed execution repeated without a "
                            "relevant repair or a flake check; returning the "
                            "prior failure receipt; a verified relevant-input "
                            "change begins a new generation and stays allowed"),
                action="return_prior_failure_receipt")
        return None

    def _reuse_gate(self, request: LaunchRequest, exec_key: str,
                    goal_key: str, family_key: str) -> Optional[DecisionRecord]:
        if not self.reuse:
            return None
        # Uncontrolled external state: complete input identity cannot be
        # established, so reuse is ineligible regardless of any match.
        if EXTERNAL_STATE_FLAG in request.semantic_flags:
            return self._launch(
                request, exec_key, goal_key, family_key,
                reason_code="external_state_uncertainty_abstention",
                limitations=("uncontrolled external state: complete input "
                             "identity cannot be established, reuse declined "
                             "rather than inventing equivalence"))
        match = None
        for job in self.jobs.values():
            if (job.execution_key == exec_key and job.state == "completed"
                    and job.outcome == "passed"):
                match = job
        if match is None:
            return None
        if match.stale:
            return self._launch(
                request, exec_key, goal_key, family_key,
                reason_code="stale_result_abstention",
                limitations=("prior result is stale: inputs changed during "
                             "that job, so reuse is ineligible and a fresh "
                             "run proceeds; a later edit cannot "
                             "retroactively validate an earlier pass"))
        return self._decision(
            request, exec_key=exec_key, goal_key=match.goal_key,
            family_key=family_key, reason_code="completed_pass_reused",
            proposed_action="return_reused_receipt",
            actual_action="return_reused_receipt",
            limitations=("receipt is labeled reused; final acceptance may "
                         "still require a fresh run"),
            prior_job_ids=[match.job_id], lineage=dict(match.lineage),
            job_id=match.job_id)

    # -- decision constructors ---------------------------------------------

    def _launch(self, request: LaunchRequest, exec_key: str, goal_key: str,
                family_key: str, *, reason_code: str,
                limitations: str) -> DecisionRecord:
        """Register and allow a launch (possibly with a recorded proposal)."""
        family = self._families.get(family_key)
        decision = self._decision(
            request, exec_key=exec_key, goal_key=goal_key,
            family_key=family_key, reason_code=reason_code,
            proposed_action="launch_new", actual_action="launch_new",
            limitations=limitations,
            prior_job_ids=self._family_prior_ids(family), lineage={},
            job_id=self._new_job(request, exec_key, goal_key, family_key))
        decision.lineage = self._lineage(request, family)
        self.jobs[decision.job_id].lineage = decision.lineage
        return decision

    def _restrict(self, request: LaunchRequest, exec_key: str, goal_key: str,
                  family_key: str, *, reason_code: str, limitation: str,
                  action: str = "block") -> DecisionRecord:
        """Propose a launch-preventing decision.

        enforce: no job is created. observe: the proposal is recorded and
        the launch proceeds, so the job is still registered.
        """
        family = self._families.get(family_key)
        if self.enforcement == "observe":
            decision = self._decision(
                request, exec_key=exec_key, goal_key=goal_key,
                family_key=family_key, reason_code=reason_code,
                proposed_action=action, actual_action="launch_new",
                limitations=self._join(
                    limitation,
                    "observe mode: restriction recorded as a proposal only; "
                    "the launch proceeds"),
                prior_job_ids=self._family_prior_ids(family), lineage={},
                job_id=self._new_job(request, exec_key, goal_key,
                                     family_key))
            decision.lineage = self._lineage(request, family)
            self.jobs[decision.job_id].lineage = decision.lineage
            return decision
        return self._decision(
            request, exec_key=exec_key, goal_key=goal_key,
            family_key=family_key, reason_code=reason_code,
            proposed_action=action, actual_action=action,
            limitations=self._join(limitation, "enforce mode: not launched"),
            prior_job_ids=self._family_prior_ids(family),
            lineage=self._lineage(request, family),
            job_id="(no job launched)")

    def _decision(self, request: LaunchRequest, *, exec_key: str,
                  goal_key: str, family_key: str, reason_code: str,
                  proposed_action: str, actual_action: str,
                  limitations: str, prior_job_ids: list, lineage: dict,
                  job_id: str) -> DecisionRecord:
        return DecisionRecord(
            reason_code=reason_code, proposed_action=proposed_action,
            actual_action=actual_action, limitations=limitations,
            prior_job_ids=list(prior_job_ids), lineage=lineage,
            input_evidence=self._input_evidence(request),
            job_id=job_id, execution_key=exec_key, goal_key=goal_key,
            family_key=family_key, mode=self.enforcement)

    # -- state helpers -------------------------------------------------------

    def _family_job_ids(self, family: _FamilyState) -> list:
        seen, ordered = set(), []
        for jid in family.timeouts:
            if jid not in seen:
                seen.add(jid)
                ordered.append(jid)
        for job in sorted(self.jobs.values(), key=lambda j: j.job_id):
            if (job.goal_key in family.failed_goal_keys
                    and job.job_id not in seen):
                seen.add(job.job_id)
                ordered.append(job.job_id)
        return ordered

    def _family_prior_ids(self, family: Optional[_FamilyState]) -> list:
        return self._family_job_ids(family) if family is not None else []

    def _find_running(self, exec_key: str) -> Optional[JobRecord]:
        for job in self.jobs.values():
            if job.execution_key == exec_key and job.state == "running":
                return job
        return None

    def _resolve(self, job_ref: str) -> JobRecord:
        job = self.jobs.get(job_ref)
        if job is None and job_ref in self._by_request:
            job = self.jobs.get(self._by_request[job_ref])
        if job is None:
            raise PolicyError(f"unknown job reference: {job_ref!r}")
        return job

    @staticmethod
    def _validate(request: LaunchRequest) -> None:
        for name in ("request_id", "project", "component", "operation",
                     "operation_family"):
            value = getattr(request, name)
            if not isinstance(value, str) or value == "":
                raise PolicyError(f"{name} must be a non-empty string")
        if request.purpose not in PURPOSES:
            raise PolicyError(f"unknown purpose: {request.purpose!r}")

    def _lineage(self, request: LaunchRequest,
                 family: Optional[_FamilyState]) -> dict:
        if request.parent_job_id is not None:
            parent = self.jobs.get(request.parent_job_id)
            if parent is None:
                return {"parent_job_id": request.parent_job_id,
                        "association": "none", "validated": False,
                        "note": "unknown parent id: lineage stays advisory"}
            return {"parent_job_id": parent.job_id,
                    "association": "explicit", "validated": True,
                    "root_goal_key": parent.goal_key}
        if family is not None and family.primary_failed_goal:
            return {"parent_job_id": None, "association": "auto",
                    "validated": True,
                    "root_goal_key": family.primary_failed_goal,
                    "note": ("requested during an active recovery in the "
                             "same family; a distinct execution that never "
                             "counts as coverage of the failed scope")}
        return {"parent_job_id": None, "association": "none",
                "validated": False}

    def _new_job(self, request: LaunchRequest, exec_key: str, goal_key: str,
                 family_key: str) -> str:
        self._counter += 1
        job_id = f"job-{self._counter:04d}"
        self.jobs[job_id] = JobRecord(
            job_id=job_id, request_id=request.request_id,
            execution_key=exec_key, goal_key=goal_key, family_key=family_key,
            variant=request.variant, purpose=request.purpose,
            intended_scope=request.intended_scope, state="running",
            source_fingerprint=request.source_fingerprint,
            relevant_inputs_fingerprint=request.relevant_inputs_fingerprint,
            source_snapshot=request.source_snapshot,
            parent_job_id=request.parent_job_id,
            wrapper_tags=tuple(request.wrapper_tags))
        self._by_request[request.request_id] = job_id
        return job_id

    @staticmethod
    def _input_evidence(request: LaunchRequest) -> dict:
        return {
            "source_fingerprint": request.source_fingerprint,
            "relevant_inputs_fingerprint": request.relevant_inputs_fingerprint,
            "environment_fingerprint": request.environment_fingerprint,
            "environment_class": request.environment_class,
            "test_seed": request.test_seed,
            "semantic_flags": sorted(request.semantic_flags),
            "known": (fingerprints.known_fingerprint(request.source_fingerprint)
                      and fingerprints.known_fingerprint(
                          request.relevant_inputs_fingerprint)),
            "snapshot_recorded": request.source_snapshot is not None,
        }

    @staticmethod
    def _join(*parts: str) -> str:
        return "; ".join(p for p in parts if p)
