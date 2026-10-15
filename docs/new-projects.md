# New projects

Set up a repo so agent-exec works from day one — and so its checks run even where systemd isn't available (CI containers, macOS).

## 1. Make the checks boring and scriptable

- One canonical command per check in `package.json` / `Makefile` / `pyproject`: `test`, `lint`, `build`. No orchestration magic inside them.
- Emit machine-readable results to a path the caller chooses (`--outputFile`, `--junitxml`). Keep the exit code honest — no `|| true`, no swallowing.
- Deterministic-ish inputs: pin toolchains (`.tool-versions`, `go.mod` toolchain, lockfiles) so a profile's environment matches reality.

## 2. Author the first profile immediately

Start from `agent_exec/profiles/aflow-core-unit.json` as a minimal draft. For a fresh repo the first admission is usually trivial — the point is establishing the habit: command pinned, deadline sized, `draft` until proven.

```bash
python3 -m agent_exec.admit --profile myproj-unit --repeats 3 \
  --source-root . --output /var/lib/<evidence-root>/admission-myproj-unit
```

## 3. Design tests so jobs stay honest

- Zero tests collected must be visible (the report readers already treat it as incomplete — help them by not filtering everything out in config).
- Keep slow/integration suites as **separate operations** with their own deadlines, not flags bolted onto the unit profile.
- Avoid test-time network and dependency mutation: fetch/install belongs to the preparation stage. This keeps streams clean and receipts meaningful.

## 4. CI without systemd

The controller tests fake the backend (`agent_exec/tests/test_jobs.py::FakeBackend`) — mirror that pattern for your own CI: unit tests of parsing/policy logic need no privileges. The systemd-dependent path is exercised by `cli selftest --backend linux` on a real host (developer machine or a privileged CI runner). Keep both green; they cover different contracts (see `docs/architecture.md`).

## 5. Wire the agent bridge when a model starts working in the repo

Follow [harness-integration.md](harness-integration.md); add your operation ids to the supervisor's operations file. Convention: name operations `<project>.<component>.<operation>` (`myproj.web.unit`).
