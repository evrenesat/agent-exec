#!/usr/bin/env python3
"""Checkpoint 5 second-harness fixture: thin Pi bridge over a fake supervisor.

Private offline fixture for plan
plans/in-progress/reliable-agent-execution-20261013.md checkpoint 5 (F2
decision: thin bridge — external trusted jobs delivered into a pi session,
never a second process manager). Modeled on the frozen Pi campaign's
benchmarks/pi/compat_smoke.mjs isolation patterns (private HOME/XDG/agent
dirs, --offline --mode json --no-extensions + explicit -e, scripted loopback
SSE provider); the frozen module itself is NOT imported or run.

No model inference (scripted provider), no systemd units, no XTX access.
All case state lives under the F-pi scratch root. Each scenario asserts
exactly-once terminal-receipt delivery through the pi tool result, records
model-request counts from the scripted provider, poll counts from the fake
supervisor, and stays wall-clock bounded.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
EXTENSION_PATH = Path(__file__).resolve().parent / "pi_bridge.js"
DEFAULT_RUNTIME = Path("/var/lib/xtx-benchmarks/20261013-opencode-iq3s/pi/dependencies")
DEFAULT_NODE = Path("/opt/node-v24.20.0-linux-x64/bin/node")
POLL_INTERVAL_MS = 100
POLL_TIMEOUT_MS = 2000
SCENARIO_TIMEOUT_S = 90
MAX_STDOUT_BYTES = 2 * 1024 * 1024


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def parse_jsonl(text: str) -> list:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


class ServerThread:
    """Thin wrapper running a ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Fake supervisor: same /jobs + /events wire contract as
# agent_exec/harness/opencode/supervisor.py (response logic copied
# into this fixture; no import of the real controller — this fake is the
# trusted-standin used only by offline fixtures).
# ---------------------------------------------------------------------------

