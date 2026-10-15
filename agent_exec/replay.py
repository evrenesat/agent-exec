"""Offline replay of labeled recovery traces against the deterministic policy.

Reads a labeled, metadata-only fixture set (JSON files under
``agent_exec/tests/fixtures/recovery/``), replays each event
sequence through :class:`~agent_exec.policy.RecoveryPolicy` in the
requested mode, and emits a summary::

    {true_blocks, false_blocks, missed, abstentions, invalid_reuses}

Labels per case: ``must_block`` (at least one launch decision in the case
must be a block), ``must_not_block`` (no launch decision may be a block),
``advisory`` (the case exercises an abstention path). Blocks are counted from
``proposed_action``: in observe mode nothing is enforced, so proposals are
the conservative quantity to gate on. The release gate is zero false hard
blocks and zero invalid reuses on the adjudicated set; missed detections and
abstentions are reported, never silently counted as correct.

Fixtures carry opaque labeled fingerprints only (``meta-*`` strings). No
historical fingerprints are manufactured and no runtime minutes are invented
here; fixture success is not a population guarantee.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .policy import (ABSTENTION_REASONS, ENFORCEMENT_MODES, LaunchRequest,
                     PolicyError, RecoveryPolicy)

SCHEMA_VERSION = 1
LAUNCH_FIELDS = ("request_id", "project", "component", "operation",
                 "operation_family", "variant", "purpose", "selectors",
                 "intended_scope", "source_fingerprint",
                 "relevant_inputs_fingerprint", "environment_fingerprint",
                 "environment_class", "test_seed", "semantic_flags",
                 "parent_job_id", "registered_recovery_variant",
                 "wrapper_tags")
EVENT_TYPES = ("launch", "complete", "edit_verified", "edit_unknown",
               "log_read")


class ReplayError(ValueError):
    """A fixture set is malformed or its labels are unsatisfiable."""


def _build_request(case: dict, event: dict, state: dict) -> LaunchRequest:
    fields = {}
    for key in LAUNCH_FIELDS:
        if key in event:
            fields[key] = event[key]
        elif (key in ("source_fingerprint", "relevant_inputs_fingerprint")
                and key in state):
            fields[key] = state[key]
        elif key in case.get("request", {}):
            fields[key] = case["request"][key]
    defaults = {
        "project": "fixture-project", "component": "web", "operation": "unit",
        "operation_family": "unit", "request_id": None,
    }
    for key, value in defaults.items():
        fields.setdefault(key, value)
    if not fields.get("request_id"):
        raise ReplayError("launch event without request_id")
    for key in ("selectors", "semantic_flags", "wrapper_tags"):
        if isinstance(fields.get(key), list):
            fields[key] = tuple(fields[key])
    if "registered_recovery_variant" not in fields:
        fields["registered_recovery_variant"] = bool(
            event.get("registered_recovery_variant"))
    return LaunchRequest(**fields)


def _apply_state_event(event: dict, state: dict) -> None:
    kind = event.get("type")
    if kind == "edit_verified":
        if "source_fingerprint" in event:
            state["source_fingerprint"] = event["source_fingerprint"]
        if "relevant_inputs_fingerprint" in event:
            state["relevant_inputs_fingerprint"] = \
                event["relevant_inputs_fingerprint"]
    elif kind == "edit_unknown":
        state["source_fingerprint"] = "unknown"
        state["relevant_inputs_fingerprint"] = "unknown"


def run_case(case: dict, mode: str) -> dict:
    """Replay one labeled case; return its counted result (no gating here)."""
    case_id = case.get("case_id", "<unnamed>")
    labels = case.get("labels", {})
    if not isinstance(labels, dict):
        raise ReplayError(f"{case_id}: labels must be an object")
    flags = case.get("flags", {})
    policy = RecoveryPolicy(
        coalescing=bool(flags.get("coalescing", False)),
        reuse=bool(flags.get("reuse", False)),
        enforcement=mode)
    state = {
        "source_fingerprint": case.get("request", {}).get("source_fingerprint",
                                                          "unknown"),
        "relevant_inputs_fingerprint": case.get("request", {}).get(
            "relevant_inputs_fingerprint", "unknown"),
    }
    decisions, notes, invalid_reuses = [], [], 0
    for index, event in enumerate(case.get("events", [])):
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise ReplayError(f"{case_id}: unknown event type {kind!r}")
        _apply_state_event(event, state)
        if kind == "launch":
            request = _build_request(case, event, state)
            decision = policy.evaluate_launch(request)
            decisions.append(decision)
            if (decision.actual_action == "return_reused_receipt"
                    and not event.get("allow_reuse", False)):
                invalid_reuses += 1
                notes.append(f"event {index}: invalid reuse")
        elif kind == "complete":
            try:
                policy.report_completion(
                    event.get("job_ref"),
                    outcome=event.get("outcome", "passed"),
                    failure_class=event.get("failure_class"),
                    post_source_fingerprint=event.get("post_source_fingerprint"),
                    concurrent_edit=bool(event.get("concurrent_edit", False)))
            except PolicyError as exc:
                raise ReplayError(f"{case_id}: event {index}: {exc}") from exc
        elif kind == "log_read":
            try:
                policy.note_log_observation(event.get("job_ref"),
                                            int(event.get("new_bytes", 0)))
            except PolicyError as exc:
                raise ReplayError(f"{case_id}: event {index}: {exc}") from exc

    blocks = [d for d in decisions if d.prevents_launch()]
    abstentions = [d for d in decisions if d.is_abstention()]
    must_block = bool(labels.get("must_block", False))
    must_not_block = bool(labels.get("must_not_block", False))
    if must_block and must_not_block:
        raise ReplayError(f"{case_id}: contradictory labels")
    false_blocks = sum(1 for _ in blocks) if not must_block else 0
    if must_block and not blocks:
        notes.append("missed: case labeled must_block produced no block")
    return {
        "case_id": case_id,
        "description": case.get("description", ""),
        "labels": {"must_block": must_block,
                   "must_not_block": must_not_block,
                   "advisory": bool(labels.get("advisory", False))},
        "blocks": [{"reason_code": d.reason_code,
                    "proposed_action": d.proposed_action,
                    "actual_action": d.actual_action} for d in blocks],
        "abstention_count": len(abstentions),
        "abstention_reasons": sorted({d.reason_code for d in abstentions}),
        "decisions": [{"reason_code": d.reason_code,
                       "proposed_action": d.proposed_action,
                       "actual_action": d.actual_action}
                      for d in decisions],
        "true_blocks": len(blocks) if must_block else 0,
        "false_blocks": false_blocks,
        "missed": 1 if must_block and not blocks else 0,
        "invalid_reuses": invalid_reuses,
        "advisory_unsatisfied": (bool(labels.get("advisory", False))
                                 and not abstentions),
        "notes": notes,
    }


def replay(fixture_dir: Path, mode: str = "observe") -> dict:
    """Replay every labeled fixture in ``fixture_dir`` under ``mode``."""
    if mode not in ENFORCEMENT_MODES:
        raise ReplayError(f"unknown mode: {mode!r}")
    paths = sorted(Path(fixture_dir).glob("*.json"))
    if not paths:
        raise ReplayError(f"no fixtures found under {fixture_dir}")
    results = []
    for path in paths:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayError(f"{path}: unreadable fixture: {exc}") from exc
        if case.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ReplayError(f"{path}: unsupported schema_version")
        results.append(run_case(case, mode))
    summary = {
        "mode": mode,
        "blocks_counted": "proposed (observe mode enforces nothing)",
        "cases": len(results),
        "decisions": sum(len(r["decisions"]) for r in results),
        "true_blocks": sum(r["true_blocks"] for r in results),
        "false_blocks": sum(r["false_blocks"] for r in results),
        "missed": sum(r["missed"] for r in results),
        "abstentions": sum(r["abstention_count"] for r in results),
        "invalid_reuses": sum(r["invalid_reuses"] for r in results),
        "gate": {},
        "per_case": results,
    }
    summary["gate"] = {
        "zero_false_blocks": summary["false_blocks"] == 0,
        "zero_invalid_reuses": summary["invalid_reuses"] == 0,
        "passed": (summary["false_blocks"] == 0
                   and summary["invalid_reuses"] == 0),
    }
    return summary


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_exec.replay",
        description="Offline labeled recovery-policy replay (metadata only)")
    parser.add_argument("--fixtures", required=True,
                        help="directory of labeled fixture JSON files")
    parser.add_argument("--mode", default="observe",
                        choices=ENFORCEMENT_MODES,
                        help="policy enforcement mode (default: observe)")
    args = parser.parse_args(argv)
    try:
        summary = replay(Path(args.fixtures), mode=args.mode)
    except ReplayError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
