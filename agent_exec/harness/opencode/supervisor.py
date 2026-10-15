#!/usr/bin/env python3
"""Supervisor-owned HTTP bridge between harness sessions and the job layer.

Serves the F3-proven contract over the REAL controller:

    GET  /health
    GET  /events?cursor=<seq>     -> {events:[{job_id,revision,status,terminal,
                                      outcome,operation_id}], cursor}
    POST /jobs                    -> launch {request_id, operation, argv?, cwd?,
                                      deadline_seconds?, env?}
    GET  /jobs/<id>               -> current receipt/status

The supervisor is the only component with launch authority; harness sessions
(e.g. an OpenCode plugin) poll /events with a cursor and never relaunch work.
Session binding: one supervisor instance serves one harness session namespace;
request ids are namespaced by the bound session label.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_exec.jobs import JobController, JobError  # noqa: E402


class Supervisor:
    def __init__(self, controller: JobController, operations: dict | None = None):
        self.controller = controller
        self.operations = operations or {}
        self.launch_count = 0

    def events_payload(self, cursor: int) -> dict:
        """Absorb live terminal truth first, then serve events after cursor.

        The store alone never becomes terminal: systemd owns lifetime and the
        ExecStopPost record holds the exit truth, so every /events request
        first runs reconciliation (cheap `systemctl show` per open job). This
        is what makes cursor polling self-driving without a second authority.
        """
        try:
            self.controller.reconcile_all()
        except Exception:  # reconciliation failure must not fake progress
            pass
        import sqlite3
        with self.controller.store._lock:
            rows = self.controller.store._db.execute(
                "SELECT seq, job_id, revision, kind, payload_json FROM events"
                " WHERE seq>? AND revision IS NOT NULL ORDER BY seq",
                (cursor,)).fetchall()
        events = []
        max_seq = cursor
        for row in rows:
            max_seq = max(max_seq, row["seq"])
            payload = json.loads(row["payload_json"] or "{}")
            job = self.controller.store.get_job(row["job_id"])
            events.append({
                "job_id": row["job_id"],
                "revision": row["revision"],
                "kind": row["kind"],
                "status": payload.get("outcome") or row["kind"],
                "terminal": row["kind"] in ("terminal", "cancelled", "reconciled"),
                "outcome": payload.get("outcome"),
                "cleanup": payload.get("cleanup"),
                "operation_id": (job or {}).get("operation_id"),
            })
        return {"events": events, "cursor": max_seq}

    def launch(self, body: dict) -> dict:
        request_id = body.get("request_id")
        if not request_id:
            raise JobError("request_id is required")
        operation = body.get("operation")
        if operation and operation in self.operations:
            spec = self.operations[operation]
            cwd = spec.get("cwd")
            env_overrides = spec.get("env", {})
        else:
            raise JobError(f"unknown operation: {operation!r}; "
                           f"known: {sorted(self.operations)}")
        deadline = int(body.get("deadline_seconds") or spec.get("deadline_seconds", 120))
        # Per-job report dir: "{report_dir}" tokens in the operation argv are
        # replaced with a job-owned path so concurrent jobs never share files.
        report_dir = self.controller.evidence_root / "jobs" / \
            f"{self.controller.session_id}-{request_id}" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        argv = [part.replace("{report_dir}", str(report_dir)) for part in spec["argv"]]
        env = dict(env_overrides)
        env.update({str(k): str(v) for k, v in (body.get("env") or {}).items()})
        result = self.controller.launch(
            argv, request_id=str(request_id), operation_id=operation,
            deadline_seconds=deadline, stop_grace_seconds=5, env=env,
            profile_id=spec.get("profile_id"), cwd=cwd)
        return result


def make_handler(supervisor: Supervisor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/health"):
                self._json(200, {"ready": True, "supervisor": "execution-layer"})
            elif self.path.startswith("/events"):
                cursor = 0
                for part in self.path.split("?")[-1].split("&"):
                    if part.startswith("cursor="):
                        cursor = int(part.split("=", 1)[1])
                self._json(200, supervisor.events_payload(cursor))
            elif self.path.startswith("/jobs/"):
                job_id = self.path.rsplit("/", 1)[-1]
                try:
                    receipt = supervisor.controller.status(job_id)
                    from agent_exec import reports as report_reader
                    row = supervisor.controller.store.get_job(job_id)
                    if row is not None:
                        job_dir = (supervisor.controller.evidence_root / "jobs" /
                                   f"{row['session_id']}-{row['request_id']}")
                        report_file = job_dir / "reports" / "vitest-json.json"
                        if report_file.is_file():
                            receipt["report"] = report_reader.read_vitest_json(report_file)
                    self._json(200, receipt)
                except JobError as error:
                    self._json(404, {"error": str(error)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/jobs"):
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                self._json(200, supervisor.launch(body))
            except JobError as error:
                self._json(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 — surface honestly
                self._json(500, {"error": f"{type(error).__name__}: {error}"})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session-label", default="supervisor")
    parser.add_argument("--operations", type=Path, default=None,
                        help="JSON file mapping operation ids to launch specs")
    args = parser.parse_args()
    operations = {}
    if args.operations:
        operations = json.loads(args.operations.read_text())
    controller = JobController(Path(args.store), evidence_root=Path(args.evidence_root),
                               session_id=args.session_label)
    supervisor = Supervisor(controller, operations)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(supervisor))
    port = server.server_port
    print(json.dumps({"listening": f"http://127.0.0.1:{port}",
                      "operations": sorted(operations)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
