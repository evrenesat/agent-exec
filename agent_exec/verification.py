"""Requirement receipt ledger and final verification manifest.

The harness tracks which required checks a delivery still owes: each entry
binds an operation/scope to its latest valid receipt and exposes missing/
stale/running state. The supervisor validates receipt identity and freshness;
deciding WHICH checks prove a business requirement stays with the model/reviewer
and is declared here, never inferred. A manifest with unknown, failed, stale or
running required checks is incomplete — never a success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

STATES = ("missing", "running", "stale", "failed", "passed")


@dataclass
class Requirement:
    requirement_id: str
    operation_id: str
    scope: str = "full"
    description: str = ""
    manual: bool = False


@dataclass
class LedgerEntry:
    requirement_id: str
    operation_id: str
    scope: str
    state: str
    job_id: str | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    input_fingerprint: str | None = None
    observed_via: str = "caller"
    updated_wall: float = field(default_factory=time.time)


class VerificationLedger:
    """Track required checks and their freshest valid receipts.

    Callers MUST pass receipts obtained from JobController.status()/wait()
    (the trusted store); the ledger validates internal consistency — job id,
    operation id, outcome, cleanup — and refuses fabricated or borrowed
    receipt bodies for automated passes. It does not itself read the store;
    observe_from_store() is the stronger binding: it fetches the CURRENT
    receipt through the controller itself, so no caller-supplied receipt
    body can ever certify an automated pass.

    Provenance: every entry records ``observed_via`` — ``store``
    (observe_from_store(): the controller itself fetched the receipt),
    ``manual`` (a declared human acceptance), or ``caller`` (a
    caller-supplied receipt body passed to observe()). An automated
    ``passed`` with ``observed_via='caller'`` is valid only when the
    receipt genuinely came from a controller; reviewers should treat
    caller-bound automated passes as unverified provenance and prefer
    re-observation through observe_from_store().
    """

    def __init__(self, requirements: list[Requirement]):
        self.requirements = {r.requirement_id: r for r in requirements}
        self.entries: dict[str, LedgerEntry] = {}

    def observe(self, *, requirement_id: str, job_id: str | None, outcome: str,
                receipt: dict | None = None, input_fingerprint: str | None = None,
                wall: float | None = None,
                observed_via: str | None = None,
                _store_stamp: bool = False) -> LedgerEntry:
        if requirement_id not in self.requirements:
            raise KeyError(f"unknown requirement: {requirement_id!r}")
        if outcome not in STATES:
            raise ValueError(f"unknown outcome state: {outcome!r}")
        requirement = self.requirements[requirement_id]
        if observed_via is None:
            # Default provenance from the call shape: a manual requirement's
            # declared acceptance vs. a caller-supplied automated receipt.
            observed_via = "manual" if requirement.manual else "caller"
        elif observed_via not in ("store", "caller", "manual"):
            raise ValueError(f"unknown observed_via: {observed_via!r}")
        if observed_via == "store" and not _store_stamp:
            # 'store' provenance is attested by observe_from_store() actually
            # fetching the receipt from the controller; a caller cannot stamp
            # it onto a self-supplied receipt body.
            raise ValueError("observed_via='store' can only be set by "
                             "observe_from_store()")
        if outcome == "passed" and not requirement.manual:
            # An automated check passes only on the strength of a trusted job
            # receipt: a real job id plus recorded pass AND verified cleanup.
            # A caller-asserted status string is never sufficient.
            if not job_id:
                raise ValueError("automated pass requires a job id")
            receipt = receipt or {}
            if receipt.get("outcome") != "passed":
                raise ValueError("automated pass requires a receipt with "
                                 "outcome='passed'")
            if receipt.get("cleanup") != "verified_empty":
                raise ValueError("automated pass requires cleanup="
                                 "'verified_empty'")
            if receipt.get("job_id") not in (None, job_id):
                raise ValueError("receipt belongs to a different job "
                                 f"({receipt.get('job_id')} != {job_id})")
            expected_operation = requirement.operation_id
            if receipt.get("operation_id") not in (None, expected_operation):
                raise ValueError("receipt belongs to a different operation "
                                 f"({receipt.get('operation_id')} != "
                                 f"{expected_operation})")
        entry = LedgerEntry(
            requirement_id=requirement_id, operation_id=self.requirements[requirement_id].operation_id,
            scope=self.requirements[requirement_id].scope, state=outcome,
            job_id=job_id, receipt=receipt or {},
            input_fingerprint=input_fingerprint, observed_via=observed_via,
            updated_wall=wall if wall is not None else time.time())
        self.entries[requirement_id] = entry
        return entry

    def observe_from_store(self, controller, *, requirement_id: str, job_id: str,
                           input_fingerprint: str | None = None) -> LedgerEntry:
        """Observe a required check from the receipt in the trusted store.

        Fetches the CURRENT receipt for ``job_id`` through
        ``controller.status(job_id)`` (an agent_exec.jobs.JobController, or
        any duck-typed stand-in) and maps it to a ledger state. This method
        takes no receipt/outcome argument, so a fabricated receipt body can
        never certify a pass — only what the controller returns is trusted.

        Receipt -> state mapping (documented decision):
        - no terminal ``outcome`` field (running/unlaunched/unknown status
          payload) -> ``running``; these payloads carry no ``operation_id``,
          so the operation-id check is skipped for them — identity is the
          store-addressed job id alone;
        - ``outcome='passed'`` AND ``cleanup='verified_empty'`` ->
          ``passed`` (same standard as observe());
        - every other terminal outcome — ``failed``, ``timed_out``,
          ``cancelled``, ``blocked``, ``inconclusive``, or a claimed pass
          without verified-empty cleanup — maps to ``failed``. The ledger
          has no inconclusive state, and a required automated check that
          cannot prove a pass has failed to pass; the full receipt is
          retained on the entry, so the manifest's ``receipt_outcome``
          still shows the nuance (e.g. ``inconclusive``).

        Errors: a controller lookup failure (unknown job ``KeyError``,
        cross-session/ownership ``JobError``/``JobAccessError``, i.e. any
        ``RuntimeError`` subclass the controller raises) is re-raised as
        ``ValueError`` with the original exception chained, so callers can
        catch one type for store-access problems. A receipt whose
        ``job_id`` does not match the requested job raises ``ValueError``
        unconditionally; a mismatching ``operation_id`` raises
        ``ValueError`` for TERMINAL receipts. Non-terminal status payloads
        are exempt from the operation-id check (see the mapping below).
        Any other controller error propagates unchanged.
        """
        if requirement_id not in self.requirements:
            raise KeyError(f"unknown requirement: {requirement_id!r}")
        try:
            receipt = controller.status(job_id)
        except KeyError as exc:
            raise ValueError(f"job {job_id!r} is unknown to the "
                             f"controller: {exc}") from exc
        except RuntimeError as exc:
            raise ValueError(f"job {job_id!r} cannot be observed through "
                             f"this controller: {exc}") from exc
        receipt = dict(receipt or {})
        if receipt.get("job_id") != job_id:
            raise ValueError(f"store receipt is for a different job "
                             f"({receipt.get('job_id')!r} != {job_id!r})")
        requirement = self.requirements[requirement_id]
        outcome = receipt.get("outcome")
        if outcome is None:
            # Non-terminal status payload (running/unlaunched/unknown): the
            # real JobController.status() emits only job_id/state/phase/unit
            # — no operation_id — so identity here is the store-addressed
            # job id, which was just verified. The operation-id check below
            # applies only to terminal receipts, which always carry it.
            state = "running"
        else:
            if receipt.get("operation_id") != requirement.operation_id:
                raise ValueError("store receipt belongs to a different "
                                 f"operation ({receipt.get('operation_id')!r} "
                                 f"!= {requirement.operation_id!r})")
            if outcome == "passed" \
                    and receipt.get("cleanup") == "verified_empty":
                state = "passed"
            else:
                state = "failed"
        return self.observe(requirement_id=requirement_id, job_id=job_id,
                            outcome=state, receipt=receipt,
                            input_fingerprint=input_fingerprint,
                            observed_via="store", _store_stamp=True)

    def invalidate_stale(self, current_fingerprint: str | None) -> list[str]:
        """Mark passed receipts stale when the relevant inputs changed.

        Unknown current fingerprints invalidate nothing (advisory conservatism
        is a caller policy, not an automatic block).
        """
        if current_fingerprint is None:
            return []
        invalidated = []
        for requirement_id, entry in self.entries.items():
            if entry.state == "passed" and entry.input_fingerprint is not None \
                    and entry.input_fingerprint != current_fingerprint:
                entry.state = "stale"
                invalidated.append(requirement_id)
        return invalidated

    def manifest(self) -> dict:
        """Machine-readable delivery manifest; incomplete is never success."""
        checks = {}
        complete = True
        for requirement_id, requirement in self.requirements.items():
            entry = self.entries.get(requirement_id)
            state = entry.state if entry else "missing"
            if requirement.manual:
                # Manual acceptance is declared, never self-certified here.
                checks[requirement_id] = {"state": state, "manual": True,
                                          "operation_id": requirement.operation_id,
                                          "observed_via":
                                          entry.observed_via if entry else None}
                if state != "passed":
                    complete = False
                continue
            checks[requirement_id] = {
                "state": state, "manual": False,
                "operation_id": requirement.operation_id, "scope": requirement.scope,
                "job_id": entry.job_id if entry else None,
                "receipt_outcome": (entry.receipt or {}).get("outcome")
                if entry else None,
                "observed_via": entry.observed_via if entry else None,
                "input_fingerprint": entry.input_fingerprint if entry else None,
                "updated_wall": entry.updated_wall if entry else None,
            }
            if state != "passed":
                complete = False
        return {
            "schema_version": 1,
            "complete": complete,
            "checks": checks,
            "note": "complete=False is expected until every required check has a "
                    "fresh passing receipt; a complete manifest still requires "
                    "independent product review and cannot certify requirement "
                    "semantics by itself",
        }
