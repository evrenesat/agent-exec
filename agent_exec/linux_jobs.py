"""Native Linux job backend: uniquely named transient systemd units.

Implements the F1-proven recipe (see feasibility/F1/proof/f1-decision.json):

- detached launch (`systemd-run --collect`), never `--wait`/`--pipe`;
- OS-enforced execution deadline via `RuntimeMaxSec` + `TimeoutStopSec`;
- an ExecStopPost hook writing an exit record to the job directory — the only
  authoritative terminal evidence after unit collection (F1.1/F1.2);
- cleanup verified by cgroup absence/emptiness, never by process name;
- state read via `systemctl show` gated on LoadState (its defaults mimic
  success for collected units, so they must never be treated as evidence).

The backend is injectable for tests: all privileged calls go through the
module-level `_run` function, which tests may replace with a fake.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

UNIT_PREFIX = "xtx-exec"
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")
JOURNAL_ENTRY_CAP = 512  # journal entries aggregated per bounded log read


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def unit_name(job_id: str) -> str:
    return f"{UNIT_PREFIX}-{job_id}.service"


def _show(unit: str) -> dict[str, str]:
    out = _run(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                "-p", "SubState", "-p", "Result", "-p", "ExecMainCode",
                "-p", "ExecMainStatus", "-p", "ControlGroup", "-p", "InvocationID"])
    if out.returncode != 0:
        return {"LoadState": "unknown"}
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot"


@dataclass
class JobSpec:
    job_id: str
    argv: list[str]
    job_dir: Path
    deadline_seconds: int = 120
    stop_grace_seconds: int = 5
    env: dict[str, str] = field(default_factory=dict)
    purpose: str = "verify"
    operation_id: str = "unspecified"
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id or not all(c.isalnum() or c in "-_" for c in self.job_id):
            raise ValueError("job_id must be non-empty and filename-safe")
        if not self.argv:
            raise ValueError("job argv is empty")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline must be positive (no unbounded jobs)")
        if self.stop_grace_seconds <= 0:
            raise ValueError("stop grace must be positive")
        if self.cwd is not None and not self.cwd.startswith("/"):
            raise ValueError("job cwd must be an absolute path")


def exit_record_path(spec: JobSpec) -> Path:
    return spec.job_dir / "exit-record.json"


def _hook_argv(spec: JobSpec) -> str:
    record = exit_record_path(spec)
    script = (
        "import json,os,sys,time\n"
        "rec={'service_result':os.environ.get('SERVICE_RESULT'),"
        "'exit_code':os.environ.get('EXIT_CODE'),"
        "'exit_status':os.environ.get('EXIT_STATUS'),"
        "'mono':time.monotonic(),'wall':time.time()}\n"
        "p=sys.argv[1]\n"
        "tmp=p+'.partial'\n"
        "open(tmp,'w').write(json.dumps(rec))\n"
        "os.replace(tmp,p)\n")
    return "/usr/bin/python3 -c " + json.dumps(script) + " " + json.dumps(str(record))


def launch_argv(spec: JobSpec, *, hook: str | None = None) -> list[str]:
    hook = hook or _hook_argv(spec)
    argv = ["systemd-run", "--quiet", "--collect",
            "--unit=" + unit_name(spec.job_id),
            "--property=RuntimeMaxSec=" + str(spec.deadline_seconds),
            "--property=TimeoutStopSec=" + str(spec.stop_grace_seconds),
            "--property=ExecStopPost=" + hook]
    if spec.cwd:
        argv.append("--property=WorkingDirectory=" + spec.cwd)
    for key, value in sorted(spec.env.items()):
        argv.append("--setenv=" + key + "=" + value)
    return argv + spec.argv


def launch(spec: JobSpec) -> dict:
    """Start the job's transient unit; the launcher detaches immediately."""
    spec.job_dir.mkdir(parents=True, exist_ok=True)
    argv = launch_argv(spec)
    out = _run(argv, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"systemd-run failed rc={out.returncode}: "
                           f"{(out.stderr or out.stdout).strip()[:300]}")
    states = _show(unit_name(spec.job_id))
    return {
        "unit": unit_name(spec.job_id),
        "launch_wall": time.time(),
        "boot_id": boot_id(),
        "invocation_id": states.get("InvocationID") or None,
        "launch_argv": argv,
    }


def cgroup_state(unit: str) -> str:
    """verified_empty | failed | unknown for the unit's cleanup evidence."""
    cgroup = CGROUP_ROOT / unit
    try:
        if not cgroup.exists():
            return "verified_empty"
        procs = (cgroup / "cgroup.procs").read_text().split()
        return "failed" if procs else "verified_empty"
    except OSError:
        return "unknown"


