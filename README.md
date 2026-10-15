# agent-exec

Run your projects' test/build/lint commands as **named operations with hard deadlines, leak-proof cleanup and truthful results** — from the shell or from a coding agent (OpenCode / Pi).

Why: agents (and humans) lose time to commands that hang forever, leak child processes, or report success that isn't real. agent-exec runs every command in its own systemd unit: the OS enforces the deadline and kills the whole process tree, and the result receipt tells the truth (passed / failed / timed_out / cancelled / inconclusive — never a fake pass).

```
$ python3 -m agent_exec.cli launch --request-id demo1 --deadline-seconds 60 -- npm test
$ python3 -m agent_exec.cli status job-xxxx        # -> outcome, exit status, cleanup
```

## What you get

- **Owned jobs**: every command runs in a uniquely named systemd unit — deadline, TERM→KILL escalation and cgroup cleanup are OS-enforced, even if the agent or this tool dies.
- **Truthful receipts**: exit evidence comes from a durable exit record, not from logs or exit-0 optimism. Interrupted reports are "incomplete", never "passed".
- **Named operations (profiles)**: `aflow.web.unit`, `cdx.unit`, … — versioned JSON that pins the exact command, environment, deadlines and prerequisites, so nobody re-derives shell flags per session. Admitted profiles are proven by 3 real calibration runs before use.
- **Agent bridges**: an OpenCode plugin and a Pi extension exposing `project_run` / `exec_job_wait`, so the model runs operations through the supervisor instead of raw shell. Bounded waits happen inside one model turn — no polling loops, no masked failures.
- **Verification ledger**: declares which checks a delivery owes; a manifest with missing/stale/running checks is never "complete".

## Requirements

Linux with systemd ≥ 250 (cgroup v2; developed on 255), Python 3.12+, and rights to run `systemd-run` (root works). Node 20+ only for the harness bridges. Everything is stdlib — no pip installs.

## Install

```bash
git clone git@github.com:evrenesat/agent-exec.git && cd agent-exec
python3 -m unittest discover -s agent_exec/tests        # 150 tests, ~10 s
python3 -m agent_exec.cli selftest --backend linux --output /tmp/exec-selftest
```

## Use it

**One-off commands (no setup):** the CLI wraps any argv in an owned unit — `launch`, `status`, `wait`, `logs`, `cancel`, `reconcile`. See [docs/cli-reference.md](docs/cli-reference.md).

**Your existing projects:** define a profile, calibrate it (3 runs), then use it by name. Step-by-step with real examples (AFlow web/Go): [docs/existing-projects.md](docs/existing-projects.md).

**New projects:** [docs/new-projects.md](docs/new-projects.md) — the same flow from a blank repo, plus how to keep tests runnable without systemd in CI.

**Give it to an agent:** run the supervisor, point the OpenCode/Pi bridge at it. The model gets `project_run(operation)` with truthful receipts. [docs/harness-integration.md](docs/harness-integration.md).

**How it works / boundaries:** [docs/architecture.md](docs/architecture.md). Operating rules for agents: [AGENTS.md](AGENTS.md).

## Guarantees worth knowing

- A job never runs without a positive hard deadline.
- "Success" requires the durable exit record **and** verified-empty cgroup cleanup.
- A collected-away unit with no exit record is `inconclusive` — never success.
- Cancellation stops exactly that unit; no process-name kills, ever.
- Coalescing / result-reuse / "this looks like a loop" enforcement are **off** by default; the recovery policy ships in observe-only mode with a zero-false-block test gate before any enforcement.

## Provenance

Extracted 2026-10-15 from the `xtx` project (`benchmarks/execution/`, plan: "reliable coding-agent execution"). Design receipts and admission evidence live in that project under `/var/lib/xtx-benchmarks/execution-layer-20261013/`. Private repo — not for publication.
