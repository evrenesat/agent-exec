// P0#3 regression, Pi core: identical job-attribution semantics to the
// OpenCode core. Runs the pi-local core under plain node (no pi needed).
import { test } from "node:test";
import assert from "node:assert/strict";
import { BridgeCore } from "../pi/pi-bridge-core.mjs";

const jsonResponse = (code, body) => new Response(JSON.stringify(body),
  { status: code, headers: { "Content-Type": "application/json" } });

function fakeFetch(routes) {
  let call = 0;
  return async () => {
    const payload = routes[Math.min(call, routes.length - 1)];
    call += 1;
    return jsonResponse(200, payload);
  };
}

test("pi core: waiting for B ignores A's completion; B's failure settles", async () => {
  const core = new BridgeCore({ supervisorUrl: "http://x",
    fetchImpl: fakeFetch([
      { events: [{ job_id: "A", revision: 1, status: "passed", outcome: "passed",
                   terminal: true }], cursor: 1 },
      { events: [], cursor: 1 },
      { events: [{ job_id: "B", revision: 1, status: "failed", outcome: "failed",
                   terminal: true }], cursor: 2 },
    ]),
    options: { pollIntervalMs: 5 } });
  const result = await core.pollUntilTerminal({ jobId: "B", maxWaitMs: 5000 });
  assert.equal(result.inconclusive, false);
  assert.equal(result.terminal.job_id, "B");
  assert.equal(result.terminal.outcome, "failed");
});

test("pi core: launch carries the auth token (regression: duplicate headers key)", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, headers: init.headers });
    if (url.endsWith("/jobs")) {
      return jsonResponse(200, { job_id: "jA", replayed: false, state: "running" });
    }
    return jsonResponse(200, { events: [
      { job_id: "jA", revision: 1, status: "passed", outcome: "passed",
        terminal: true }], cursor: 1 });
  };
  const core = new BridgeCore({ supervisorUrl: "http://x", token: "tok-pi",
    fetchImpl, options: { pollIntervalMs: 5 } });
  const launch = await core.launchOperation("demo.op", { requestId: "r1" });
  assert.equal(seen[0].headers["X-Agent-Exec-Token"], "tok-pi",
    "POST /jobs must carry the token (duplicate headers key regression)");
  assert.equal(seen[0].headers["Content-Type"], "application/json");
  const result = await core.pollUntilTerminal({ jobId: launch.job_id,
    maxWaitMs: 1000 });
  assert.equal(result.terminal.job_id, "jA");
});
