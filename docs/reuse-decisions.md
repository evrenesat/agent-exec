# Reuse decisions (checkpoint-0 provenance)

> Extracted from the originating `xtx` project; `/var/lib/...` and `plans/...` links resolve there, not in this repo.

# Execution layer: reuse decisions (checkpoint 0)

Status: decisions bound 13 October 2026 on p100 (`codex`). Each decision has a
machine-readable receipt under
`/var/lib/xtx-benchmarks/execution-layer-20261013/feasibility/<F#>/proof/`
with exact commands, raw exits and limits. Validate with:

```text
python3 agent_exec/feasibility/check_receipts.py \
  --root /var/lib/xtx-benchmarks/execution-layer-20261013/feasibility \
  --require F0 F1 F2 F3 F4
```

## Decisions

| ID | Capability | Decision | One-line reason |
| --- | --- | --- | --- |
| F0 | Conventional commands | **adapt** existing canonical command | Exact npm pretest + isolated one-worker Vitest already admitted at the frozen source/dependency state; leak-shape reproduction shows the F1 recipe contains the descendant defect class. Live singleFork/canonical re-runs deferred until the scored s1 arm releases p100 (marker `ongoing/benchmark_2`). |
| F1 | Process ownership | **adapt** systemd transient units (CLI) | Proven lifecycle: detached launch, OS RuntimeMaxSec deadline, cgroup containment of setsid children (bare + bwrap), ExecStopPost durable exit record for every exit class; `systemctl show` on collected units mimics success, so the record file is the only authoritative post-GC evidence. No D-Bus gap. |
| F2 | Pi delivery | **build thin bridge** | pi-background-tasks (pinned `89997e2f`, MIT) cannot consume externally owned jobs (in-process bus, self-spawning `start()`); reuse its interface ideas (bounded untrusted receipts, byte cursors, wake batching) in a minimal Pi 0.99.2 extension. |
| F3 | OpenCode delivery | **adapt** OpenCode 1.18.25 + private bridge | Fixture-proven 4/4 (completion-before-watch, delayed, duplicate-redelivery dedupe, disconnect-retry) with a scripted provider; headless continuation requires the wait-tool rendezvous; plugin files must live in the session config dir for package resolution. |
| F4 | Evaluation | **retain XTX evaluator** | Harbor 0.24.0 (pinned `4778ceb`, Apache-2.0) passed a fake-agent trial and separates verifiers, but mandates a container runtime — it cannot preserve the bwrap/systemd boundary; keep as optional import wrapper only. |

## Inventory (decision-level; re-verified per profile at checkpoint 1 admission)

- **AFlow web (first operation)**: npm lifecycle `pretest` → `npm run changelog`
  (generated prerequisite), `test` → vitest; vitest ^1.0.0 (1.6.1 lock); node
  v24.20.0 (`/opt/node-v24.20.0-linux-x64`); canonical command frozen in
  `benchmarks/opencode/protocol.json`; source commit `9d89c98…` for tasks,
  reference HEAD `ebc64fb…`.
- **Host**: systemd 255 (unified cgroup v2), bubblewrap 0.9.0, Python 3.12.3
  (stdlib-only policy core), OpenCode 1.18.25 (`d91e0d33…`), Pi 0.99.2,
  Docker 29.1.3 (F4 trial only).
- **Second-language drafts (checkpoint 6)**: cdx Go 1.24+, Doublangu Go 1.26.5 +
  Vitest 4 (installed system Go is 1.22.2 — toolchain resolution is an admission
  prerequisite); Asky/Audioventura inventory-only; Rust/C-C++ documented
  contracts only.

## Deferred live validations (blocked by the running scored s1 arm)

- F0: fresh owned-copy canonical run, singleFork-vs-one-worker comparison,
  reporter admission — all deferred because live AFlow web tests would contend
  with the frozen arm's timing measurements (`ongoing/benchmark_2` present).
- F2: live pi 0.99.2 headless fixture (needs a model session).
- F3: QFN-specific tool-call parsing through the bridge (needs inference).

The deferred items do not unbind the decisions above: each names the exact
command to run when the marker disappears.

## Alignment with the 14 October QFN harness-improvement handoff

The owner-flagged handoff
([qfn-harness-improvement-handoff-20261014.md](../plans/research/qfn-harness-improvement-handoff-20261014.md))
does not change these decisions; it sharpens them:

- Its prioritized experiment 1 — bound foreground commands, descendant/process-group
  cleanup and unmasked pipeline exit status — is exactly the capability this layer's
  checkpoints 2–3 build (per-job systemd deadline + cgroup containment + ExecStopPost
  truthful exit). The OpenCode bridge gains a deadline-bounded run/wait shape serving
  that experiment; the hanging-fixture success criterion (ends within bound, no
  descendants, failures stay failures) maps onto the F1 fixtures already proven.
- F2 lineage correction: the campaign's `pi-background-tasks@2.6.10` is the
  ismailsaleekh package, not the Jawfish repo reviewed at checkpoint 0. The thin-bridge
  recommendation is unchanged and strengthened (no cgroup ownership, peers exclude Pi
  1.2.0, hardcoded spool); see the corrected `f2-decision.json`.
- Checkpoint 7 prospective evaluation follows the handoff/owner-dated protocol, not
  this plan's older three-repeat text: n=1 first, a conditional verification second,
  maximum 2 per configuration; if reusing its task, retain the material focus/preview
  regression check, the no-image wire enforcement, advertised vision and thermal
  guards. Avoid the named dead ends (native batch, published loop artifact, ToolSearch).

