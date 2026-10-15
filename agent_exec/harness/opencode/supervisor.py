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
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_exec.jobs import JobController, JobError  # noqa: E402


REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_DEADLINE_SECONDS = 3600


class Supervisor:
    def __init__(self, controller: JobController, operations: dict | None = None,
                 *, token: str | None = None, max_deadline_seconds: int = MAX_DEADLINE_SECONDS):
        self.controller = controller
        self.operations = operations or {}
        self.token = token
        self.max_deadline_seconds = max_deadline_seconds
        self.launch_count = 0

    def validate_request_id(self, request_id: str) -> str:
        """Restrictive id policy: it becomes part of a filesystem path."""
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise JobError("invalid request_id: must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        return request_id

    def job_dir_for(self, session_id: str, request_id: str) -> Path:
        """Job directory under the evidence root; refuses any escape."""
        job_dir = (self.controller.evidence_root / "jobs" /
                   f"{session_id}-{request_id}")
        resolved = job_dir.resolve()
        evidence_resolved = Path(self.controller.evidence_root).resolve()
        if evidence_resolved != resolved and evidence_resolved not in resolved.parents:
            raise JobError("job path escapes the evidence root")
        return job_dir

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
        if not isinstance(body, dict):
            raise JobError("body must be a JSON object")
        if "env" in body:
            # Callers cannot alter the execution environment: the operation
            # definition owns it (review P0#2).
            raise JobError("caller-supplied environment overrides are rejected")
        request_id = self.validate_request_id(body.get("request_id"))
        operation = body.get("operation")
        if operation and operation in self.operations:
            spec = self.operations[operation]
            cwd = spec.get("cwd")
            env = dict(spec.get("env", {}))
        else:
            raise JobError(f"unknown operation: {operation!r}; "
                           f"known: {sorted(self.operations)}")
        # Deadlines come from the admitted operation definition; a caller may
        # only shorten within it, never exceed the server-side maximum.
        requested = body.get("deadline_seconds")
        deadline = int(spec.get("deadline_seconds", 120))
        if requested is not None:
            deadline = min(deadline, int(requested))
        deadline = min(deadline, self.max_deadline_seconds)
        if deadline <= 0:
            raise JobError("deadline must be positive")
        # Per-job report dir: "{report_dir}" tokens in the operation argv are
        # replaced with a job-owned path so concurrent jobs never share files.
        # Defense in depth beyond the request_id regex: the resolved path must
        # stay inside the evidence root.
        report_dir = self.job_dir_for(self.controller.session_id,
                                      request_id) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        argv = [part.replace("{report_dir}", str(report_dir)) for part in spec["argv"]]
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

        def _authorized(self) -> bool:
            if supervisor.token is None:
                return True
            return self.headers.get("X-Agent-Exec-Token") == supervisor.token

        def do_GET(self):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
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
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
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
    token = secrets.token_hex(16)
    supervisor = Supervisor(controller, operations, token=token)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(supervisor))
    port = server.server_port
    # The token is emitted once, locally, for the session driver to hand to
    # the bridge config; loopback binding plus token gates other local
    # processes from launching jobs.
    print(json.dumps({"listening": f"http://127.0.0.1:{port}",
                      "operations": sorted(operations), "token": token}), flush=True)
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
