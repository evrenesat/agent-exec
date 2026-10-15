#!/usr/bin/env python3
"""CK7 controlled arm driver: run one contestant arm on the frozen task.

Arms: 'control' (native OpenCode, no bridge) and 'bridge' (same model/sampling
with project_run/exec_job_wait tools over the supervisor). Acceptance is
independent and runs after the session: provided spec tests, held-out edge
tests (copied in only at acceptance), the full suite, and lint — all through
the execution layer's native job backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXEC = HERE.parent  # agent_exec
EVAL = Path("/var/lib/xtx-benchmarks/execution-layer-20261013/evaluation")
TASK = EVAL / "task"
EVIDENCE = Path("/var/lib/xtx-benchmarks/execution-layer-20261013/evaluation")
OWNED_COPY = Path("/root/code/worktrees/execution-aflow-owned-20261014")
NODE_BIN = "/opt/node-v24.20.0-linux-x64/bin"
OPENCODE = "/usr/local/bin/opencode"
RUNTIME = Path("/tmp/xtx-opencode-plugin-runtime-20261013")
MODEL = "qfn/qwen3.8-flash-next-iq3_s"
ARM_DEADLINE_SECONDS = 3600


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preflight() -> None:
    for required in (EXEC / "harness/opencode/exec_bridge.mjs",
                     EXEC / "harness/opencode/exec-bridge-core.mjs",
                     EXEC / "harness/opencode/supervisor.py"):
        if not required.is_file():
            raise FileNotFoundError(f"harness file missing: {required}")


def render_prompt(arm: str) -> str:
    template = (TASK / "prompt-template.txt").read_text()
    test_file = (TASK / "runRetryHint.test.ts").read_text()
    prompt = template.replace("{TEST_FILE}", test_file)
    if arm == "bridge":
        prompt += ("\n\nYou have a project_run tool: prefer it for running the "
                   "unit suite and lint (it enforces a deadline and returns a "
                   "truthful receipt). Use operation id 'aflow.web.unit' for "
                   "tests and 'aflow.web.lint' for lint, with a fresh "
                   "request_id per invocation.\n")
    else:
        prompt += ("\n\nRun tests with the shell from the repository root using "
                   "the exact isolated one-worker command above. Never use "
                   "--singleFork or a singleThread/singleFork configuration.\n")
    return prompt


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


def prepare_candidate(arm_dir: Path) -> Path:
    candidate = arm_dir / "candidate"
    if candidate.exists():
        raise RuntimeError(f"refusing to overwrite {candidate}")
    shutil.copytree(OWNED_COPY, candidate, symlinks=True)
    return candidate


def stage_session(arm_dir: Path, candidate: Path, arm: str) -> tuple[Path, dict]:
    staging: dict = {"supervisor_process": None, "supervisor_url": None}
    session = arm_dir / "session"
    config_dir = session / "config"
    work = arm_dir / "work"
    for path in (config_dir, work, session / "home", session / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    # The model works on the candidate directly via --dir; 'work' only anchors cwd.
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "share": "disabled",
        "autoupdate": False,
        "plugin": [],
        "provider": {"qfn": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "QFN via bounded relay",
            "options": {"baseURL": "http://127.0.0.1:18489/v1",
                        "apiKey": "relay-injected"},
            "models": {"qwen3.8-flash-next-iq3_s": {
                "name": "QFN IQ3_S", "tool_call": True, "temperature": True,
                "limit": {"context": 131072, "output": 16384}},
            },
        }},
        "agent": {"build": {"temperature": 0.6, "top_p": 0.95}},
    }
    supervisor_url = None
    if arm == "bridge":
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(RUNTIME / name, config_dir / name)
        link = config_dir / "node_modules"
        if not link.exists():
            link.symlink_to(RUNTIME / "node_modules")
        shutil.copyfile(EXEC / "harness/opencode/exec_bridge.mjs",
                        config_dir / "exec_bridge.mjs")
        shutil.copyfile(EXEC / "harness/opencode/exec-bridge-core.mjs",
                        config_dir / "exec-bridge-core.mjs")
        operations = {
            "aflow.web.unit": {
                "argv": [f"{NODE_BIN}/npm", "--prefix", "apps/aflow_app/web",
                         "test", "--", "--run", "--maxWorkers=1", "--minWorkers=1",
                         "--pool=forks", "--reporter=json",
                         "--outputFile={report_dir}/vitest-json.json"],
                "cwd": str(candidate), "deadline_seconds": 180,
                "profile_id": "aflow-web-p100",
                "env": {"PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
                        "npm_config_audit": "false", "npm_config_fund": "false",
                        "NO_COLOR": "1", "HOME": str(session / "home")},
            },
            "aflow.web.lint": {
                "argv": [f"{NODE_BIN}/npm", "--prefix", "apps/aflow_app/web",
                         "run", "lint"],
                "cwd": str(candidate), "deadline_seconds": 120,
                "profile_id": "aflow-web-p100",
                "env": {"PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin",
                        "NO_COLOR": "1", "HOME": str(session / "home")},
            },
        }
        (arm_dir / "operations.json").write_text(json.dumps(operations, indent=2))
        supervisor = subprocess.Popen(
            [sys.executable, str(EXEC / "harness/opencode/supervisor.py"),
             "--store", str(arm_dir / "supervisor-store.sqlite"),
             "--evidence-root", str(arm_dir / "evidence"),
             "--session-label", f"ck7-{arm_dir.name}",
             "--operations", str(arm_dir / "operations.json")],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        info = json.loads(supervisor.stdout.readline())
        supervisor_url = info["listening"]
        wait_health(supervisor_url + "/health")
        staging["supervisor_process"] = supervisor
        config["plugin"] = [(config_dir / "exec_bridge.mjs").as_uri()]
        (config_dir / "xtx-exec-bridge.json").write_text(json.dumps({
            "supervisorName": "execution-layer", "supervisorUrl": supervisor_url,
            "pollIntervalMs": 250, "pollTimeoutMs": 3000,
            "logPath": str(session / "bridge.jsonl")}))
    config_path = config_dir / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": str(session / "tmp"),
           "XDG_CONFIG_HOME": str(session / "XDG_CONFIG_HOME"),
           "XDG_DATA_HOME": str(session / "XDG_DATA_HOME"),
           "XDG_CACHE_HOME": str(session / "XDG_CACHE_HOME"),
           "XDG_STATE_HOME": str(session / "XDG_STATE_HOME"),
           "OPENCODE_CONFIG": str(config_path),
           "OPENCODE_CONFIG_DIR": str(config_dir),
           "OPENCODE_TEST_HOME": str(session / "home"),
           "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
           "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1",
           "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
           "OPENCODE_DISABLE_CLAUDE_CODE": "1",
           "HOME": str(session / "home")}
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        Path(env[key]).mkdir(exist_ok=True)
    staging["supervisor_url"] = supervisor_url
    return env, staging


def run_session(arm_dir: Path, candidate: Path, env: dict) -> dict:
    prompt = render_prompt("bridge" if (arm_dir / "session/config/exec_bridge.mjs").exists()
                           else "control")
    (arm_dir / "prompt.rendered.txt").write_text(prompt)
    started = time.time()
    with (arm_dir / "opencode.log").open("w") as log:
        process = subprocess.Popen(
            [OPENCODE, "run", "--format", "json",
             "--title", f"CK7 arm {arm_dir.name}", "--model", MODEL,
             "--dir", str(candidate), "--", prompt],
            cwd=arm_dir / "work", env=env, stdout=log, stderr=log,
            start_new_session=True)
        try:
            rc = process.wait(timeout=ARM_DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            rc = "timeout"
    return {"exit": rc, "wall_seconds": round(time.time() - started, 2)}


def session_metrics(arm_dir: Path) -> dict:
    requests = tool_by_type = 0
    tools: dict[str, int] = {}
    tokens_in = tokens_out = 0
    first_edit_wall = None
    start_wall = None
    for line in (arm_dir / "opencode.log").read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "step_start":
            requests += 1
            start_wall = start_wall or event.get("timestamp")
        elif event.get("type") == "step_finish":
            usage = (event.get("part") or {}).get("tokens") or {}
            tokens_in += usage.get("input", 0) or 0
            tokens_out += usage.get("output", 0) or 0
        elif event.get("type") == "tool_use":
            part = event.get("part") or {}
            name = part.get("tool", "unknown")
            tools[name] = tools.get(name, 0) + 1
            tool_by_type += 1
            if name in ("edit", "write", "apply_patch", "multiedit") and first_edit_wall is None:
                first_edit_wall = event.get("timestamp")
    return {"model_requests": requests, "tool_attempts": tool_by_type,
            "tools_by_type": tools, "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "first_edit_offset_seconds": (first_edit_wall - start_wall)
            if first_edit_wall and start_wall else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["control", "bridge"], required=True)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    _preflight()
    arm_dir = EVIDENCE / "arms" / f"{args.arm}-r{args.repeat}"
    if arm_dir.exists():
        print(f"arm dir exists, refusing: {arm_dir}", file=sys.stderr)
        return 2
    for required in (TASK / "prompt-template.txt", TASK / "runRetryHint.test.ts",
                     TASK / "held-out/runRetryHint.heldout.test.ts"):
        if not required.is_file():
            print(f"frozen task file missing: {required}", file=sys.stderr)
            return 2
    arm_dir.mkdir(parents=True)
    candidate = prepare_candidate(arm_dir)
    env, staging = stage_session(arm_dir, candidate, args.arm)
    try:
        session = run_session(arm_dir, candidate, env)
    finally:
        if staging.get("supervisor_process") is not None:
            staging["supervisor_process"].terminate()
            staging["supervisor_process"].wait(timeout=10)
    metrics = session_metrics(arm_dir)
    files = {
        "prompt_template_sha256": sha256(TASK / "prompt-template.txt"),
        "public_spec_sha256": sha256(TASK / "runRetryHint.test.ts"),
        "heldout_sha256": sha256(TASK / "held-out/runRetryHint.heldout.test.ts"),
        "candidate_seed_head": subprocess.run(
            ["git", "-c", f"safe.directory={candidate}", "-C", str(candidate),
             "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
    }
    staging_public = {k: v for k, v in staging.items()
                      if k not in ("supervisor_process", "supervisor_url")}
    receipt = {"schema_version": 1, "arm": args.arm, "repeat": args.repeat,
               "session": session, "metrics": metrics, "frozen_inputs": files,
               "staging": staging_public}
    (arm_dir / "arm-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
