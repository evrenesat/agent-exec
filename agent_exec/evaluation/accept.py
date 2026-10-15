#!/usr/bin/env python3
"""Independent acceptance for a CK7 arm: held-out edges + full suite + lint.

Runs AFTER the contestant session, on the retained candidate, through the
native job backend. The held-out test file is copied in only now, so it was
never visible to the contestant. Incomplete or failed acceptance is recorded
truthfully and never repaired.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_exec import reports
from agent_exec.jobs import JobController
from agent_exec.reports import read_vitest_json

HERE = Path(__file__).resolve().parent
EVIDENCE = Path("/var/lib/xtx-benchmarks/execution-layer-20261013/evaluation")
TASK = EVIDENCE / "task"
NODE_BIN = "/opt/node-v24.20.0-linux-x64/bin"
ENVS = {"PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp", "npm_config_audit": "false", "npm_config_fund": "false",
        "NO_COLOR": "1"}


def run_job(controller: JobController, arm_dir: Path, name: str, argv: list[str],
            cwd: Path, deadline: int) -> dict:
    request_id = f"accept-{arm_dir.name}-{name}"
    started = controller.launch(argv, request_id=request_id, operation_id=name,
                                deadline_seconds=deadline, stop_grace_seconds=5,
                                env=ENVS, cwd=str(cwd))
    deadline_at = time.monotonic() + deadline + 30
    receipt = None
    while time.monotonic() < deadline_at:
        result = controller.wait(started["job_id"], max_wait_seconds=10)
        if not result["elapsed"]:
            receipt = result.get("receipt") or controller.status(started["job_id"])
            break
    receipt = receipt or controller.status(started["job_id"])
    return {"case": name, "job_id": started["job_id"],
            "outcome": receipt.get("outcome"), "exit_status": receipt.get("exit_status"),
            "cleanup": receipt.get("cleanup"),
            "wall_seconds": round((receipt["terminal_wall"] - receipt["started_wall"]), 2)
            if receipt.get("terminal_wall") and receipt.get("started_wall") else None,
            "replayed": started.get("replayed", False)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm-dir", type=Path, required=True,
                        help="e.g. .../evaluation/arms/control-r1")
    args = parser.parse_args()
    candidate = args.arm_dir / "candidate"
    src_test = candidate / "apps/aflow_app/web/src/runRetryHint.test.ts"
    if not src_test.is_file():
        print(json.dumps({"accepted": False,
                          "reason": "provided spec test file missing from candidate"}))
        return 1
    heldout_src = TASK / "held-out/runRetryHint.heldout.test.ts"
    heldout_dst = candidate / "apps/aflow_app/web/src/runRetryHint.heldout.test.ts"
    if heldout_dst.exists():
        print(json.dumps({"accepted": False,
                          "reason": "held-out file already present in candidate; "
                                    "acceptance integrity violated"}))
        return 2
    shutil_copy(heldout_src, heldout_dst)
    controller = JobController(args.arm_dir / "acceptance-store.sqlite",
                               evidence_root=args.arm_dir / "acceptance-jobs")
    cases = []
    try:
        npm = f"{NODE_BIN}/npm"
        cases.append(run_job(controller, args.arm_dir, "heldout-edges",
                             [npm, "--prefix", "apps/aflow_app/web", "test", "--",
                              "--run", "--maxWorkers=1", "--minWorkers=1",
                              "--pool=forks", "src/runRetryHint.test.ts",
                              "src/runRetryHint.heldout.test.ts",
                              "--reporter=json",
                              "--outputFile=" + str(args.arm_dir / "acceptance-jobs/heldout-report.json")],
                             candidate, 120))
        heldout_report = read_vitest_json(args.arm_dir / "acceptance-jobs/heldout-report.json")
        cases.append(run_job(controller, args.arm_dir, "full-suite",
                             [npm, "--prefix", "apps/aflow_app/web", "test", "--",
                              "--run", "--maxWorkers=1", "--minWorkers=1",
                              "--pool=forks", "--reporter=json",
                              "--outputFile=" + str(args.arm_dir / "acceptance-jobs/full-report.json")],
                             candidate, 180))
        full_report = read_vitest_json(args.arm_dir / "acceptance-jobs/full-report.json")
        cases.append(run_job(controller, args.arm_dir, "lint",
                             [npm, "--prefix", "apps/aflow_app/web", "run", "lint"],
                             candidate, 120))
    finally:
        controller.close()
    accepted = (all(c["outcome"] == "passed" and c["cleanup"] == "verified_empty"
                    for c in cases)
                and heldout_report.get("complete") and heldout_report.get("runner_success")
                and full_report.get("complete") and full_report.get("runner_success"))
    receipt = {
        "schema_version": 1, "acceptance": "ck7-independent", "arm_dir": str(args.arm_dir),
        "cases": cases,
        "heldout_report": {k: heldout_report.get(k) for k in
                           ("integrity", "complete", "total", "passed", "failed")},
        "full_report": {k: full_report.get(k) for k in
                        ("integrity", "complete", "total", "passed", "failed")},
        "accepted": accepted,
        "note": "held-out tests were copied into the candidate only at acceptance time",
    }
    (args.arm_dir / "acceptance-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0 if accepted else 1


def shutil_copy(src: Path, dst: Path) -> None:
    dst.write_bytes(src.read_bytes())


if __name__ == "__main__":
    sys.exit(main())
