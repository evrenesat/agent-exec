"""Ledger builder/checker for controlled arm comparisons (offline).

Reads per-arm receipts (arm-receipt.json + acceptance-receipt.json) and emits
one bound ledger; `--check-ledger` validates structural integrity: frozen task
inputs identical across arms, every arm accepted-or-failed with receipts
present, no duplicate (arm, repeat), and n reported honestly. This module
makes no statistical claims and never pools tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_arm(arm_dir: Path) -> dict:
    receipt_path = arm_dir / "arm-receipt.json"
    acceptance_path = arm_dir / "acceptance-receipt.json"
    if not receipt_path.is_file():
        return {"arm_dir": str(arm_dir), "error": "arm-receipt.json missing"}
    receipt = json.loads(receipt_path.read_text())
    acceptance = None
    if acceptance_path.is_file():
        acceptance = json.loads(acceptance_path.read_text())
    entry = {
        "arm": receipt.get("arm"),
        "repeat": receipt.get("repeat"),
        "session": receipt.get("session"),
        "metrics": receipt.get("metrics"),
        "frozen_inputs": receipt.get("frozen_inputs"),
        "acceptance": None if acceptance is None else {
            "accepted": acceptance.get("accepted"),
            "cases": [{k: c.get(k) for k in ("case", "outcome", "cleanup")}
                      for c in acceptance.get("cases", [])],
            "full_report": acceptance.get("full_report"),
            "heldout_report": acceptance.get("heldout_report"),
        },
        "arm_receipt_sha256": sha256(receipt_path),
    }
    if acceptance_path.is_file():
        entry["acceptance_receipt_sha256"] = sha256(acceptance_path)
    return entry


def build_ledger(arms_root: Path, output: Path | None = None) -> dict:
    arms = []
    for arm_dir in sorted((arms_root / "arms").glob("*-r*")):
        if not arm_dir.is_dir() or arm_dir.name.startswith("reference"):
            continue
        entry = collect_arm(arm_dir)
        if "error" not in entry and entry.get("arm"):
            arms.append(entry)
    frozen = {json.dumps(a.get("frozen_inputs"), sort_keys=True) for a in arms
              if a.get("frozen_inputs")}
    ledger = {
        "schema_version": 1,
        "created_wall": time.time(),
        "task": {"name": "runRetryHint TDD task",
                 "inputs_consistent": len(frozen) <= 1,
                 "frozen_inputs_sample": arms[0].get("frozen_inputs") if arms else None},
        "arms": arms,
        "notes": [
            "n=1 arms are single observations; medians require n>=2 and even "
            "then support no statistical population claims",
            "accepted/attempted and wall metrics never pool across arms with "
            "different repeat counts; failed sessions count in denominators",
            "first_edit_offset_seconds from the session log is diagnostic-only "
            "(timestamp clock inconsistency observed); excluded from comparisons",
        ],
    }
    if output is not None:
        if output.exists():
            raise SystemExit(f"refusing to overwrite {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(ledger, indent=2) + "\n")
    return ledger


def check_ledger(path: Path) -> list[str]:
    problems: list[str] = []
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1:
        problems.append("unsupported schema_version")
    arms = data.get("arms", [])
    if not arms:
        problems.append("ledger has no arms")
    seen = set()
    for arm in arms:
        key = (arm.get("arm"), arm.get("repeat"))
        if key in seen:
            problems.append(f"duplicate arm/repeat: {key}")
        seen.add(key)
        if arm.get("acceptance") is None:
            problems.append(f"arm {key}: no acceptance receipt binding")
        elif arm["acceptance"].get("accepted") not in (True, False):
            problems.append(f"arm {key}: acceptance outcome not boolean")
    frozen = {json.dumps(a.get("frozen_inputs"), sort_keys=True) for a in arms}
    if len(frozen) > 1:
        problems.append("frozen task inputs differ across arms")
    if not data.get("task", {}).get("inputs_consistent", False):
        problems.append("task inputs_consistent flag false")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_exec.comparison")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", metavar="ARMS_ROOT", type=Path,
                       help="build ledger.json from an evaluation arms root")
    group.add_argument("--check-ledger", metavar="LEDGER", type=Path,
                       help="validate an existing ledger")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.build:
        ledger = build_ledger(args.build, args.output)
        print(json.dumps({"arms": len(ledger["arms"]),
                          "inputs_consistent": ledger["task"]["inputs_consistent"],
                          "output": str(args.output) if args.output else None},
                         indent=2))
        return 0
    problems = check_ledger(args.check_ledger)
    print(json.dumps({"ledger": str(args.check_ledger),
                      "problems": problems,
                      "passed": not problems}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
