# Harness integration: giving an agent the tools

The supervisor is a small HTTP service wrapping the job controller. Harness plugins (OpenCode 1.x, Pi) talk to it; the model never gets arbitrary-argv execution.

## Components

```
agent_exec/harness/opencode/supervisor.py     # the service (Python)
agent_exec/harness/opencode/exec_bridge.mjs   # OpenCode 1.x plugin
agent_exec/harness/pi/pi_bridge.js            # Pi extension
agent_exec/harness/opencode/exec-bridge-core.mjs / pi-bridge-core.mjs  # testable cores
```

## Run the supervisor

```bash
python3 -m agent_exec.harness.opencode.supervisor \
  --store <dir>/supervisor.sqlite \
  --evidence-root <dir>/evidence \
  --session-label my-session \
  --operations operations.json
# prints {"listening": "http://127.0.0.1:<port>", "operations": [...]}
```

`operations.json` maps operation ids to launch specs (argv template with `{report_dir}` per-job substitution, cwd, deadline, env). Bind it to 127.0.0.1; one supervisor per session namespace.

HTTP contract:

| Route | Meaning |
| --- | --- |
| `GET /health` | liveness |
| `POST /jobs` `{operation, request_id, deadline_seconds?}` | idempotent launch (same request_id → same job) |
| `GET /jobs/<id>` | current receipt (+ parsed vitest report when present) |
| `GET /events?cursor=<seq>` | wake-capable events after cursor; **reconciles live jobs first**, so polling is self-driving |

Events are deduplicated by `(job_id, revision)` on both sides; unchanged status never wakes a waiter; a transient 503 is retried; an elapsed wait is reported as such and cancels nothing.

## OpenCode 1.x

Plugin files must live **inside the session config dir** (they import `@opencode-ai/plugin` via its `node_modules` symlink):

```bash
cp agent_exec/harness/opencode/exec_bridge*.mjs <config-dir>/
ln -s <pinned-runtime>/node_modules <config-dir>/node_modules
```

Config (`opencode.json`) essentials:

```json
{ "plugin": ["file://<config-dir>/exec_bridge.mjs"],
  "provider": { "<id>": { "npm": "@ai-sdk/openai-compatible",
    "options": { "baseURL": "<your-local-endpoint>/v1", "apiKey": "<placeholder>" } } } }
```

`<config-dir>/xtx-exec-bridge.json`:

```json
{ "supervisorUrl": "http://127.0.0.1:<port>",
  "pollIntervalMs": 250, "pollTimeoutMs": 3000,
  "logPath": "<session>/bridge.jsonl" }
```

Tools the model sees: **`project_run`** (launch + bounded wait for the terminal receipt, includes test totals) and **`exec_job_wait`** (wait only).

Headless note: `opencode run` exits after the final turn — outstanding jobs are resolved only through the in-turn wait. A driver must reconcile leftover jobs before declaring session success.

## Pi

Load the extension explicitly against a pinned runtime:

```bash
<pi-cli> --offline --mode json --no-extensions \
  -e <path>/pi_bridge.js -- <prompt>
```

Same wire contract and tool names as the OpenCode bridge. Characterization (measured on Pi 1.2.0): pi stays alive during the bounded tool wait; after the final answer the process exits and a later completion reaches only the supervisor journal — no post-answer continuation exists. Treat the supervisor store as authoritative and reconcile.

## Model-facing prompt convention (what worked)

> "Use project_run for tests/lint (operation ids …, fresh request_id per call). Report the receipt verbatim: outcome, exit status, totals, cleanup. If inconclusive, say so; don't retry blindly."

Observed in prospective trials: models adopt the tool unprompted when told it enforces deadlines, and report inconclusive receipts honestly instead of fabricating success (see provenance receipts in the originating project).

## Boundaries

- The supervisor accepts **only** declared operations — no caller-supplied argv/cwd/hosts.
- One session label per session; the store rejects cross-session job access.
- Keep the endpoint loopback; put TLS/auth in front only if you must expose it further (not covered here).
