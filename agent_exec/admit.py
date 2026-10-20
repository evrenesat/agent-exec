"""Admission runner: calibrate one operation profile with real repeats.

Runs the rendered profile argv N times through the native job controller on an
owned source copy, reads the admitted native reports, verifies cleanup, and
writes one admission receipt. Tool-only: no model calls, no contestant code.

Notes on containment: admission runs execute directly in the disposable owned
copy (systemd unit provides deadline + cgroup containment); serving contestants
composes the established bwrap recipe inside the same unit later.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import linux_jobs, reports
from .contracts import ProfileError, render, validate_profile
from .jobs import JobController
from .profiles import load_profile

DEFAULT_ROOT = Path("/var/lib/xtx-benchmarks/execution-layer-20261013")
NODE_BIN = "/opt/node-v24.20.0-linux-x64/bin"
REQUIRED_ADMISSION_REPEATS = 3


def check_storage(output: Path) -> dict:
    import shutil
    usage = shutil.disk_usage(output)
    return {"free_bytes": usage.free, "free_gib": round(usage.free / 2**30, 1)}


def run_admission(profile_path: Path, repeats: int, output: Path,
                  source_root: Path) -> dict:
    profile = load_profile(profile_path)
    validate_profile(profile)
    rendered = render(profile, source_root=str(source_root), job_id="admission",
                      report_dir=str(output / "reports"))
    output.mkdir(parents=True, exist_ok=True)
    storage = check_storage(output)
    if storage["free_bytes"] < 20 * 2**30:
        raise RuntimeError(f"insufficient headroom before admission: {storage}")
    controller = JobController(output / "admission-store.sqlite",
                               evidence_root=output / "jobs")
    runs = []
    try:
        for index in range(1, repeats + 1):
            run_dir = output / f"run-{index}"
            run_dir.mkdir(parents=True, exist_ok=True)
            request_id = f"admit-{profile['profile_id']}-{index}"
            npm = Path(NODE_BIN) / "npm"
            argv = [str(npm) if npm.is_file() else rendered.argv[0]] + \
                list(rendered.argv[1:-1]) + [
                rendered.argv[-1].replace(str(output / "reports"),
                                          str(run_dir / "reports"))]
            env = {
                "PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(run_dir / "home"),
                "npm_config_audit": "false",
                "npm_config_fund": "false",
                "NO_COLOR": "1",
            }
            (run_dir / "home").mkdir(exist_ok=True)
            (run_dir / "reports").mkdir(exist_ok=True)
            started = controller.launch(argv, request_id=request_id,
                                        operation_id=rendered.operation_id,
                                        deadline_seconds=rendered.deadlines["hard_seconds"],
                                        stop_grace_seconds=5, env=env,
                                        profile_id=profile["profile_id"],
                                        cwd=str(source_root))
            wait_deadline = time.monotonic() + rendered.deadlines["hard_seconds"] + 30
            receipt = None
            while time.monotonic() < wait_deadline:
                result = controller.wait(started["job_id"], max_wait_seconds=15)
                if not result["elapsed"]:
                    receipt = result.get("receipt") or controller.status(started["job_id"])
                    break
            receipt = receipt or controller.status(started["job_id"])
            report_path = Path(argv[-1].removeprefix("--outputFile="))
            report = reports.read_vitest_json(report_path)
            generated = [str(source_root / rel)
                         for rel in profile["inputs"]["generated"]]
            wall = None
            if receipt.get("started_wall") and receipt.get("terminal_wall"):
                wall = round(receipt["terminal_wall"] - receipt["started_wall"], 3)
            runs.append({
                "repeat": index, "job_id": started["job_id"],
                "replayed": started.get("replayed", False),
                "outcome": receipt.get("outcome"),
                "exit_status": receipt.get("exit_status"),
                "cleanup": receipt.get("cleanup"),
                "wall_seconds": wall,
                "report": report,
                "generated_outputs_present": {Path(p).name: Path(p).is_file()
                                              for p in generated},
                "argv": argv,
            })
    finally:
        controller.close()
    passed = all(
        r["outcome"] == "passed" and r["cleanup"] == "verified_empty"
        and r["report"].get("complete") and r["report"].get("runner_success")
        and all(r["generated_outputs_present"].values())
        for r in runs)
    margins = [rendered.deadlines["hard_seconds"] for _ in runs]
    from .profiles import admission_identity
    receipt = {
        "schema_version": 1,
        "admission": profile["profile_id"],
        "status_before": profile["status"],
        "repeats": len(runs),
        "profile_identity_sha256": admission_identity(profile),
        "source_root": str(source_root),
        "source_policy": "owned copy; production checkout never scored",
        "storage_before": storage,
        "deadlines": rendered.deadlines,
        "hard_deadline_margin_seconds": margins,
        "runs": runs,
        "passed": passed,
        "notes": [
            "admission ran unsandboxed inside the systemd unit on an owned "
            "disposable copy; contestant serving composes the established bwrap "
            "recipe within the same unit",
            "singleFork remains a forbidden flag for this profile",
            "profile status flips to fixture_verified only after root reviews "
            "this receipt",
        ],
        "created_wall": time.time(),
    }
    dest = output / "admission-receipt.json"
    dest.write_text(json.dumps(receipt, indent=2, default=str) + "\n")
    print(json.dumps({"receipt": str(dest), "passed": passed,
                      "runs": [{k: r.get(k) for k in ("repeat", "outcome", "cleanup",
                                                      "report")}
                               for r in runs]}, indent=2, default=str))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_exec.admit")
    parser.add_argument("--profile", required=True,
                        help="profile id (under agent_exec/profiles)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT / "admission-aflow-web")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--profiles-dir", type=Path,
                        default=Path(__file__).resolve().parent / "profiles")
    args = parser.parse_args(argv)
    if args.repeats < REQUIRED_ADMISSION_REPEATS:
        print(f"admission error: --repeats must be >= "
              f"{REQUIRED_ADMISSION_REPEATS} (got {args.repeats}); a shorter "
              f"run can never satisfy the calibration gate", file=sys.stderr)
        return 2
    profile_path = args.profiles_dir / f"{args.profile}.json"
    try:
        receipt = run_admission(profile_path, args.repeats, args.output,
                                args.source_root)
    except (ProfileError, RuntimeError) as error:
        print(f"admission error: {error}", file=sys.stderr)
        return 2
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
