#!/usr/bin/env python3
"""Validate checkpoint-0 feasibility decision receipts.

Checks that each required F# decision exists, is structurally complete, and
that every evidence file it names is present under the same proof directory.
Prints a JSON summary and exits nonzero on any missing/invalid receipt.
Read-only; never mutates evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ALLOWED_DECISIONS = {
    "reuse_existing_evidence", "adapt", "build_thin_bridge",
    "reuse_public_integration", "thin_native_bridge", "adopt_harbor_for_evaluation",
    "retain_xtx_evaluator", "retain_xtx_with_optional_harbor_import",
    "inspect_only_unavailable", "reject", "hybrid", "pending",
}
REQUIRED_KEYS = ("schema_version", "decision_id", "title", "date", "decision",
                 "decision_summary", "evidence_files")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def check_decision(root: Path, fid: str) -> dict:
    proof = root / fid / "proof"
    receipt_path = proof / f"{fid.lower()}-decision.json"
    result = {"decision_id": fid, "receipt": str(receipt_path), "ok": False, "problems": []}
    if not receipt_path.is_file():
        result["problems"].append("decision receipt missing")
        return result
    try:
        receipt = json.loads(receipt_path.read_text())
    except json.JSONDecodeError as error:
        result["problems"].append(f"invalid JSON: {error}")
        return result
    for key in REQUIRED_KEYS:
        if key not in receipt:
            result["problems"].append(f"missing key: {key}")
    if receipt.get("decision_id") != fid:
        result["problems"].append("decision_id mismatch")
    if receipt.get("decision") not in ALLOWED_DECISIONS:
        result["problems"].append(f"unknown decision value: {receipt.get('decision')}")
    evidence = {}
    for name in receipt.get("evidence_files", []):
        path = proof / name
        if not path.is_file():
            result["problems"].append(f"evidence file missing: {name}")
            continue
        evidence[name] = sha256(path)
    result["evidence_sha256"] = evidence
    result["receipt_sha256"] = sha256(receipt_path)
    result["ok"] = not result["problems"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="feasibility evidence root (contains F0..F4/proof)")
    parser.add_argument("--require", nargs="+", default=["F0", "F1", "F2", "F3", "F4"])
    args = parser.parse_args()
    summary = {"root": str(args.root), "required": args.require, "results": [], "passed": True}
    for fid in args.require:
        result = check_decision(args.root, fid)
        summary["results"].append(result)
        if not result["ok"]:
            summary["passed"] = False
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
