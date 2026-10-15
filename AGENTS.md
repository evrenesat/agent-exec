# AGENTS.md — operating rules for coding agents using agent-exec

You are an agent working in a repository that has access to `agent_exec`. This file tells you how to use it correctly and what you must never do. Humans: the [README](README.md) is the short version.

## Mental model

- **systemd owns processes; the journal owns truth.** Every job is a uniquely named transient unit with an OS-enforced deadline. Python code only observes; it never supervises with timers.
- **Receipts are the only truth.** A job's outcome comes from its durable exit record plus verified-empty cgroup. Shell exit 0, a "PASS" log line, and product acceptance are different facts — never conflate them.
- **The model stays responsible.** This tooling runs checks and reports results truthfully. It never edits source, fixes assertions, weakens tests or retries silently. Do not expect it to make a failing implementation pass.

## Using the CLI

```bash
python3 -m agent_exec.cli launch --request-id <stable-id> --deadline-seconds 300 -- <argv...>
python3 -m agent_exec.cli status|wait|events|logs|cancel|reconcile <job_id>
python3 -m agent_exec.cli selftest --backend linux --output <dir>   # real-system proof
```

- Always pass a stable `--request-id`: replaying it returns the same job instead of launching a second one (this is the only dedupe that exists).
- `--deadline-seconds` is required and positive. There is no unbounded mode; if you need longer, that's a different operation profile with its own admitted deadline.
- An elapsed `wait` cancels nothing and proves nothing. If a receipt is `inconclusive`, say so and stop — do not treat it as success or retry blindly.
- Store/evidence locations default to the p100 paths; on other hosts pass `--store` and `--evidence-root` explicitly.

## Using named operations (profiles)

- Discover with `python3 -m agent_exec.profiles --check <profiles-dir>`; run with the operation id (e.g. `aflow.web.unit`) through the harness bridge or an admitted profile invocation.
- Profiles are admission-gated: `draft` profiles must not launch scored work. `fixture_verified` means three real calibration runs passed with cleanup evidence — trust that, and don't edit the command/env without re-admitting.
- Profiles are version-specific. Never copy flags between runners (Vitest 1 ≠ Vitest 4; AFlow's `--pool=forks --maxWorkers=1` is not Doublangu's invocation). If a version mismatches the profile, the profile rejects it — that is correct behavior.
- Preparation (install, codegen, `go mod download`) is a separate owned stage. Test calls never mutate dependencies. See [docs/existing-projects.md](docs/existing-projects.md).

## Harness integration (OpenCode / Pi)

- The supervisor is the only component with launch authority. Sessions poll `/events` with a cursor; they never relaunch work and never see arbitrary-argv execution.
- In headless `opencode run`/`pi --mode json`, completion delivery needs the bounded wait tool (`project_run` blocks until a terminal receipt or its own wait bound). After the final answer there is no continuation — a runner must reconcile outstanding jobs before claiming session success.
- Report receipts verbatim: outcome, exit status, test totals, cleanup. If `cleanup != verified_empty` or the report is incomplete, that is a failed check even with exit 0.

## Recovery policy

`policy.py` implements bounded recovery: one extra attempt per unchanged goal, `recovery_budget_exhausted` after a second timeout in the same lineage, wrapper changes (tail/grep/nohup) don't buy new budget, relevant source edits do. It ships in **observe** mode. Enforcement requires the labeled replay gate (`replay.py`) to show zero false hard blocks and zero invalid reuses — never flip modes without it. Unknown fingerprints are abstentions, never blocks.

## Hard prohibitions

- No `pkill -f`/process-name kills, no PID-only ownership, no unbounded logs.
- Never weaken a deadline, test, or acceptance gate to make a run pass.
- Never treat `grep`/`tail` exit codes as test results, or absent/empty reports as passes.
- Never feed hidden evaluator artifacts or prior solutions to a contestant/agent.
- Don't enable coalescing/result-reuse/loop-enforcement because it seems useful; each is a separately admitted feature with its own gate.
- Cross-session job access is rejected by design — don't work around it.

## Verification before you claim work is done

```bash
python3 -m unittest discover -s agent_exec/tests          # all green
python3 -m agent_exec.profiles --check agent_exec/profiles
node --test agent_exec/harness/tests/*.test.mjs            # if bridges touched
```

A delivery is complete only when every required check in the verification ledger has a fresh passing receipt — and even then, independent review still applies.
