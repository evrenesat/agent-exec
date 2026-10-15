# CLI reference

All commands run from the repo root (`python3 -m agent_exec.…`). Global flags: `--store <path>` (journal sqlite; default `/var/lib/xtx-benchmarks/execution-layer-20261013/controller.sqlite`) and `--evidence-root <dir>` (job dirs/reports). On non-origin hosts, always pass both.

## launch

```bash
python3 -m agent_exec.cli launch --request-id <stable-id> \
  [--operation-id <label>] [--deadline-seconds 120] [--stop-grace-seconds 5] -- <argv...>
```

Starts one owned unit. `--request-id` is required and idempotent: replaying it returns the original job (`"replayed": true`) and launches nothing. Deadline must be positive. Output: `{job_id, replayed, state}`.

## status `<job_id>`

One truthful observation; terminal classification absorbed from the exit record. Receipt fields: `outcome`, `service_result`, `exit_code`, `exit_status`, `cleanup` (`verified_empty|failed|unknown`), `inconclusive_reason`.

## wait `<job_id>` `[--max-wait-seconds 30]`

Blocks until a wake-capable event, terminal receipt, or elapsed. Elapsed ⇒ `{elapsed: true}` and **cancels nothing**.

## events `<job_id>` / logs `<job_id>`

Event cursor replay (`seq`, `revision`, `kind` — NULL-revision rows are status noise and never satisfy a revision wait). `logs [--after-cursor <c>] [--max-bytes 32768]` reads committed journal entries only, with cursor + truncation flags.

## cancel `<job_id>`

Marks cancel-requested and stops exactly that unit (TERM, then reset-failed). Distinguishes request from verified termination — check `status` afterwards for the receipt.

## reconcile `<job_id>` / reconcile-all

Post-restart truth: exit record if present, else explicit `inconclusive`. Run before claiming any session's work is done.

## selftest

```bash
python3 -m agent_exec.cli selftest --backend linux --output <empty-dir>
```

Real-system proof (~30 s): passed/failed/timed_out/cancelled/blocked cases + bounded log read; asserts `verified_empty` cleanup; writes `selftest-receipt.json`. Exit 0 = pass.

## profiles

```bash
python3 -m agent_exec.profiles --check agent_exec/profiles
python3 -m agent_exec.profiles --render <profile-id> agent_exec/profiles \
  --source-root /path --report-dir /tmp/x [--selectors apps/.../src/foo.test.ts]
```

Read-only validation / argv preview (never executes). `--check` also fails if a `fixture_verified` profile's bound admission receipt file is missing.

## admit

```bash
python3 -m agent_exec.admit --profile <id> --repeats 3 \
  --source-root <owned-copy> --output <evidence-dir>
```

Runs the rendered command N times through the job layer, reads reports, verifies generated outputs + cleanup, writes `admission-receipt.json`. (Vitest-json based; for Go/other stacks see the cdx pattern in `docs/existing-projects.md`.)

## inventory

`python3 agent_exec/inventory.py [--output file.json]` — read-only manifests/CI/version/git-state snapshot. Refuses to overwrite.

## replay (policy gate)

```bash
python3 -m agent_exec.replay --fixtures agent_exec/tests/fixtures/recovery --mode observe
```

Zero false hard blocks + zero invalid reuses required (exit code reflects it) before enforcement is ever considered.

## comparison

```bash
python3 -m agent_exec.comparison --build <arms-root> --output ledger.json
python3 -m agent_exec.comparison --check-ledger ledger.json
```

Builds/validates a cross-arm ledger: consistent frozen inputs, acceptance bound per arm, no duplicate (arm, repeat).

## supervisor (harness)

`python3 -m agent_exec.harness.opencode.supervisor --store … --evidence-root … --session-label … --operations operations.json` — see [harness-integration.md](harness-integration.md).