def inspect(spec: JobSpec) -> dict:
    """One truthful observation. Terminal classification needs the exit record.

    - loaded+running  -> running
    - loaded+inactive -> finishing/terminal via systemctl show (real values)
    - not-found       -> terminal ONLY if the exit record exists, else unknown
    """
    unit = unit_name(spec.job_id)
    states = _show(unit)
    load = states.get("LoadState", "unknown")
    record_file = exit_record_path(spec)
    record = None
    if record_file.is_file():
        try:
            record = json.loads(record_file.read_text())
        except (OSError, json.JSONDecodeError):
            record = {"corrupt": True}
    observation: dict = {
        "unit": unit,
        "load_state": load,
        "active_state": states.get("ActiveState") or None,
        "sub_state": states.get("SubState") or None,
        "result": states.get("Result") or None,
        "exec_main_code": states.get("ExecMainCode") or None,
        "exec_main_status": states.get("ExecMainStatus") or None,
        "invocation_id": states.get("InvocationID") or None,
        "exit_record": record,
        "cgroup": cgroup_state(unit),
        "observed_wall": time.time(),
    }
    if load == "not-found":
        # The unit is definitively gone: with or without an exit record this
        # job has ended and may be finalized (missing record => inconclusive,
        # never success). Transient manager failures yield LoadState "unknown"
        # and must NOT be finalized.
        observation["ended"] = True
        if record is None or record.get("corrupt"):
            observation["phase"] = "inconclusive"
            observation["warning"] = (
                "unit collected without a usable exit record; result unknown "
                "(never success)")
        else:
            observation["phase"] = "terminal"
    elif load == "unknown":
        observation["phase"] = "unknown"
    elif states.get("ActiveState") == "active":
        observation["phase"] = "running"
    elif states.get("ActiveState") in ("activating", "reloading"):
        observation["phase"] = "starting"
    elif states.get("ActiveState") in ("deactivating",):
        observation["phase"] = "finishing"
    elif states.get("ActiveState") in ("failed", "inactive"):
        observation["phase"] = "terminal"
    else:
        observation["phase"] = "unknown"
    return observation


def classify(observation: dict, *, cancel_requested: bool = False) -> dict:
    """Map an observation to the truthful outcome contract."""
    record = observation.get("exit_record") or {}
    outcome = "inconclusive"
    exit_code = record.get("exit_code")
    exit_status = record.get("exit_status")
    service_result = record.get("service_result")
    cleanup = observation.get("cgroup", "unknown")
    if observation.get("phase") == "terminal":
        if cancel_requested:
            outcome = "cancelled"
        elif service_result == "timeout":
            outcome = "timed_out"
        elif service_result == "success" and exit_code == "exited" and exit_status == "0":
            # A pass requires BOTH valid exit evidence AND verified-empty
            # cleanup; unknown cleanup is incomplete evidence, never a pass.
            outcome = "passed" if cleanup == "verified_empty" else "inconclusive"
        elif service_result in ("exit-code", "signal", "core-dump", "watchdog",
                                "oom-kill", "resources", "protocol"):
            outcome = "failed"
        else:
            outcome = "inconclusive"
    inconclusive_reason = None
    if outcome == "inconclusive":
        inconclusive_reason = (
            observation.get("warning")
            or ("corrupt exit record" if record.get("corrupt") else None)
            or ("cleanup evidence is " + cleanup if cleanup != "verified_empty"
                and outcome == "inconclusive" else None)
            or "insufficient terminal evidence")
    return {
        "phase": observation.get("phase"),
        "outcome": outcome,
        "service_result": service_result,
        "exit_code": exit_code,
        "exit_status": exit_status,
        "cleanup": cleanup,
        "inconclusive_reason": inconclusive_reason,
    }


def stop(spec: JobSpec, *, kill: bool = False) -> dict:
    """Stop the exact owned unit; tolerate already-collected units."""
    unit = unit_name(spec.job_id)
    if kill:
        out = _run(["systemctl", "kill", "--signal=SIGKILL", "--kill-whom=all", unit])
        return {"requested": True, "signal": "SIGKILL", "rc": out.returncode}
    out = _run(["systemctl", "stop", unit])
    _run(["systemctl", "reset-failed", unit])
    return {"requested": True, "signal": "TERM", "rc": out.returncode,
            "note": "stop of a collected unit fails with rc!=0; that is not an error"}


def read_logs(spec: JobSpec, *, after_cursor: str | None = None,
              max_bytes: int = 32768) -> dict:
    """Bounded journal read with cursor replay; committed entries only."""
    unit = unit_name(spec.job_id)
    argv = ["journalctl", "-q", "-u", unit, "-o", "json", "--no-pager",
            "-n", str(JOURNAL_ENTRY_CAP)]
    if after_cursor:
        argv += ["--after-cursor", after_cursor]
    try:
        out = _run(argv, timeout=20)
    except subprocess.TimeoutExpired:
        return {"entries": [], "next_cursor": after_cursor, "bytes": 0,
                "gap": "journal read timed out"}
    if out.returncode != 0:
        return {"entries": [], "next_cursor": after_cursor, "bytes": 0,
                "gap": f"journalctl rc={out.returncode}"}
    entries = []
    used = 0
    truncated = False
    next_cursor = after_cursor
    for line in out.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cursor = entry.get("__CURSOR")
        if cursor:
            next_cursor = cursor
        size = len(line) + 1
        if used + size > max_bytes:
            truncated = True
            break
        used += size
        entries.append({"cursor": cursor, "message": entry.get("MESSAGE", ""),
                        "monotonic_ts": entry.get("__MONOTONIC_TIMESTAMP")})
    return {"entries": entries, "next_cursor": next_cursor, "bytes": used,
            "truncated": truncated, "gap": None}


def reconcile(spec: JobSpec, *, cancel_requested: bool = False) -> dict:
    """Post-restart truth: exit record if present, else explicit inconclusive."""
    observation = inspect(spec)
    verdict = classify(observation, cancel_requested=cancel_requested)
    return {"observation": observation, "verdict": verdict}
