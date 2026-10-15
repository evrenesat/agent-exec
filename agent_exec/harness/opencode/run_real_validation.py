#!/usr/bin/env python3
"""CK3 real-harness validation: OpenCode 1.18.25 + QFN drives aflow.web.unit
through the supervisor and the native job layer. One bounded session."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

HERE = Path(__file__).resolve().parent          # harness/opencode
MODULE = HERE.parents[1]                        # agent_exec
PROOF = Path("/var/lib/xtx-benchmarks/execution-layer-20261013/ck3-real-harness")
RUNTIME = Path("/tmp/xtx-opencode-plugin-runtime-20261013")
OPENCODE = "/usr/local/bin/opencode"
SOURCE = "/root/code/worktrees/execution-aflow-owned-20261014"
NODE_BIN = "/opt/node-v24.20.0-linux-x64/bin"
RELAY_URL = "http://127.0.0.1:18489/v1"
MODEL = "qfn/qwen3.8-flash-next-iq3_s"
PROMPT = ("Use the project_run tool exactly once to run operation 'aflow.web.unit' "
          "with request_id 'qfn-validate-1'. Wait for its terminal receipt, then "
          "report verbatim: outcome, exit_status, the test totals from the receipt, "
          "and cleanup. Do not run any other tools and do not retry.")


def wait_health(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"supervisor not healthy at {url}")


def main() -> int:
    if PROOF.exists():
        # Preserve failed/unique wiring attempts as numbered evidence.
        attempts = sorted(PROOF.parent.glob(PROOF.name + "-attempt-*"))
        PROOF.rename(PROOF.parent / f"{PROOF.name}-attempt-{len(attempts) + 1}")
    PROOF.mkdir(parents=True)
    session = PROOF / "session"
    config_dir = session / "config"
    work = session / "work"
    for path in (config_dir, work, session / "home", session / "tmp"):
        path.mkdir(parents=True)
    operations = {
        "aflow.web.unit": {
            "argv": [f"{NODE_BIN}/npm", "--prefix", "apps/aflow_app/web", "test",
                     "--", "--run", "--maxWorkers=1", "--minWorkers=1",
                     "--pool=forks", "--reporter=json",
                     "--outputFile={report_dir}/vitest-json.json"],
            "cwd": SOURCE,
            "deadline_seconds": 180,
            "profile_id": "aflow-web-p100",
            "env": {"PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
                    "npm_config_audit": "false", "npm_config_fund": "false",
                    "NO_COLOR": "1"},
        },
    }
    (PROOF / "operations.json").write_text(json.dumps(operations, indent=2))
    supervisor = subprocess.Popen(
        [sys.executable, str(HERE / "supervisor.py"),
         "--store", str(PROOF / "supervisor-store.sqlite"),
         "--evidence-root", str(PROOF / "evidence"),
         "--session-label", "qfn-validation",
         "--operations", str(PROOF / "operations.json")],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    line = supervisor.stdout.readline()
    info = json.loads(line)
    supervisor_url = info["listening"]
    token = info["token"]
    wait_health(supervisor_url + "/health")

    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(RUNTIME / name, config_dir / name)
    (config_dir / "node_modules").symlink_to(RUNTIME / "node_modules")
    shutil.copyfile(HERE / "exec_bridge.mjs", config_dir / "exec_bridge.mjs")
    shutil.copyfile(HERE / "exec-bridge-core.mjs", config_dir / "exec-bridge-core.mjs")
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "share": "disabled",
        "autoupdate": False,
        "plugin": [(config_dir / "exec_bridge.mjs").as_uri()],
        "provider": {"qfn": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "QFN via bounded relay",
            "options": {"baseURL": RELAY_URL, "apiKey": "relay-injected"},
            "models": {"qwen3.8-flash-next-iq3_s": {
                "name": "QFN IQ3_S", "tool_call": True,
                "temperature": True,
                "limit": {"context": 131072, "output": 16384}},
            },
        }},
        "agent": {"build": {"temperature": 0.6, "top_p": 0.95}},
    }
    (config_dir / "opencode.json").write_text(json.dumps(config))
    (config_dir / "xtx-exec-bridge.json").write_text(json.dumps({
        "supervisorName": "execution-layer",
        "supervisorUrl": supervisor_url,
        "token": token,
        "pollIntervalMs": 250,
        "pollTimeoutMs": 3000,
        "logPath": str(session / "bridge.jsonl"),
    }))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": str(session / "tmp"),
           "XDG_CONFIG_HOME": str(session / "XDG_CONFIG_HOME"),
           "XDG_DATA_HOME": str(session / "XDG_DATA_HOME"),
           "XDG_CACHE_HOME": str(session / "XDG_CACHE_HOME"),
           "XDG_STATE_HOME": str(session / "XDG_STATE_HOME"),
           "OPENCODE_CONFIG": str(config_dir / "opencode.json"),
           "OPENCODE_CONFIG_DIR": str(config_dir),
           "OPENCODE_TEST_HOME": str(session / "home"),
           "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
           "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1",
           "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
           "OPENCODE_DISABLE_CLAUDE_CODE": "1"}
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        Path(env[key]).mkdir(exist_ok=True)
    started = time.time()
    with (session / "opencode.log").open("w") as log:
        process = subprocess.Popen(
            [OPENCODE, "run", "--format", "json", "--title", "CK3 real-harness validation",
             "--model", MODEL, "--dir", str(work), "--", PROMPT],
            cwd=work, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            rc = process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            rc = "timeout"
    elapsed = time.time() - started
    supervisor.terminate()
    supervisor.wait(timeout=10)

    # Post-hoc audit under the SAME session id the supervisor used: reconcile
    # must reach the launched jobs, and the assertions below decide success.
    from agent_exec.jobs import JobController
    auditor = JobController(PROOF / "supervisor-store.sqlite",
                            evidence_root=PROOF / "evidence",
                            session_id="qfn-validation")
    failures: list[str] = []
    jobs: list[dict] = []
    try:
        auditor.reconcile_all()
        import sqlite3
        db = sqlite3.connect(PROOF / "supervisor-store.sqlite")
        db.row_factory = sqlite3.Row
        jobs = [dict(r) for r in db.execute(
            "SELECT job_id, state, outcome, cleanup FROM jobs")]
        db.close()
        if not jobs:
            failures.append("no jobs launched")
        for job in jobs:
            if job["state"] != "completed":
                failures.append(f"job {job['job_id']} not terminal: {job['state']}")
            if job["outcome"] != "passed":
                failures.append(f"job {job['job_id']} outcome={job['outcome']}")
            if job["cleanup"] != "verified_empty":
                failures.append(f"job {job['job_id']} cleanup={job['cleanup']}")
        # The model must have reported the receipt of its own job.
        texts = []
        for log_line in (session / "opencode.log").read_text().splitlines():
            try:
                event = json.loads(log_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "text":
                texts.append(event["part"]["text"])
        final_text = texts[-1] if texts else ""
        for job in jobs:
            if job["job_id"] not in final_text:
                failures.append(f"model report missing job id {job['job_id']}")
        if "passed" not in final_text.lower():
            failures.append("model report lacks 'passed'")
        if rc != 0:
            failures.append(f"opencode exit={rc}")
    finally:
        auditor.close()

    receipt = {
        "schema_version": 1,
        "validation": "ck3-real-harness-opencode-qfn",
        "created_wall": started,
        "opencode_exit": rc,
        "elapsed_seconds": round(elapsed, 2),
        "supervisor_operations": sorted(operations),
        "jobs": jobs,
        "failures": failures,
        "passed": not failures,
        "notes": ["relay keeps the API key on XTX; one client, one bounded session"],
    }
    (PROOF / "validation-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
