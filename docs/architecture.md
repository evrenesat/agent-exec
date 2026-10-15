# Architecture

## Module map

```
agent_exec/
  contracts.py      pure profile validation + command rendering (no side effects)
  profiles.py       profile loading + read-only CLI (--check / --render)
  profiles/*.json   operation definitions with admission states
  inventory.py      read-only repo/toolchain inventory
  linux_jobs.py     systemd backend: launch/inspect/stop/logs/reconcile
  store.py          SQLite journal: jobs, request-id idempotency, event cursor
  jobs.py           JobController: the session-facing lifecycle API
  cli.py            human CLI incl. real-system selftest
  reports.py        vitest-json + junit readers (incomplete ≠ pass)
  go_reports.py     go test -json stream parser
  verification.py   requirement ledger + delivery manifest
  fingerprints.py   execution key vs recovery-goal key; byte fingerprints
  policy.py         bounded recovery policy (observe/enforce; DecisionRecords)
  replay.py         offline labeled replay gate for the policy
  comparison.py     cross-arm ledger builder/checker
  admit.py          admission runner (3 calibration repeats)
  evaluation/       controlled-arm driver + independent acceptance (pattern)
  harness/          supervisor + OpenCode/Pi bridges (+ testable cores)
  tests/            150 unit tests incl. fixtures; backend fake for CI
```

## Job lifecycle

`queued → starting → running → finishing → completed`; cancellation/deadline paths pass through `cancelling`. Terminal outcomes: `passed | failed | timed_out | cancelled | blocked | inconclusive`.

One job = one uniquely named transient systemd unit, launched detached
(`systemd-run --quiet --collect`) with:

- `RuntimeMaxSec` = execution deadline, `TimeoutStopSec` = stop grace — both OS-enforced;
- an `ExecStopPost` hook writing an exit record (`EXIT_CODE`, `EXIT_STATUS`,
  `SERVICE_RESULT`) into the job dir — **the only authoritative terminal
  evidence after the unit is collected**.

Why the hook: `systemctl show` on a collected unit returns success-mimicking
defaults (measured: `Result=success`, `0:0` for a timed-out-and-collected
unit), and quiet successful units leave no journal entries. Missing record +
not-found unit ⇒ `inconclusive`, never success. `--pipe` units additionally
leave no journal trail at all — detached launches only.

Cleanup truth = the unit's cgroup is absent/empty (`/sys/fs/cgroup/system.slice/<unit>`),
checked on every terminal classification.

## Authority split

- **systemd**: process lifetime, deadlines, descendant containment (cgroup kill catches `setsid` escapes; a bwrap PID-namespace adds a second boundary).
- **SQLite journal** (`store.py`): admitted requests (unique per `(session, request_id)`), receipts, event cursor. Events: wake-capable kinds get monotonic revisions and are unique per `(job, revision)`; status observations carry NULL revision and never wake waiters.
- **Controller** (`jobs.py`): observes, classifies, absorbs terminal truth into receipts, exposes `wait` (never cancels/extends), cursor `logs`, `reconcile` after restarts.

Crash matrix (proven by fixtures): harness death leaves jobs to run to their
deadlines; controller death likewise (systemd holds limits); both gone → jobs
still stop at `RuntimeMaxSec`; reboot ⇒ new boot id, incomplete outcomes, never
inferred passes.

## Reports and verification

Report readers classify only what the format proves: missing/empty/truncated =
incomplete; zero collected tests = incomplete required check; failure evidence
preserved. The ledger (`verification.py`) binds requirements to freshest
receipts; a manifest with anything not fresh-passed is `complete: false`.
Manual acceptance is declared, never self-certified.

## Policy layer

Two identities: **execution key** (exact reusable work — variant/selectors/seed
in the key; isolation changes are different executions) and **recovery goal
key** (related attempts — wrapper/presentation changes don't change it).
Defaults: coalescing off, reuse off, enforcement observe-only. The replay gate
(12 labeled fixtures) must show zero false hard blocks / zero invalid reuses
before any enforcement flip. Unknown fingerprints ⇒ abstain.

## Harness path

Supervisor (HTTP, loopback, operations-allowlist) over the controller; plugins
poll `/events` with a cursor; the supervisor reconciles live jobs before
serving, making polling self-driving. Bounded waits absorb in one model turn —
measured: an 86-poll wait replaced what would have been model-turn polling.

## Deliberate non-features

No second process manager, no remote queue, no D-Bus (no CLI gap found), no
shell parsing/emulation for "equivalent" commands (raw shell is out of policy
scope), no auto-retry/rerun-until-green, no model-driven deadline extension.

## Provenance

Designed and proven in the `xtx` project (plan: "reliable coding-agent
execution"; checkpoints 0–7 completed 13–14 Oct 2026, incl. an OpenCode+QFN
end-to-end validation and a 4/4-accepted prospective comparison). Receipts and
admission evidence: `/var/lib/xtx-benchmarks/execution-layer-20261013/` on the
originating host.
