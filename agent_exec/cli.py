"""Execution-layer CLI. The `selftest` subcommand is the actual Linux proof
(positive hard deadlines, real units, bounded runtime); every other subcommand
drives one controller. No model calls anywhere."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path


from . import linux_jobs
from .jobs import JobController, JobError

DEFAULT_ROOT = Path("/var/lib/xtx-benchmarks/execution-layer-20261013")
SESSION_FILE = Path.home() / ".agent-exec" / "session-id"


def default_session_id() -> str:
    """Persistent local-CLI identity so separate invocations share jobs.

    Resolution: --session-id flag > AGENT_EXEC_SESSION_ID env >
    ~/.agent-exec/session-id file (created on first use). Embedded callers
    (supervisor, tests) pass explicit ids and keep full isolation.
    """
    env = os.environ.get("AGENT_EXEC_SESSION_ID")
    if env:
        return env
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SESSION_FILE.is_file():
            value = SESSION_FILE.read_text().strip()
            if value:
                return value
        value = "cli-" + uuid.uuid4().hex[:12]
        SESSION_FILE.write_text(value + chr(10))
        return value
    except OSError:
        return "cli-" + uuid.uuid4().hex[:12]


def _controller(args) -> JobController:
    return JobController(Path(args.store), evidence_root=Path(args.evidence_root),
                         session_id=args.session_id)


def cmd_selftest(args) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    controller = JobController(output / "selftest-store.sqlite",
                               evidence_root=output / "evidence")
    try:
        cases = [
            ("instant-success", ["/usr/bin/true"], 60, 5, None, "passed"),
            ("nonzero-exit", ["/bin/sh", "-c", "exit 3"], 60, 5, None, "failed"),
            ("hard-deadline", ["/usr/bin/sleep", "300"], 3, 2, None, "timed_out"),
        ]
        for name, argv, deadline, grace, expect_cancel, expected in cases:
            request = "selftest-" + name
            started = controller.launch(argv, request_id=request,
                                        operation_id="selftest." + name,
                                        deadline_seconds=deadline,
                                        stop_grace_seconds=grace)
            job_id = started["job_id"]
            if expect_cancel:
                controller.cancel(job_id, reason="selftest cancellation")
            receipt = None
            deadline_at = time.monotonic() + 60
            while time.monotonic() < deadline_at:
                result = controller.wait(job_id, max_wait_seconds=10)
                if not result["elapsed"]:
                    receipt = result.get("receipt") or controller.status(job_id)
                    break
            receipt = receipt or controller.status(job_id)
            results.append({
                "case": name, "job_id": job_id, "expected_outcome": expected,
                "outcome": receipt.get("outcome"), "cleanup": receipt.get("cleanup"),
                "passed": receipt.get("outcome") == expected
                and receipt.get("cleanup") == "verified_empty",
                "receipt": receipt,
            })
        # cancellation of a running job
        started = controller.launch(["/usr/bin/sleep", "300"],
                                    request_id="selftest-cancel",
                                    operation_id="selftest.cancel",
                                    deadline_seconds=60, stop_grace_seconds=2)
        controller.cancel(started["job_id"], reason="selftest")
        receipt = controller.wait(started["job_id"], max_wait_seconds=20).get("receipt")
        results.append({
            "case": "cancel-running", "job_id": started["job_id"],
            "outcome": (receipt or {}).get("outcome"),
            "cleanup": (receipt or {}).get("cleanup"),
            "passed": bool(receipt) and receipt["outcome"] == "cancelled"
            and receipt["cleanup"] == "verified_empty",
            "receipt": receipt,
        })
        # launch failure is blocked, and its request replays as blocked
        try:
            controller.launch(["/nonexistent-binary-xyz"], request_id="selftest-blocked",
                              operation_id="selftest.blocked")
            blocked_raised = False
        except JobError:
            blocked_raised = True
        replay = controller.launch(["/nonexistent-binary-xyz"],
                                   request_id="selftest-blocked",
                                   operation_id="selftest.blocked")
        results.append({
            "case": "launch-failure-blocked",
            "raised": blocked_raised, "replay": replay,
            "passed": blocked_raised and replay.get("replayed") is True,
        })
        # journal cursor read on a completed job
        job_id = results[0]["job_id"]
        logs = controller.logs(job_id)
        results.append({
            "case": "bounded-log-read",
            "bytes": logs.get("bytes"), "gap": logs.get("gap"),
            "entries": len(logs.get("entries", [])),
            "passed": logs.get("gap") is None,
        })
        receipt_summary = {
            "schema_version": 1, "selftest": "linux-backend",
            "created_wall": time.time(), "hostname": linux_jobs.boot_id(),
            "cases": results,
            "passed": all(r.get("passed") for r in results),
        }
    finally:
        controller.close()
    dest = output / "selftest-receipt.json"
    dest.write_text(json.dumps(receipt_summary, indent=2) + "\n")
    print(json.dumps({"receipt": str(dest), "passed": receipt_summary["passed"],
                      "cases": [{k: r.get(k) for k in ("case", "outcome", "passed")}
                                for r in results]}, indent=2))
    return 0 if receipt_summary["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_exec.cli")
    parser.add_argument("--store", default=str(DEFAULT_ROOT / "controller.sqlite"))
    parser.add_argument("--evidence-root", default=str(DEFAULT_ROOT))
    parser.add_argument("--session-id", default=None,
                        help="session identity (default: persistent "
                             "~/.agent-exec/session-id; AGENT_EXEC_SESSION_ID "
                             "env overrides)")
    sub = parser.add_subparsers(dest="command", required=True)
    launch = sub.add_parser("launch")
    launch.add_argument("--request-id", required=True)
    launch.add_argument("--operation-id", default="cli.operation")
    launch.add_argument("--deadline-seconds", type=int, default=120)
    launch.add_argument("--stop-grace-seconds", type=int, default=5)
    launch.add_argument("argv", nargs="+")
    for name in ("status", "wait", "events", "logs", "cancel", "reconcile"):
        subparser = sub.add_parser(name)
        subparser.add_argument("job_id")
        if name == "wait":
            subparser.add_argument("--max-wait-seconds", type=float, default=30.0)
        if name == "logs":
            subparser.add_argument("--after-cursor")
            subparser.add_argument("--max-bytes", type=int, default=32768)
    reconcile_all = sub.add_parser("reconcile-all")
    selftest = sub.add_parser("selftest")
    selftest.add_argument("--backend", choices=["linux"], default="linux")
    selftest.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if getattr(args, "session_id", None) is None:
        args.session_id = default_session_id()
    try:
        if args.command == "selftest":
            return cmd_selftest(args)
        controller = _controller(args)
        try:
            if args.command == "launch":
                result = controller.launch(args.argv, request_id=args.request_id,
                                           operation_id=args.operation_id,
                                           deadline_seconds=args.deadline_seconds,
                                           stop_grace_seconds=args.stop_grace_seconds)
            elif args.command == "status":
                result = controller.status(args.job_id)
            elif args.command == "wait":
                result = controller.wait(args.job_id, max_wait_seconds=args.max_wait_seconds)
            elif args.command == "events":
                result = controller.events(args.job_id)
            elif args.command == "logs":
                result = controller.logs(args.job_id, after_cursor=args.after_cursor,
                                         max_bytes=args.max_bytes)
            elif args.command == "cancel":
                result = controller.cancel(args.job_id, reason="cli")
            elif args.command == "reconcile":
                result = controller.reconcile(args.job_id)
            elif args.command == "reconcile-all":
                result = {"results": controller.reconcile_all()}
            else:  # pragma: no cover
                parser.error(f"unknown command {args.command}")
        finally:
            controller.close()
        print(json.dumps(result, indent=2, default=str))
        return 0
    except JobError as error:
        print(f"job error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
