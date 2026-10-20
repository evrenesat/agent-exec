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

def _package_root() -> str:
    """Repo root that makes `import agent_exec` work in ANY execution mode
    (python -m, direct script, or copied location)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "agent_exec" / "__init__.py").is_file():
            return str(parent)
    return str(here.parents[3] if len(here.parents) > 3 else here.parents[0])


sys.path.insert(0, _package_root())

from agent_exec.contracts import render  # noqa: E402
from agent_exec.jobs import JobController, JobError  # noqa: E402  # noqa: E402


REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_DEADLINE_SECONDS = 3600


class Supervisor:
    def __init__(self, controller: JobController, operations: dict | None = None,
                 *, token: str | None = None,
                 max_deadline_seconds: int = MAX_DEADLINE_SECONDS,
                 profiles_dir: Path | None = None):
        self.controller = controller
        self.operations = operations or {}
        self.token = token
        self.max_deadline_seconds = max_deadline_seconds
        self.profiles_dir = Path(profiles_dir) if profiles_dir else \
            Path(__file__).resolve().parents[2] / "profiles"
        self.launch_count = 0

    def resolve_operation_command(self, operation: str, spec: dict,
                                  request_id: str, report_dir: Path) -> tuple[list[str], int, str]:
        """Admission-enforced command resolution.

        Operations bound to a profile render their command FROM the profile
        and (when scored) require an admitted profile with an intact receipt.
        Legacy argv operations are allowed only as unscored
        calibration/diagnostic work.
        """
        profile_id = spec.get("profile_id")
        if not profile_id:
            return list(spec["argv"]), int(spec.get("deadline_seconds", 120)), \
                "unscored: no profile bound"
        from agent_exec.profiles import load_profile, verify_admission
        profile_path = self.profiles_dir / f"{profile_id}.json"
        if not profile_path.is_file():
            raise JobError(f"bound profile not found: {profile_id}")
        profile = load_profile(profile_path)
        scored = bool(spec.get("scored", False))
        errors = verify_admission(profile, scored=scored, profile_path=profile_path)
        if errors:
            raise JobError(f"admission enforcement failed for {profile_id}: "
                           + "; ".join(errors))
        source_root = spec.get("source_root")
        if not source_root:
            raise JobError(f"operation {operation!r} binds a profile but has "
                           "no source_root")
        rendered = render(profile, source_root=str(source_root), job_id=request_id,
                          report_dir=str(report_dir),
                          selectors=spec.get("selectors") or None)
        return (list(rendered.argv), rendered.deadlines["hard_seconds"],
                rendered.cwd, f"admitted:{profile_id}")

    def validate_request_id(self, request_id: str) -> str:
        """Restrictive id policy: it becomes part of a filesystem path."""
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise JobError("invalid request_id: must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        return request_id

    def job_reports(self, job_id: str, receipt: dict) -> list[dict]:
        """Parse per-job reports according to the bound profile's report
        definitions (vitest-json | junit | go-test-json). Legacy unscored
        operations fall back to probing vitest-json.json."""
        from agent_exec import reports as report_reader
        from agent_exec.contracts import ProfileError
        from agent_exec.profiles import load_profile
        row = self.controller.store.get_job(job_id)
        if row is None:
            return []
        job_reports_dir = (self.controller.evidence_root / "jobs" /
                           f"{row['session_id']}-{row['request_id']}" / "reports")
        definitions = []
        profile_id = row.get("profile_id")
        if profile_id:
            profile_path = self.profiles_dir / f"{profile_id}.json"
            if profile_path.is_file():
                try:
                    definitions = load_profile(profile_path).get("reports", [])
                except ProfileError:
                    definitions = []
        summaries = []
        if definitions:
            for report in definitions:
                path = Path(report["path_template"].format(
                    report_dir=str(job_reports_dir), job_id=row["request_id"]))
                kind = report.get("kind")
                if kind == "vitest-json":
                    summaries.append(report_reader.read_vitest_json(path))
                elif kind == "junit":
                    summaries.append(report_reader.read_junit_xml(path))
                elif kind == "go-test-json":
                    summaries.append(report_reader.read_go_test_json(path))
                else:
                    summaries.append({"kind": kind, "integrity": "unsupported",
                                      "complete": False})
            return summaries
        fallback = job_reports_dir / "vitest-json.json"
        if fallback.is_file():
            summaries.append(report_reader.read_vitest_json(fallback))
        return summaries

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
        if "argv" in spec and spec.get("scored"):
            raise JobError("scored operations must bind an admitted profile, "
                           "not a raw argv")
        # Per-job report dir: "{report_dir}" tokens in the operation argv are
        # replaced with a job-owned path so concurrent jobs never share files.
        # Defense in depth beyond the request_id regex: the resolved path must
        # stay inside the evidence root.
        report_dir = self.job_dir_for(self.controller.session_id,
                                      request_id) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        if "argv" in spec and not spec.get("profile_id"):
            argv = [part.replace("{report_dir}", str(report_dir))
                    for part in spec["argv"]]
        else:
            argv, profile_deadline, profile_cwd, origin = \
                self.resolve_operation_command(
                    operation, spec, request_id, report_dir)
            deadline = min(deadline if requested is not None else profile_deadline,
                           profile_deadline, self.max_deadline_seconds)
            # The profile is the authority for the working directory of
            # profile-bound operations.
            cwd = profile_cwd
        result = self.controller.launch(
            argv, request_id=str(request_id), operation_id=operation,
            deadline_seconds=deadline, stop_grace_seconds=5, env=env,
            profile_id=spec.get("profile_id"), cwd=cwd)
        result["command_origin"] = ("admitted:" + spec["profile_id"]
                                    if spec.get("profile_id") else
                                    "unscored:legacy-argv")
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
            try:
                self._do_get()
            except JobError as error:
                self._json(400, {"error": str(error)})
            except (ValueError, KeyError) as error:
                self._json(400, {"error": f"{type(error).__name__}: {error}"})
            except Exception as error:  # noqa: BLE001 — never drop the socket
                self._json(500, {"error": f"{type(error).__name__}: {error}"})

        def _do_get(self):
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
                    receipt["reports"] = supervisor.job_reports(job_id, receipt)
                    self._json(200, receipt)
                except JobError as error:
                    self._json(404, {"error": str(error)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if not self.path.startswith("/jobs"):
                    self._json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    self._json(400, {"error": "body must be a JSON object"})
                    return
                self._json(200, supervisor.launch(body))
            except JobError as error:
                self._json(400, {"error": str(error)})
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                self._json(400, {"error": f"{type(error).__name__}: {error}"})
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