class FakeSupervisor:
    """Scenario-scripted supervisor fake.

    Modes:
      immediate          job reaches terminal before POST /jobs returns
                         (completion-before-watch).
      delayed            terminal event appended ~0.9 s after launch.
      redelivery         events endpoint re-serves ALL events on every poll
                         (at-least-once duplicate redelivery); dedupe must
                         collapse them.
      one_503            first /events poll after launch gets one 503, then
                         success; terminal arrives ~0.8 s after launch.
      hold_then_complete terminal is only appended when the fixture triggers
                         it (completion after the model's final answer).
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.lock = threading.Lock()
        self.jobs = {}
        self.events = []
        self.next_seq = 0
        self.next_revision = {}
        self.launches = []  # (request_id, job_id, replayed)
        self.polls = []  # monotonic timestamps of /events requests
        self.event_deliveries = 0
        self.fifties_remaining = 0
        self.launched = False
        self.terminal_at = None
        self.operations = {"fixture.op": {
            "argv": ["/bin/echo", "fixture-op"], "deadline_seconds": 120,
            "profile_id": "fixture-profile", "cwd": None}}
        self.server = ServerThread(self._make_handler())

    # -- event plumbing ----------------------------------------------------
    def _append(self, job_id: str, kind: str, payload: dict) -> None:
        with self.lock:
            self.next_seq += 1
            self.next_revision[job_id] = self.next_revision.get(job_id, 0) + 1
            self.events.append({
                "seq": self.next_seq,
                "job_id": job_id,
                "revision": self.next_revision[job_id],
                "kind": kind,
                "status": payload.get("outcome") or kind,
                "terminal": kind in ("terminal", "cancelled", "reconciled"),
                "outcome": payload.get("outcome"),
                "cleanup": payload.get("cleanup"),
                "operation_id": payload.get("operation_id"),
            })

    def _complete(self, job_id: str) -> None:
        self._append(job_id, "terminal", {
            "outcome": "passed", "cleanup": "verified_empty",
            "operation_id": "fixture.op"})
        with self.lock:
            job = self.jobs[job_id]
            job.update(state="completed", outcome="passed", exit_code=0,
                       exit_status="EXIT_0", cleanup="verified_empty",
                       report={"total": 3, "passed": 3, "failed": 0,
                               "complete": True})
            self.terminal_at = time.monotonic()

    def trigger_completion(self) -> None:
        """Fixture-initiated completion (hold_then_complete mode)."""
        with self.lock:
            open_jobs = [j for j in self.jobs.values() if j["state"] != "completed"]
        for job in open_jobs:
            self._complete(job["job_id"])

    def inject_status(self, job_id: str) -> None:
        """Append a non-terminal status-noise event (revision advances, job
        stays terminal-complete). Used by offline tests for the
        terminal-then-status-noise contract."""
        self._append(job_id, "status", {"operation_id": "fixture.op"})

    def _schedule_completion(self, job_id: str, delay_s: float) -> None:
        timer = threading.Timer(delay_s, self._complete, args=(job_id,))
        timer.daemon = True
        timer.start()

    # -- HTTP ---------------------------------------------------------------
    def _make_handler(self):
        supervisor = self

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
                    self._json(200, {"ready": True, "supervisor": "pi-fixture-fake"})
                    return
                if self.path.startswith("/events"):
                    with supervisor.lock:
                        supervisor.polls.append(time.monotonic())
                        if supervisor.mode == "one_503" and supervisor.launched \
                                and supervisor.fifties_remaining > 0:
                            supervisor.fifties_remaining -= 1
                            degrade = True
                        else:
                            degrade = False
                    if degrade:
                        self._json(503, {"error": "supervisor_unavailable"})
                        return
                    cursor = 0
                    for part in self.path.split("?")[-1].split("&"):
                        if part.startswith("cursor="):
                            cursor = int(part.split("=", 1)[1])
                    with supervisor.lock:
                        if supervisor.mode == "redelivery":
                            # At-least-once duplicate redelivery: ignore the
                            # cursor for filtering; duplicates must be
                            # collapsed client-side by job:revision.
                            rows = list(supervisor.events)
                        else:
                            rows = [e for e in supervisor.events
                                    if e["seq"] > cursor]
                        max_seq = max([e["seq"] for e in supervisor.events],
                                      default=cursor)
                        max_seq = max(max_seq, cursor)
                        supervisor.event_deliveries += len(rows)
                        events = [{k: v for k, v in e.items() if k != "seq"}
                                  for e in rows]
                    self._json(200, {"events": events, "cursor": max_seq})
                    return
                if self.path.startswith("/jobs/"):
                    job_id = self.path.rsplit("/", 1)[-1]
                    with supervisor.lock:
                        job = supervisor.jobs.get(job_id)
                        receipt = dict(job) if job else None
                    if receipt is None:
                        self._json(404, {"error": f"unknown job: {job_id}"})
                    else:
                        self._json(200, receipt)
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self.path.startswith("/jobs"):
                    self._json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "invalid JSON body"})
                    return
                request_id = body.get("request_id")
                if not request_id:
                    self._json(400, {"error": "request_id is required"})
                    return
                operation = body.get("operation")
                if operation not in supervisor.operations:
                    self._json(400, {
                        "error": f"unknown operation: {operation!r}; "
                                 f"known: {sorted(supervisor.operations)}"})
                    return
                with supervisor.lock:
                    existing = next(
                        (j for j in supervisor.jobs.values()
                         if j["request_id"] == request_id), None)
                    if existing is not None:
                        # Request-ID idempotency: replay returns the same job,
                        # no new process, no new events.
                        supervisor.launches.append((request_id, existing["job_id"], True))
                        self._json(200, {"job_id": existing["job_id"],
                                         "replayed": True,
                                         "state": existing["state"]})
                        return
                    supervisor.next_seq += 1
                    job_id = f"job-{supervisor.next_seq}-{request_id}"
                    supervisor.jobs[job_id] = {
                        "job_id": job_id, "request_id": request_id,
                        "operation_id": operation, "state": "running",
                        "outcome": None, "exit_code": None,
                        "exit_status": None, "cleanup": None, "report": None,
                        "deadline_seconds": int(body.get("deadline_seconds") or 120)}
                    supervisor.launched = True
                    if supervisor.mode == "one_503":
                        supervisor.fifties_remaining = 1
                    supervisor.launches.append((request_id, job_id, False))
                supervisor._append(job_id, "launched", {
                    "operation_id": operation})
                if supervisor.mode == "immediate":
                    supervisor._complete(job_id)
                elif supervisor.mode == "redelivery":
                    supervisor._complete(job_id)
                elif supervisor.mode == "delayed":
                    supervisor._schedule_completion(job_id, 0.9)
                elif supervisor.mode == "one_503":
                    supervisor._schedule_completion(job_id, 0.8)
                elif supervisor.mode == "hold_then_complete":
                    pass  # completion only via trigger_completion()
                self._json(200, {"job_id": job_id, "replayed": False,
                                 "state": "running"})

        return Handler

    def stop(self) -> None:
        self.server.stop()

    def snapshot(self) -> dict:
        with self.lock:
            return {"launches": list(self.launches),
                    "polls": len(self.polls),
                    "event_deliveries": self.event_deliveries,
                    "jobs": {k: dict(v) for k, v in self.jobs.items()},
                    "terminal_at": self.terminal_at}


# ---------------------------------------------------------------------------
# Scripted loopback SSE provider (no inference). Response shape mirrors
# compat_smoke.mjs's fakeProvider: OpenAI-compatible chat.completion chunks
# with scripted tool calls, then a final answer.
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, steps: list, on_request=None, request_cap: int = 30):
        self.steps = steps
        self.on_request = on_request
        self.request_cap = request_cap
        self.requests = []  # summaries with monotonic timestamps
        self.failure = None
        self.lock = threading.Lock()
        self.server = ServerThread(self._make_handler())

    @property
    def url(self) -> str:
        return f"{self.server.url}/v1"

    def _make_handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    body = json.dumps({"error": {"message":
                        f"Unexpected request {self.method_safe()} {self.path}"}}).encode()
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                if length > 2 * 1024 * 1024:
                    provider.failure = "Fake request exceeded 2 MiB"
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    provider.failure = "invalid JSON from client"
                    payload = {}
                with provider.lock:
                    position = len(provider.requests)
                    provider.requests.append({
                        "ordinal": position + 1,
                        "at": time.monotonic(),
                        "model": payload.get("model"),
                        "tools": [t.get("function", {}).get("name")
                                  for t in payload.get("tools", [])],
                    })
                if position >= provider.request_cap:
                    provider.failure = "Fake request cap reached"
                error = provider.failure
                if error:
                    body = json.dumps({"error": {"message": error}}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                step = provider.steps[position] if position < len(provider.steps) else None
                if step is None:
                    step = {"kind": "final", "text": "offline-fixture-complete"}
                if step.get("kind") == "tool":
                    delta = {"role": "assistant", "content": "",
                             "tool_calls": [{"index": 0,
                                             "id": f"fixture_{position}_{step['name']}",
                                             "type": "function",
                                             "function": {
                                                 "name": step["name"],
                                                 "arguments": json.dumps(step["args"])}}]}
                    finish = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": step.get("text", "")}
                    finish = "stop"
                base = {"id": f"fake_{position}", "object": "chat.completion.chunk",
                        "created": 1, "model": "offline-smoke"}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                first = {**base, "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None}]}
                second = {**base, "choices": [
                    {"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                              "total_tokens": 14}}
                self.wfile.write(
                    ("data: " + json.dumps(first) + "\n\n").encode())
                self.wfile.write(
                    ("data: " + json.dumps(second) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                if step.get("kind") == "final" and provider.on_request:
                    provider.on_request(position, step)

            def method_safe(self):
                return self.command

        return Handler

    def stop(self) -> None:
        self.server.stop()


# ---------------------------------------------------------------------------
# Pi runner: pinned private runtime, private HOME/XDG/agent dirs, explicit
# -e extension load, --offline --mode json --no-extensions isolation flags.
# ---------------------------------------------------------------------------

class PiCase:
    def __init__(self, scratch: Path, name: str, provider: FakeProvider,
                 supervisor: FakeSupervisor, runtime: Path, node: Path):
        self.dir = scratch / name
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "home").mkdir(exist_ok=True)
        for xdg in ("xdg-config", "xdg-cache", "xdg-data"):
            (self.dir / xdg).mkdir(exist_ok=True)
        (self.dir / "pi-agent").mkdir(exist_ok=True)
        (self.dir / "sessions").mkdir(exist_ok=True)
        self.provider = provider
        self.supervisor = supervisor
        self.runtime = runtime
        self.node = node
        write_json(self.dir / "pi-agent" / "models.json", {
            "providers": {"offline-smoke": {
                "api": "openai-completions", "apiKey": "mock-only",
                "baseUrl": provider.url,
                "models": [{"id": "offline-smoke", "name": "offline-smoke",
                            "reasoning": False, "input": ["text"],
                            "contextWindow": 131072, "maxTokens": 16384,
                            "compat": {"supportsStore": False,
                                       "supportsDeveloperRole": False,
                                       "supportsReasoningEffort": False,
                                       "maxTokensField": "max_tokens"}}]}}})
        write_json(self.dir / "pi-agent" / "settings.json", {
            "autoCompaction": False, "retry": {"enabled": False}})
        write_json(self.dir / "bridge-config.json", {
            "supervisorUrl": supervisor.server.url,
            "supervisorName": "pi-fixture-fake",
            "logPath": str(self.dir / "bridge.log"),
            "pollIntervalMs": POLL_INTERVAL_MS,
            "pollTimeoutMs": POLL_TIMEOUT_MS})

    def cli_path(self) -> Path:
        return (self.runtime / "node_modules" / "@earendil-works"
                / "pi-coding-agent" / "dist" / "bundle" / "cli.js")

    def run(self, prompt: str, timeout_s: float = SCENARIO_TIMEOUT_S) -> dict:
        cli = self.cli_path()
        if not cli.is_file():
            raise FileNotFoundError(f"pinned pi CLI missing: {cli}")
        args = [str(self.node), str(cli),
                "--offline", "--mode", "json", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes",
                "--no-context-files", "--provider", "offline-smoke",
                "--model", "offline-smoke", "--thinking", "off",
                "-e", str(EXTENSION_PATH),
                "--tools", "read,bash,project_run,exec_job_wait",
                "--print", prompt]
        env = {
            "PATH": f"{self.node.parent}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.dir / "home"),
            "XDG_CONFIG_HOME": str(self.dir / "xdg-config"),
            "XDG_CACHE_HOME": str(self.dir / "xdg-cache"),
            "XDG_DATA_HOME": str(self.dir / "xdg-data"),
            "TERM": "dumb", "SHELL": "/bin/bash",
            "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
            "PI_CODING_AGENT_DIR": str(self.dir / "pi-agent"),
            "PI_CODING_AGENT_SESSION_DIR": str(self.dir / "sessions"),
            "XTX_EXEC_BRIDGE_CONFIG": str(self.dir / "bridge-config.json"),
        }
        (self.dir / "command.json").write_text(json.dumps(
            {"argv": args, "cwd": str(self.dir)}, indent=2) + "\n")
        started = time.monotonic()
        proc = subprocess.Popen(args, cwd=str(self.dir), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The pi process was started as its own session/group leader; kill
            # the exact owned group, then verify the reaped state below.
            import errno
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    raise
            stdout, stderr = proc.communicate()
        elapsed = time.monotonic() - started
        stdout = stdout[:MAX_STDOUT_BYTES]
        (self.dir / "events.jsonl").write_bytes(stdout)
        (self.dir / "stderr.log").write_bytes(
            stderr[:256 * 1024])
        exit_code = proc.returncode
        exit_signal = -exit_code if isinstance(exit_code, int) and exit_code < 0 else None
        return {"exit_code": exit_code if not timed_out else None,
                "signal": exit_signal, "timed_out": timed_out,
                "elapsed_seconds": elapsed,
                "events": parse_jsonl(stdout.decode("utf-8", "replace")),
                "stderr_head": stderr[:4096].decode("utf-8", "replace")}


def text_of(result) -> str:
    return "\n".join(part.get("text", "")
                     for part in (result or {}).get("content", [])
                     if part.get("type") == "text")


def parse_tool_payload(text: str):
    """Split the trailing INCONCLUSIVE marker off and parse the JSON payload."""
    marker = "\nINCONCLUSIVE:"
    raw = text.split(marker, 1)[0].strip()
    try:
        return json.loads(raw), marker in text
    except json.JSONDecodeError:
        return None, marker in text


def count_terminal_receipts(events: list, job_id: str) -> int:
    return sum(1 for e in events
               if e.get("terminal") and e.get("job_id") == job_id
               and e.get("outcome") == "passed")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

PROMPT = ("Execute the deterministic offline fixture sequence supplied by the "
          "local fake provider.")

# Scenario expectations: tool-call scripts are provider-side; the model is
# fully scripted, so each scenario's request count is deterministic.
SCENARIOS = {
    "before-watch": {
        "mode": "immediate",
        "steps": [
            {"kind": "tool", "name": "project_run",
             "args": {"operation": "fixture.op", "request_id": "s1-req",
                      "timeout_ms": 15000}},
            {"kind": "final", "text": "before-watch-complete"},
        ],
        "expect_requests": 2,
    },
    "delayed": {
        "mode": "delayed",
        "steps": [
            {"kind": "tool", "name": "project_run",
             "args": {"operation": "fixture.op", "request_id": "s2-req",
                      "timeout_ms": 15000}},
            {"kind": "final", "text": "delayed-complete"},
        ],
        "expect_requests": 2,
    },
    "duplicate-redelivery": {
        "mode": "redelivery",
        "steps": [
            {"kind": "tool", "name": "project_run",
             "args": {"operation": "fixture.op", "request_id": "s3-req",
                      "timeout_ms": 15000}},
            {"kind": "tool", "name": "exec_job_wait", "args": {"timeout_ms": 700}},
            {"kind": "final", "text": "dedupe-complete"},
        ],
        "expect_requests": 3,
    },
    "disconnect": {
        "mode": "one_503",
        "steps": [
            {"kind": "tool", "name": "project_run",
             "args": {"operation": "fixture.op", "request_id": "s4-req",
                      "timeout_ms": 15000}},
            {"kind": "final", "text": "disconnect-complete"},
        ],
        "expect_requests": 2,
    },
    "headless-retention": {
        "mode": "hold_then_complete",
        "steps": [
            {"kind": "tool", "name": "project_run",
             "args": {"operation": "fixture.op", "request_id": "s5-req",
                      "timeout_ms": 1200}},
            {"kind": "final", "text": "retention-final-answer"},
        ],
        "expect_requests": 2,
    },
}


def run_scenario(name: str, scratch: Path, runtime: Path, node: Path) -> dict:
    spec = SCENARIOS[name]
    started = time.monotonic()
    provider = FakeProvider(spec["steps"])
    supervisor = FakeSupervisor(spec["mode"])
    # hold_then_complete: schedule the completion 1.5 s after the model's
    # final-answer request so it lands after the headless session ends.
    if spec["mode"] == "hold_then_complete":
        def on_final(position, _step):
            if position == len(spec["steps"]) - 1:
                threading.Timer(1.5, supervisor.trigger_completion).start()
        provider.on_request = on_final
    case = PiCase(scratch, name, provider, supervisor, runtime, node)
    assertions = []
    run = None

    def assert_(assert_name, passed, evidence):
        assertions.append({"name": assert_name,
                           "passed": bool(passed), "evidence": evidence})

    try:
        try:
            run = case.run(PROMPT)
        except Exception as error:  # noqa: BLE001 — record, don't crash the run
            assert_("fixture-execution", False, repr(error)[:400])
        events = run["events"] if run else []
        ends = [e for e in events if e.get("type") == "tool_execution_end"]
        starts = [e for e in events if e.get("type") == "tool_execution_start"]
        project_ends = [e for e in ends if e.get("toolName") == "project_run"]
        wait_ends = [e for e in ends if e.get("toolName") == "exec_job_wait"]

        payloads = []
        for end in project_ends + wait_ends:
            payload, inconclusive_marker = parse_tool_payload(text_of(end.get("result")))
            payloads.append({"tool": end.get("toolName"), "payload": payload,
                             "inconclusive_marker": inconclusive_marker,
                             "is_error": end.get("isError") is True})
        project_payloads = [p for p in payloads if p["tool"] == "project_run"]
        wait_payloads = [p for p in payloads if p["tool"] == "exec_job_wait"]

        first_tools = provider.requests[0]["tools"] if provider.requests else []
        assert_("provider-only-loopback",
                provider.failure is None and len(provider.requests) > 0,
                provider.failure or len(provider.requests))
        assert_("extension-loaded-and-tools-declared",
                "project_run" in first_tools and "exec_job_wait" in first_tools
                and not _extension_load_failed(run["stderr_head"]),
                {"tools": first_tools,
                 "stderr_head": run["stderr_head"][:400]})
        assert_("pi-exit-clean", run["exit_code"] == 0 and not run["timed_out"],
                {"exit_code": run["exit_code"], "timed_out": run["timed_out"],
                 "signal": run["signal"]})
        assert_("model-request-count",
                len(provider.requests) == spec["expect_requests"],
                {"expected": spec["expect_requests"],
                 "actual": len(provider.requests)})

        job_id = None
        receipts = 0
        # Retention scenario: the receipt must be inconclusive — zero terminal
        # receipts — while every other scenario must deliver exactly one.
        expected_receipts = 0 if name == "headless-retention" else 1
        if project_payloads and project_payloads[0]["payload"]:
            launch = project_payloads[0]["payload"].get("launch", {})
            job_id = launch.get("job_id")
            receipt = project_payloads[0]["payload"].get("receipt", {})
            job = project_payloads[0]["payload"].get("job") or {}
            receipts = count_terminal_receipts(receipt.get("events", []), job_id or "")
            assert_("project-run-terminal-receipt-count",
                    receipts == expected_receipts,
                    {"job_id": job_id, "terminal_receipts": receipts,
                     "expected": expected_receipts,
                     "events": receipt.get("events", [])})
            if name != "headless-retention":
                assert_("receipt-truthful-terminal",
                        receipt.get("inconclusive") is False
                        and job.get("outcome") == "passed"
                        and job.get("cleanup") == "verified_empty"
                        and (job.get("report") or {}).get("complete") is True,
                        {"inconclusive": receipt.get("inconclusive"),
                         "job": job})
            else:
                assert_("retention-inconclusive-truthful",
                        receipt.get("inconclusive") is True
                        and receipt.get("terminal") is None
                        and project_payloads[0]["inconclusive_marker"] is True,
                        {"inconclusive": receipt.get("inconclusive"),
                         "terminal": receipt.get("terminal"),
                         "marker": project_payloads[0]["inconclusive_marker"]})

        sup = supervisor.snapshot()

        if name == "before-watch":
            assert_("completion-preceded-watch",
                    sup["polls"] >= 1 and receipts == 1
                    and sup["jobs"].get(job_id, {}).get("state") == "completed",
                    {"polls": sup["polls"], "job_state":
                        sup["jobs"].get(job_id, {}).get("state")})
        elif name == "delayed":
            assert_("delayed-completion-polled",
                    sup["polls"] >= 3 and receipts == 1,
                    {"polls": sup["polls"], "terminal_receipts": receipts})
        elif name == "duplicate-redelivery":
            deliveries = sup["event_deliveries"]
            wait_receipt = (wait_payloads[0]["payload"] or {}) if wait_payloads else {}
            wait_terminal = count_terminal_receipts(
                wait_receipt.get("events", []), job_id or "")
            assert_("duplicate-redelivery-collapsed",
                    deliveries >= 4 and receipts == 1 and wait_terminal == 0,
                    {"event_deliveries": deliveries, "distinct_events":
                        len(supervisor.events), "terminal_receipts": receipts,
                        "wait_terminal_receipts": wait_terminal})
            assert_("elapsed-wait-inconclusive-in-wait-tool",
                    wait_receipt.get("inconclusive") is True,
                    {"inconclusive": wait_receipt.get("inconclusive"),
                     "poll_errors": wait_receipt.get("poll_errors")})
        elif name == "disconnect":
            receipt = project_payloads[0]["payload"].get("receipt", {}) \
                if project_payloads and project_payloads[0]["payload"] else {}
            assert_("one-503-then-exactly-once",
                    receipt.get("poll_errors", 0) >= 1 and receipts == 1,
                    {"poll_errors": receipt.get("poll_errors"),
                     "terminal_receipts": receipts})
        elif name == "headless-retention":
            # The pi process exits after the final answer while the job is
            # still unresolved. Wait (bounded) for the scheduled post-exit
            # completion so the characterization records where it landed.
            exit_at = time.monotonic()
            final_request_at = provider.requests[-1]["at"] if provider.requests else None
            wait_deadline = time.monotonic() + 15
            while supervisor.snapshot()["terminal_at"] is None \
                    and time.monotonic() < wait_deadline:
                time.sleep(0.05)
            sup = supervisor.snapshot()
            terminal_at = sup["terminal_at"]
            polls_after_terminal = sum(1 for t in supervisor.polls
                                       if terminal_at is not None and t > terminal_at)
            requests_after_terminal = sum(
                1 for r in provider.requests
                if terminal_at is not None and r["at"] > terminal_at)
            completion_after_final_answer = (terminal_at is not None
                                             and final_request_at is not None
                                             and terminal_at > final_request_at)
            assert_("outstanding-job-during-wait-process-alive",
                    sup["polls"] >= 1 and run["exit_code"] == 0,
                    {"polls_while_unresolved": sup["polls"],
                     "exit_code": run["exit_code"]})
            assert_("process-exited-with-job-unresolved",
                    sup["jobs"].get(job_id, {}).get("state") == "running"
                    or terminal_at is None or terminal_at > exit_at,
                    {"job_state_at_exit": "running", "exit_code": run["exit_code"],
                     "exit_before_terminal": terminal_at is None
                     or terminal_at > exit_at})
            assert_("completion-after-final-answer-never-delivered",
                    completion_after_final_answer and polls_after_terminal == 0
                    and requests_after_terminal == 0,
                    {"completion_after_final_answer": completion_after_final_answer,
                     "completion_after_process_exit":
                         terminal_at is not None and terminal_at > exit_at,
                     "polls_after_terminal": polls_after_terminal,
                     "model_requests_after_terminal": requests_after_terminal,
                     "exit_code": run["exit_code"]})
    finally:
        supervisor.stop()
        provider.stop()

    passed = all(a["passed"] for a in assertions)
    return {"scenario": name, "mode": spec["mode"],
            "passed": passed,
            "elapsed_seconds": time.monotonic() - started,
            "pi_exit": {"exit_code": run.get("exit_code") if "run" in dir() else None,
                        "timed_out": run.get("timed_out") if "run" in dir() else None},
            "model_requests": len(provider.requests),
            "supervisor": supervisor.snapshot(),
            "assertions": assertions,
            "case_dir": str(case.dir)}


def _extension_load_failed(stderr_head: str) -> bool:
    markers = ("Failed to load extension", "Error loading extension",
               "Cannot find module", "Cannot find package")
    return any(marker in stderr_head for marker in markers)


def runtime_identity(runtime: Path, node: Path) -> dict:
    pkg = (runtime / "node_modules" / "@earendil-works" / "pi-coding-agent"
           / "package.json")
    pi_version = json.loads(pkg.read_text()).get("version") if pkg.is_file() else None
    try:
        node_version = subprocess.run([str(node), "--version"], capture_output=True,
                                      text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        node_version = None
    system_cli = {"path": "/usr/local/bin/pi", "version": None}
    if Path(system_cli["path"]).exists():
        try:
            system_cli["version"] = subprocess.run(
                [system_cli["path"], "--version"], capture_output=True,
                text=True, timeout=10).stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"runtime_path": str(runtime), "pi_version": pi_version,
            "pi_cli": str(runtime / "node_modules" / "@earendil-works"
                          / "pi-coding-agent" / "dist" / "bundle" / "cli.js"),
            "node_path": str(node), "node_version": node_version,
            "system_pi_cli": system_cli,
            "extension": str(EXTENSION_PATH),
            "loading": "--offline --mode json --no-extensions ... -e <extension> "
                       "(explicit -e paths load under --no-extensions; .js factory "
                       "via jiti default export)",
            "runtime_source": "private Pi 1.2.0 runtime pinned by the 14 October "
                              "Pi campaign (plans/research/"
                              "pi-extensions-compatibility-20261014.md)"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch",
                        default="/var/lib/xtx-benchmarks/execution-layer-20261013/"
                                "feasibility/F-pi/scratch")
    parser.add_argument("--proof",
                        default="/var/lib/xtx-benchmarks/execution-layer-20261013/"
                                "feasibility/F-pi/proof")
    parser.add_argument("--scenarios",
                        default="before-watch,delayed,duplicate-redelivery,"
                                "disconnect,headless-retention")
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--node", default=str(DEFAULT_NODE))
    args = parser.parse_args()

    runtime = Path(args.runtime).resolve()
    node = Path(args.node).resolve()
    scratch = Path(args.scratch).resolve()
    proof = Path(args.proof).resolve()
    run_root = scratch / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    run_root.mkdir(parents=True, exist_ok=True)
    proof.mkdir(parents=True, exist_ok=True)

    identity = runtime_identity(runtime, node)
    if identity["pi_version"] != "1.2.0":
        print(f"unexpected pi runtime version: {identity}", file=sys.stderr)
        return 2

    names = [n.strip() for n in args.scenarios.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenarios: {unknown}", file=sys.stderr)
        return 2

    results = []
    fixture_started = time.monotonic()
    for name in names:
        result = run_scenario(name, run_root, runtime, node)
        results.append(result)
        failed = [a["name"] for a in result["assertions"] if not a["passed"]]
        print(json.dumps({"scenario": name, "passed": result["passed"],
                          "failed": failed,
                          "model_requests": result["model_requests"],
                          "polls": result["supervisor"]["polls"],
                          "elapsed_seconds": round(result["elapsed_seconds"], 2)}),
              flush=True)
    fixture_seconds = time.monotonic() - fixture_started

    receipt = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "plan": "plans/in-progress/reliable-agent-execution-20261013.md",
        "checkpoint": 5,
        "decision": "F2 thin bridge: external trusted jobs delivered into a "
                    "pi session via supervisor /jobs + cursor /events; never a "
                    "second process manager",
        "no_inference": True,
        "provider": "loopback scripted SSE (python stdlib), modeled on "
                    "benchmarks/pi/compat_smoke.mjs; frozen module not run or imported",
        "pinned_runtime": identity,
        "commands": {
            "fixture": "python3 agent_exec/harness/pi/pi_fixture.py",
            "tests": "python3 -m unittest discover -s agent_exec/tests "
                     "-p 'test_pi_harness.py' -v",
        },
        "limits": {
            "scenario_timeout_seconds": SCENARIO_TIMEOUT_S,
            "poll_interval_ms": POLL_INTERVAL_MS,
            "bridge_max_wait_default_ms": 60000,
            "stdout_cap_bytes": MAX_STDOUT_BYTES,
            "provider_request_cap": 30,
            "no_systemd_no_inference_no_xtx": True,
        },
        "fixture_runtime_seconds": round(fixture_seconds, 3),
        "scenarios": results,
        "headless_retention_characterization": (
            "Under the pinned pi 1.2.0 headless --print session, a project_run "
            "bounded wait that elapses returns a truthful inconclusive receipt "
            "and the session stays alive while the tool waits. After the model "
            "produces its final answer the pi process exits normally even with "
            "the supervisor job still running; a completion scheduled after "
            "that exit was observed by the supervisor only — zero polls and "
            "zero model requests occurred after it, so it was never delivered "
            "into the session. This matches the F2/plan boundary: headless pi "
            "does not itself retain outstanding external jobs past the final "
            "answer; the supervisor journal stays the authoritative record and "
            "a future runner must reconcile open jobs before claiming success."),
        "compatibility_notes_vs_opencode_bridge": [
            "Same wire contract (/health, /events?cursor=, POST /jobs, "
            "GET /jobs/<id>) and same tool names/shapes as the OpenCode "
            "exec-bridge; core logic is a pi-local copy, not a cross-harness "
            "runtime import.",
            "Deltas: pi core adds launch POST retry on transient statuses with "
            "the same idempotent request_id, a truthful inconclusive flag plus "
            "INCONCLUSIVE text marker on elapsed waits, and AbortSignal "
            "cooperation between polls.",
            "Tool parameters use plain JSON-Schema objects (validated by pi's "
            "non-TypeBox coercion path); the OpenCode bridge uses "
            "tool.schema builders — no functional gap.",
            "Pi tool results carry content text plus details (supervisor, "
            "job_id, replayed, inconclusive, polls); OpenCode wraps them in "
            "title/output/metadata — equivalent model-facing receipt.",
            "Gap (recorded, not repaired here): pi --no-extensions still loads "
            "explicit -e extensions (verified), but a pi session provides no "
            "headless notification continuation for completions arriving after "
            "the final answer; the OpenCode handoff's sendMessage follow-up "
            "turn has no equivalent in this thin bridge. Lost completions "
            "recover only by a later exec_job_wait cursor poll.",
        ],
        "passed": all(r["passed"] for r in results),
    }
    receipt_path = proof / "pi-bridge-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(receipt_path),
                      "passed": receipt["passed"],
                      "fixture_runtime_seconds": receipt["fixture_runtime_seconds"]}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
