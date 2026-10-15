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
    updated_wall: float = field(default_factory=time.time)


class VerificationLedger:
    """Track required checks and their freshest valid receipts."""

    def __init__(self, requirements: list[Requirement]):
        self.requirements = {r.requirement_id: r for r in requirements}
        self.entries: dict[str, LedgerEntry] = {}

    def observe(self, *, requirement_id: str, job_id: str | None, outcome: str,
                receipt: dict | None = None, input_fingerprint: str | None = None,
                wall: float | None = None) -> LedgerEntry:
        if requirement_id not in self.requirements:
            raise KeyError(f"unknown requirement: {requirement_id!r}")
        if outcome not in STATES:
            raise ValueError(f"unknown outcome state: {outcome!r}")
        requirement = self.requirements[requirement_id]
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
        entry = LedgerEntry(
            requirement_id=requirement_id, operation_id=self.requirements[requirement_id].operation_id,
            scope=self.requirements[requirement_id].scope, state=outcome,
            job_id=job_id, receipt=receipt or {},
            input_fingerprint=input_fingerprint,
            updated_wall=wall if wall is not None else time.time())
        self.entries[requirement_id] = entry
        return entry

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
                                          "operation_id": requirement.operation_id}
                if state != "passed":
                    complete = False
                continue
            checks[requirement_id] = {
                "state": state, "manual": False,
                "operation_id": requirement.operation_id, "scope": requirement.scope,
                "job_id": entry.job_id if entry else None,
                "receipt_outcome": (entry.receipt or {}).get("outcome")
                if entry else None,
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
