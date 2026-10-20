"""Session-bound job controller over the native systemd backend.

Contract highlights (from the plan's Design B and the F1 recipe):

- request-ID idempotency: replaying a (session, request_id) launch returns the
  original job and never starts a second unit (semantic coalescing stays off);
- the controller is an observer: systemd owns lifetime, deadlines and cleanup;
- terminal truth requires the ExecStopPost exit record; a collected unit
  without one is `inconclusive`, never success;
- `wait` never cancels or extends work; an elapsed wait returns elapsed;
- events are delivered at-least-once and deduplicated by (job, revision);
  unchanged status never wakes a waiter;
- cross-session access is rejected.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import linux_jobs
from .store import Store, DuplicateRequest


class JobError(RuntimeError):
    pass


class JobAccessError(JobError):
    pass


class UnknownJob(JobError):
    pass


TERMINAL_STATES = {"completed"}
# A launch reservation is "abandoned" only after this window; an in-flight
# systemd-run is never raced into a blocked finalization.
LAUNCH_LEASE_SECONDS = 120


class JobController:
    def __init__(self, store_path: Path, *, evidence_root: Path,
                 session_id: str | None = None, backend=linux_jobs):
        self.session_id = session_id or ("session-" + uuid.uuid4().hex[:12])
        self.evidence_root = Path(evidence_root)
        self.store = Store(store_path)
        self.backend = backend
        self.stop_reasons: dict[str, str] = {}

    def close(self) -> None:
        self.store.close()

    # -- lifecycle ----------------------------------------------------------
    def launch(self, argv: list[str], *, request_id: str, operation_id: str,
               deadline_seconds: int = 120, stop_grace_seconds: int = 5,
               env: dict[str, str] | None = None, profile_id: str | None = None,
               purpose: str = "verify", cwd: str | None = None) -> dict:
        if not request_id:
            raise JobError("request_id is required for idempotent launch")
        existing = self.store.find_by_request(self.session_id, request_id)
        if existing:
            return {"job_id": existing["job_id"], "replayed": True,
                    "state": existing["state"]}
        job_dir = self.evidence_root / "jobs" / (self.session_id + "-" + request_id)
        spec = linux_jobs.JobSpec(
            job_id="pending", argv=argv, job_dir=job_dir,
            deadline_seconds=deadline_seconds, stop_grace_seconds=stop_grace_seconds,
            env=env or {}, purpose=purpose, operation_id=operation_id, cwd=cwd)
        # Reserve the row first so duplicate concurrent requests hit the unique
        # index; the real job id replaces the placeholder afterwards.
        try:
            job_id = self.store.create_job(
                session_id=self.session_id, request_id=request_id,
                operation_id=operation_id, profile_id=profile_id, purpose=purpose,
                unit="(unlaunched)", job_dir=job_dir, deadline_seconds=deadline_seconds,
                stop_grace_seconds=stop_grace_seconds)
        except DuplicateRequest:
            # A concurrent identical request reserved the row first; its
            # launch is authoritative. Replay the same job id.
            existing = self.store.find_by_request(self.session_id, request_id)
            return {"job_id": existing["job_id"], "replayed": True,
                    "state": existing["state"]}
        # Lease: a reservation is only "abandoned" (reconcilable to blocked)
        # after this window; an in-flight systemd-run is never raced.
        self.store.update_job(job_id, launch_lease_until=time.time()
                              + LAUNCH_LEASE_SECONDS)
        spec.job_id = job_id
        try:
            meta = self.backend.launch(spec)
        except Exception as error:
            # CAS: only finalize if no other writer already did.
            self.store.update_job_if_state(job_id, ("starting", "queued", "running"),
                                           state="completed", outcome="blocked",
                                           terminal_wall=time.time(),
                                           exit_record_json=None)
            self.store.record_event(job_id, "terminal",
                                    {"outcome": "blocked", "error": str(error)[:300]})
            raise JobError(f"launch failed: {error}") from error
        # Persist the launch argv WITHOUT environment values: the journal is
        # not a secret store, and reconcile does not need them.
        redacted = [a for a in meta["launch_argv"] if not a.startswith("--setenv=")]
        claimed = self.store.update_job_if_state(
            job_id, ("starting", "queued"), unit=meta["unit"],
            boot_id=meta["boot_id"], invocation_id=meta["invocation_id"],
            state="running", started_wall=meta["launch_wall"],
            launch_argv_json=json.dumps(redacted))
        if not claimed:
            row_now = self.store.get_job(job_id)
            if row_now is not None and row_now["unit"] != "(unlaunched)":
                # The row already references a unit (e.g. reconcile adopted
                # this very launch during the crash window): the unit is
                # owned by the journal — do NOT stop it, and do not fail.
                return {"job_id": job_id, "replayed": True,
                        "state": row_now["state"]}
            # Truly orphaned: nobody owns this unit. Stop it and fail loudly.
            try:
                self.backend.stop(spec)
            except Exception:  # noqa: BLE001 — best-effort orphan cleanup
                pass
            raise JobError("launch lost the state race; the job row was "
                           "finalized concurrently and the unit was stopped")
        self.store.record_event(job_id, "launched",
                                {"unit": meta["unit"], "invocation_id": meta["invocation_id"]})
        return {"job_id": job_id, "replayed": False, "state": "running"}

    def _spec_for(self, row: dict) -> linux_jobs.JobSpec:
        import json as _json
        job_dir = Path(row["job_dir"])
        unit = row["unit"]
        if unit.startswith(linux_jobs.UNIT_PREFIX + "-") and unit.endswith(".service"):
            job_id_from_unit = unit[len(linux_jobs.UNIT_PREFIX) + 1:-len(".service")]
        else:
            job_id_from_unit = row["job_id"]
        argv = []
        if row.get("launch_argv_json"):
            try:
                argv = _json.loads(row["launch_argv_json"])
            except _json.JSONDecodeError:
                argv = []
        return linux_jobs.JobSpec(
            job_id=job_id_from_unit, argv=argv or ["true"], job_dir=job_dir,
            deadline_seconds=row["deadline_seconds"] or 120,
            stop_grace_seconds=row["stop_grace_seconds"] or 5,
            purpose=row["purpose"], operation_id=row["operation_id"])

    def _require_owned(self, job_id: str) -> dict:
        row = self.store.get_job(job_id)
        if row is None:
            raise UnknownJob(job_id)
        if row["session_id"] != self.session_id:
            raise JobAccessError("job belongs to a different session")
        return row

    def status(self, job_id: str) -> dict:
        row = self._require_owned(job_id)
        if row["state"] in TERMINAL_STATES:
            return self._receipt(row)
        if row["unit"] == "(unlaunched)":
            # Reserved but launch not confirmed yet: there is no unit to
            # inspect. Reporting blocked here (not waiting) avoids racing an
            # in-flight launch into a wrong finalization; reconcile is the
            # authority for crash recovery of such rows.
            return {"job_id": job_id, "state": row["state"], "phase": "unlaunched",
                    "unit": row["unit"],
                    "note": "launch not confirmed; use reconcile to recover "
                            "if the launching controller died"}
        spec = self._spec_for(row)
        observation = self.backend.inspect(spec)
        if observation.get("load_state") == "unknown":
            # Transient manager communication failure: report, never finalize.
            self.store.record_event(job_id, "status", {"phase": "unknown"})
            return {"job_id": job_id, "state": row["state"], "phase": "unknown",
                    "unit": row["unit"]}
        if observation["phase"] == "terminal":
            return self._absorb_terminal(job_id, row, spec, observation)
        if observation.get("phase") == "inconclusive" and observation.get("ended"):
            # The unit is definitively collected but its exit evidence is
            # missing/corrupt: the job HAS ended. Finalize as inconclusive
            # (never success) and persist the reason.
            return self._finalize_inconclusive(job_id, observation)
        self.store.record_event(job_id, "status",
                                {"phase": observation["phase"]})
        return {"job_id": job_id, "state": row["state"],
                "phase": observation["phase"], "unit": row["unit"]}

    def _reconcile_unlaunched(self, job_id: str, row: dict) -> dict:
        """Recover a reservation whose launch outcome is unknown.

        A row with unit="(unlaunched)" may be (a) mid-launch in another
        thread, (b) a unit that ran and was collected before its identity was
        stored, or (c) genuinely abandoned. The lease protects (a); the
        deterministic unit name (xtx-exec-<job_id>) distinguishes (b) from
        (c) once the lease has expired.
        """
        lease = row.get("launch_lease_until") or 0
        if time.time() < lease:
            return {"reconciled": False,
                    "reason": "launch lease still active; not finalizing"}
        probe = self._spec_for(row)
        observation = self.backend.inspect(probe)
        if observation.get("phase") in ("running", "starting", "finishing",
                                        "terminal"):
            # The launch DID happen; adopt the unit and continue normally.
            claimed = self.store.update_job_if_state(
                job_id, ("starting", "queued"), unit=observation["unit"],
                state="running", started_wall=time.time())
            if not claimed:
                row = self.store.get_job(job_id)
                if row["state"] in TERMINAL_STATES:
                    return {"reconciled": True, "receipt": self._receipt(row)}
            refreshed = self.store.get_job(job_id)
            observation = self.backend.inspect(self._spec_for(refreshed))
            if observation["phase"] == "terminal":
                return {"reconciled": True, "receipt":
                        self._absorb_terminal(job_id, refreshed, probe, observation)}
            if observation.get("phase") == "inconclusive" and observation.get("ended"):
                return {"reconciled": True, "receipt":
                        self._finalize_inconclusive(job_id, observation)}
            return {"reconciled": False, "reason":
                    f"adopted launched unit; still {observation['phase']}"}
        if observation.get("load_state") == "unknown":
            return {"reconciled": False, "reason": "manager communication failed; retry later"}
        # Lease expired and the deterministic unit does not exist: the launch
        # never completed. CAS-finalize blocked.
        if self.store.update_job_if_state(job_id, ("starting", "queued"),
                                          state="completed", outcome="blocked",
                                          terminal_wall=time.time()):
            self.store.record_event(job_id, "reconciled",
                                    {"outcome": "blocked",
                                     "reason": "launch never confirmed "
                                               "(no unit after lease expiry)"})
            return {"reconciled": True, "receipt": self._receipt(self.store.get_job(job_id))}
        row = self.store.get_job(job_id)
        return {"reconciled": True, "receipt": self._receipt(row)}

    def _finalize_inconclusive(self, job_id: str, observation: dict) -> dict:
        import json as _json
        record = _json.dumps(observation.get("exit_record"))
        self.store.update_job_if_state(job_id, ("starting", "queued", "running"),
                                       state="completed", outcome="inconclusive",
                                       exit_record_json=record,
                                       cleanup=observation.get("cgroup", "unknown"),
                                       terminal_wall=time.time())
        self.store.record_event(job_id, "reconciled", {
            "outcome": "inconclusive", "cleanup": observation.get("cgroup"),
            "reason": observation.get("warning") or "missing usable exit evidence"})
        return self._receipt(self.store.get_job(job_id))

    def _absorb_terminal(self, job_id: str, row: dict, spec, observation) -> dict:
        verdict = self.backend.classify(
            observation, cancel_requested=bool(row["cancel_requested"]))
        receipt = {
            "job_id": job_id, "operation_id": row["operation_id"],
            "unit": row["unit"], "invocation_id": row["invocation_id"],
            "boot_id": row["boot_id"],
            "outcome": verdict["outcome"],
            "service_result": verdict["service_result"],
            "exit_code": verdict["exit_code"],
            "exit_status": verdict["exit_status"],
            "cleanup": verdict["cleanup"],
            "inconclusive_reason": verdict["inconclusive_reason"],
            "cancel_requested": bool(row["cancel_requested"]),
            "started_wall": row["started_wall"],
            "terminal_wall": time.time(),
            "freshness": "fresh",
        }
        import json as _json
        self.store.update_job_if_state(
            job_id, ("starting", "queued", "running"),
            state="completed", outcome=verdict["outcome"],
            exit_record_json=_json.dumps(observation.get("exit_record")),
            cleanup=verdict["cleanup"], terminal_wall=receipt["terminal_wall"])
        self.store.record_event(job_id, "terminal", {
            "outcome": verdict["outcome"], "cleanup": verdict["cleanup"],
            "service_result": verdict["service_result"],
            "exit_code": verdict["exit_code"], "exit_status": verdict["exit_status"]})
        return receipt

    def _receipt(self, row: dict) -> dict:
        import json as _json
        record = _json.loads(row["exit_record_json"]) if row["exit_record_json"] else None
        return {
            "job_id": row["job_id"], "operation_id": row["operation_id"],
            "request_id": row["request_id"],
            "unit": row["unit"], "invocation_id": row["invocation_id"],
            "boot_id": row["boot_id"], "outcome": row["outcome"],
            "service_result": (record or {}).get("service_result"),
            "exit_code": (record or {}).get("exit_code"),
            "exit_status": (record or {}).get("exit_status"),
            "cleanup": row["cleanup"],
            "inconclusive_reason": verdict_reason(row),
            "cancel_requested": bool(row["cancel_requested"]),
            "started_wall": row["started_wall"],
            "terminal_wall": row["terminal_wall"],
            "freshness": "fresh",
        }

    def wait(self, job_id: str, *, after_revision: int | None = None,
             max_wait_seconds: float = 30.0) -> dict:
        """Block until a revision change, terminal state, or elapsed wait.

        An elapsed wait returns {"elapsed": true} and cancels nothing. The
        loop observes the backend (absorbing terminal truth) and the shared
        event store; unchanged status events (NULL revision) never satisfy a
        revision wait.
        """
        self._require_owned(job_id)
        deadline = time.monotonic() + max_wait_seconds
        last_seq = 0
        while True:
            for event in self.store.events_after(job_id, last_seq):
                last_seq = max(last_seq, event["seq"])
                if event["kind"] in ("terminal", "cancelled", "reconciled"):
                    current = self.store.get_job(job_id)
                    receipt = (self._receipt(current)
                               if current["state"] in TERMINAL_STATES else None)
                    return {"elapsed": False, "event": _event_public(event),
                            "receipt": receipt or self.status(job_id)}
                if (after_revision is not None and event["revision"] is not None
                        and event["revision"] > after_revision):
                    return {"elapsed": False, "event": _event_public(event),
                            "receipt": None}
            current = self.store.get_job(job_id)
            if current["state"] in TERMINAL_STATES:
                return {"elapsed": False, "event": None,
                        "receipt": self._receipt(current)}
            if current["unit"] != "(unlaunched)":
                spec = self._spec_for(current)
                observation = self.backend.inspect(spec)
                if observation["phase"] == "terminal":
                    receipt = self._absorb_terminal(job_id, current, spec, observation)
                    return {"elapsed": False, "event": None, "receipt": receipt}
                if (observation.get("phase") == "inconclusive"
                        and observation.get("ended")):
                    receipt = self._finalize_inconclusive(job_id, observation)
                    return {"elapsed": False, "event": None, "receipt": receipt}
                self.store.record_event(job_id, "status", {"phase": observation["phase"]})
            if time.monotonic() >= deadline:
                return {"elapsed": True, "event": None, "receipt": None,
                        "observed_seq": last_seq}
            time.sleep(0.25)

    def events(self, job_id: str, *, after_seq: int = 0) -> dict:
        self._require_owned(job_id)
        rows = self.store.events_after(job_id, after_seq)
        return {"events": [_event_public(r) for r in rows],
                "next_seq": max((r["seq"] for r in rows), default=after_seq)}

    def logs(self, job_id: str, *, after_cursor: str | None = None,
             max_bytes: int = 32768) -> dict:
        row = self._require_owned(job_id)
        if row["state"] in TERMINAL_STATES and row["unit"] == "(unlaunched)":
            raise JobError("job never launched")
        spec = self._spec_for(row)
        return self.backend.read_logs(spec, after_cursor=after_cursor,
                                      max_bytes=max_bytes)

    def cancel(self, job_id: str, reason: str = "requested") -> dict:
        row = self._require_owned(job_id)
        if row["state"] in TERMINAL_STATES:
            return {"cancelled": False, "reason": "already terminal",
                    "outcome": row["outcome"]}
        self.stop_reasons[job_id] = reason
        self.store.update_job(job_id, cancel_requested=1)
        spec = self._spec_for(row)
        result = self.backend.stop(spec)
        self.store.record_event(job_id, "cancel_requested",
                                {"reason": reason, **result})
        return {"cancelled": True, "reason": reason, "stop": result}

    def reconcile(self, job_id: str) -> dict:
        row = self._require_owned(job_id)
        if row["state"] in TERMINAL_STATES:
            return {"reconciled": False, "reason": "already terminal",
                    "receipt": self._receipt(row)}
        if row["unit"] == "(unlaunched)":
            return self._reconcile_unlaunched(job_id, row)
        spec = self._spec_for(row)
        outcome = self.backend.reconcile(spec,
                                         cancel_requested=bool(row["cancel_requested"]))
        observation = outcome["observation"]
        if observation.get("load_state") == "unknown":
            self.store.record_event(job_id, "status",
                                    {"phase": "unknown", "reconciled": True})
            return {"reconciled": False, "reason": "manager communication failed; retry later"}
        if observation["phase"] == "terminal":
            return {"reconciled": True, "receipt":
                    self._absorb_terminal(job_id, row, spec, observation)}
        if observation.get("phase") == "inconclusive" and observation.get("ended"):
            return {"reconciled": True, "receipt":
                    self._finalize_inconclusive(job_id, observation)}
        if row["cancel_requested"] and observation.get("phase") == "inconclusive":
            self.store.update_job(job_id, state="completed", outcome="inconclusive",
                                  cleanup=observation.get("cgroup", "unknown"),
                                  terminal_wall=time.time())
            self.store.record_event(job_id, "reconciled",
                                    {"outcome": "inconclusive"})
            return {"reconciled": True, "receipt": self._receipt(self.store.get_job(job_id))}
        self.store.record_event(job_id, "status",
                                {"phase": observation["phase"], "reconciled": True})
        return {"reconciled": False, "reason": f"still {observation['phase']}"}

    def reconcile_all(self) -> list[dict]:
        results = []
        for row in self.store.open_jobs():
            if row["session_id"] != self.session_id:
                continue
            try:
                results.append(self.reconcile(row["job_id"]))
            except JobError as error:
                results.append({"job_id": row["job_id"], "error": str(error)})
        return results


def verdict_reason(row: dict) -> str | None:
    """Best-effort persistent reason for inconclusive/blocked outcomes."""
    if row.get("outcome") not in ("inconclusive", "blocked"):
        return None
    import json as _json
    record = _json.loads(row["exit_record_json"]) if row["exit_record_json"] else None
    if record and record.get("corrupt"):
        return "corrupt exit record"
    if row.get("outcome") == "blocked":
        return "launch never confirmed"
    return "missing usable exit evidence"


def _event_public(row: dict) -> dict:
    import json as _json
    return {"seq": row["seq"], "revision": row["revision"], "kind": row["kind"],
            "wakeup": row["revision"] is not None,
            "payload": _json.loads(row["payload_json"] or "{}"),
            "created_wall": row["created_wall"]}
