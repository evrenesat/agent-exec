"""Durable job journal and event cursor (SQLite, stdlib only).

One authority for application-level job state: admitted requests, request-ID
idempotency, terminal receipts and the per-job event revision cursor. systemd
remains authoritative for process lifetime; this store never invents outcomes.
History and live ownership are separate: rows survive controller restarts, and
reconciliation — not optimism — moves them to terminal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    profile_id TEXT,
    purpose TEXT NOT NULL,
    unit TEXT NOT NULL,
    boot_id TEXT,
    invocation_id TEXT,
    state TEXT NOT NULL,
    launch_lease_until REAL,
    outcome TEXT,
    exit_record_json TEXT,
    cleanup TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    launch_argv_json TEXT,
    job_dir TEXT,
    deadline_seconds INTEGER,
    stop_grace_seconds INTEGER,
    created_wall REAL NOT NULL,
    started_wall REAL,
    terminal_wall REAL,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_session_request
    ON jobs (session_id, request_id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    revision INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT,
    created_wall REAL NOT NULL,
    UNIQUE (job_id, revision)
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- jobs ---------------------------------------------------------------
    def create_job(self, *, session_id: str, request_id: str, operation_id: str,
                   profile_id: str | None, purpose: str, unit: str, job_dir: Path,
                   deadline_seconds: int, stop_grace_seconds: int) -> str:
        job_id = "job-" + uuid.uuid4().hex[:16]
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO jobs (job_id, session_id, request_id, operation_id,"
                    " profile_id, purpose, unit, state, job_dir, deadline_seconds,"
                    " stop_grace_seconds, created_wall, boot_id, launch_lease_until)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, session_id, request_id, operation_id, profile_id, purpose,
                     unit, "starting", str(job_dir), deadline_seconds,
                     stop_grace_seconds, time.time(), None, None))
                self._db.commit()
            except sqlite3.IntegrityError:
                row = self._db.execute(
                    "SELECT job_id FROM jobs WHERE session_id=? AND request_id=?",
                    (session_id, request_id)).fetchone()
                raise DuplicateRequest(row["job_id"]) from None
        return job_id

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE job_id=?",
                                   (job_id,)).fetchone()
        return dict(row) if row else None

    def find_by_request(self, session_id: str, request_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE session_id=? AND request_id=?",
                (session_id, request_id)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {columns} WHERE job_id=?",
                             (*fields.values(), job_id))
            self._db.commit()

    def update_job_if_state(self, job_id: str, states: tuple[str, ...],
                            **fields) -> bool:
        """Compare-and-set: apply fields only while the row is in one of
        `states`. Returns False (and changes nothing) when another writer
        already moved the row past them — terminal results are never
        overwritten by a delayed launch update."""
        if not fields:
            return True
        columns = ", ".join(f"{name}=?" for name in fields)
        placeholders = ", ".join("?" for _ in states)
        with self._lock:
            cursor = self._db.execute(
                f"UPDATE jobs SET {columns} WHERE job_id=? AND state IN ({placeholders})",
                (*fields.values(), job_id, *states))
            changed = cursor.rowcount > 0
            self._db.commit()
        return changed

    def open_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE state NOT IN ('completed')").fetchall()
        return [dict(row) for row in rows]

    # -- events -------------------------------------------------------------
    WAKE_KINDS = ("launched", "terminal", "cancelled", "reconciled")

    def record_event(self, job_id: str, kind: str, payload: dict | None = None,
                     *, wake: bool | None = None) -> int:
        """Append one event; deduplicates wake-capable events by (job, revision).

        Wake-capable events (launched/terminal/cancelled/reconciled) get the
        job's next revision and are unique per revision. Status-only
        observations carry a NULL revision: they are always recorded for the
        cursor but never satisfy a revision wait.
        """
        if wake is None:
            wake = kind in self.WAKE_KINDS
        with self._lock:
            if wake:
                row = self._db.execute(
                    "SELECT COALESCE(MAX(revision), 0) AS r FROM events"
                    " WHERE job_id=?", (job_id,)).fetchone()
                revision = row["r"] + 1
                try:
                    cursor = self._db.execute(
                        "INSERT INTO events (job_id, revision, kind, payload_json,"
                        " created_wall) VALUES (?,?,?,?,?)",
                        (job_id, revision, kind, json.dumps(payload or {}),
                         time.time()))
                    self._db.commit()
                    return int(cursor.lastrowid)
                except sqlite3.IntegrityError:
                    row = self._db.execute(
                        "SELECT seq FROM events WHERE job_id=? AND revision=?"
                        " ORDER BY seq LIMIT 1", (job_id, revision)).fetchone()
                    return int(row["seq"]) if row else 0
            cursor = self._db.execute(
                "INSERT INTO events (job_id, revision, kind, payload_json,"
                " created_wall) VALUES (?,?,?,?,?)",
                (job_id, None, kind, json.dumps(payload or {}), time.time()))
            self._db.commit()
            return int(cursor.lastrowid)

    def events_after(self, job_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, after_seq)).fetchall()
        return [dict(row) for row in rows]


class DuplicateRequest(Exception):
    def __init__(self, existing_job_id: str):
        super().__init__(existing_job_id)
        self.existing_job_id = existing_job_id
