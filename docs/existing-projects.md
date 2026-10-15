# Adding an existing project

Turn a repository you already have into named operations with proven commands. Total effort: ~30–60 min per project, most of it reading how the project already runs its checks.

Flow: **owned copy → inventory → profile → admission → use**.

## 0. Prerequisites

- The project's real toolchain installed and pinned (exact node/python/go versions — the profile records them).
- An **owned copy** of the source if the repo is production/shared: `cp -a /path/proj /root/code/worktrees/<name>-owned-<date>`. Never point scored runs at a checkout others depend on.

## 1. Inventory (read-only)

```bash
python3 -m agent_exec.inventory --output inventory.json
```

Records manifests, CI files, tool versions, git HEAD and dirty state. Use it to fill the profile honestly.

## 2. Write the profile

Copy `agent_exec/profiles/aflow-web-p100.json` (JS/npm) or `cdx-unit.json` (Go) as a template into `agent_exec/profiles/<project>-<component>.json`. The fields that matter:

| Field | What to pin |
| --- | --- |
| `command.argv_template` | The **exact** invocation the project already uses. Take it from CI/Makefile, not memory. `--reporter=json --outputFile={report_dir}/...` captures a machine-readable report outside the source tree. |
| `command.cwd` | `{source_root}` (the owned copy). |
| `selectors` | Allowed kinds + base dir; empty selection = full suite. |
| `preparation` | Separate owned stages the runner performs **before** tests (`npm` lifecycle via pretest, `go mod download`, codegen). Test calls never mutate dependencies. |
| `deadlines` | `diagnose_seconds` ≤ `hard_seconds`, both positive; sized to the operation (23 s suite → 180 s hard; a full browser suite legitimately needs tens of minutes). |
| `environment` | Exact interpreter/runner versions; `forbidden_flags` for traps you've hit (e.g. Vitest 1 `--singleFork`). |
| `inputs.fingerprints` / `generated` | What counts as an input; which outputs are generated (and by which stage). |
| `capabilities` | All default to off. Turn nothing on at admission time. |
| `status` | `draft` — the admission run flips it, not you. |

Version traps seen in the wild: Vitest 1 vs 4 flags; npm `pretest` generation that bare `npx vitest` skips; Go needing `GOTOOLCHAIN=local`, a pinned toolchain, `-buildvcs=false` (or a git `safe.directory`) on copies, and a pre-fetched module cache so test streams stay clean.

## 3. Admit (3 real runs)

```bash
python3 -m agent_exec.admit --profile <id> --repeats 3 \
  --source-root /root/code/worktrees/<owned-copy> \
  --output /var/lib/<evidence-root>/admission-<id>
```

(For Go or non-vitest stacks, model your runner on the loop in the cdx admission — see `agent_exec/go_reports.py` and the provenance receipts.)

Pass = 3× `passed` + `verified_empty` cleanup + valid report + generated outputs present. Then set `"status": "fixture_verified"` and add `"admission_receipt": "<path>"` in the profile — `--check` enforces that the file exists.

## 4. Use it

```bash
# by hand, through the job layer:
python3 -m agent_exec.cli launch --request-id run-1 -- <rendered argv...>

# from an agent: add the operation to the supervisor and expose project_run
# (docs/harness-integration.md)
```

## Maintenance

- Source/toolchain/lockfile drift doesn't auto-invalidate a profile — re-run admission when the project's toolchain moves (new node major, vitest major, go bump).
- If admission fails, fix the profile or environment; never widen the deadline until it passes. A failing admission is the system working.
- Keep one profile per project+component+environment; don't generalize flags across them.
