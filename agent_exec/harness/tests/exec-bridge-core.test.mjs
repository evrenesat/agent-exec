// Unit tests for the execution-bridge core (no OpenCode, no network).
// Run: node --test agent_exec/harness/tests/

import { test } from "node:test";
import assert from "node:assert/strict";
import { BridgeCore } from "../opencode/exec-bridge-core.mjs";

function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const handler = routes.length > 1 ? routes.shift() : routes[0];
    return handler(url, init);
  };
  impl.calls = calls;
  return impl;
}

const jsonResponse = (code, body) => new Response(JSON.stringify(body),
  { status: code, headers: { "Content-Type": "application/json" } });

test("dedupes repeated events by job and revision", async () => {
  const fetchImpl = fakeFetch([
    () => jsonResponse(200, { events: [
      { job_id: "j1", revision: 1, status: "completed", terminal: true }],
      cursor: 1 }),
    () => jsonResponse(200, { events: [
      { job_id: "j1", revision: 1, status: "completed", terminal: true }],
      cursor: 1 }),
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  const first = await core.pollUntilTerminal({ maxWaitMs: 500 });
  assert.equal(first.events.length, 1);
  const second = await core.pollUntilTerminal({ maxWaitMs: 300 });
  assert.equal(second.events.length, 0, "redelivered revision must dedupe");
  assert.equal(core.stats().distinct_events, 1);
});

test("retries a transient 503 and still delivers exactly once", async () => {
  const fetchImpl = fakeFetch([
    () => jsonResponse(200, { events: [], cursor: 0 }),
    () => jsonResponse(503, { error: "down" }),
    () => jsonResponse(200, { events: [
      { job_id: "j2", revision: 1, status: "passed", outcome: "passed",
        terminal: true }],
      cursor: 2 }),
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl,
    options: { pollIntervalMs: 10 } });
  const result = await core.pollUntilTerminal({ maxWaitMs: 3000 });
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].outcome, "passed");
  assert.equal(result.poll_errors, 1);
});

test("timeout leaves remaining_ms and does not fabricate events", async () => {
  const fetchImpl = fakeFetch([
    () => jsonResponse(200, { events: [], cursor: 0 }),
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl,
    options: { pollIntervalMs: 20, maxWaitMs: 150 } });
  const result = await core.pollUntilTerminal({});
  assert.equal(result.events.length, 0);
  assert.ok(result.remaining_ms <= 1,
    "an expired wait reports no remaining budget");
});

test("cursor advances so supervisor is not re-queried from zero", async () => {
  const fetchImpl = fakeFetch([
    () => jsonResponse(200, { events: [
      { job_id: "j3", revision: 1, status: "failed", terminal: true }],
      cursor: 7 }),
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  await core.pollOnce();
  assert.equal(core.cursor, 7);
  assert.ok(fetchImpl.calls[0].url.endsWith("/events?cursor=0"));
  const second = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  second.cursor = 7;
  await second.pollOnce();
  assert.ok(fetchImpl.calls.at(-1).url.endsWith("/events?cursor=7"),
    "reconnect resumes from the persisted cursor");
});

test("launchOperation posts the operation contract", async () => {
  const fetchImpl = fakeFetch([
    (url, init) => {
      assert.equal(init.method, "POST");
      const body = JSON.parse(init.body);
      assert.equal(body.operation, "aflow.web.unit");
      assert.ok(body.request_id);
      return jsonResponse(200, { job_id: "job-9", replayed: false, state: "running" });
    },
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  const result = await core.launchOperation("aflow.web.unit",
    { requestId: "req-1" });
  assert.equal(result.job_id, "job-9");
});

test("launch failure surfaces the supervisor error", async () => {
  const fetchImpl = fakeFetch([
    () => jsonResponse(400, { error: "unknown operation: 'nope'" }),
  ]);
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  await assert.rejects(() => core.launchOperation("nope", { requestId: "r" }),
    /unknown operation/);
});

// P0#3 regression: an unfiltered wait must never settle on another job's
// terminal event. Job A completes while we wait for Job B; B must keep
// waiting, then fail, and the result must identify B.
test("concurrent jobs: waiting for B ignores A's completion and returns B's failure", async () => {
  const stream = [
    { events: [{ job_id: "A", revision: 1, status: "passed", outcome: "passed",
                 terminal: true }], cursor: 1 },
    { events: [], cursor: 1 },
    { events: [], cursor: 1 },
    { events: [{ job_id: "B", revision: 1, status: "failed", outcome: "failed",
                 terminal: true }], cursor: 2 },
  ];
  let call = 0;
  const fetchImpl = async () => {
    const payload = stream[Math.min(call, stream.length - 1)];
    call += 1;
    return jsonResponse(200, payload);
  };
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl,
    options: { pollIntervalMs: 5 } });
  const result = await core.pollUntilTerminal({ jobId: "B", maxWaitMs: 5000 });
  assert.equal(result.inconclusive, false);
  assert.equal(result.settled_event.job_id, "B");
  assert.equal(result.settled_event.outcome, "failed");
  assert.ok(result.events.some((e) => e.job_id === "A"),
            "A's event stays available in the collected stream");
  assert.equal(result.settled_event.job_id !== "A", true);
});

test("expiry on job-specific wait is explicitly inconclusive", async () => {
  const fetchImpl = async () => jsonResponse(200, { events: [
    { job_id: "A", revision: 1, status: "passed", outcome: "passed",
      terminal: true }], cursor: 1 });
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl,
    options: { pollIntervalMs: 5 } });
  const result = await core.pollUntilTerminal({ jobId: "B", maxWaitMs: 120 });
  assert.equal(result.inconclusive, true);
  assert.equal(result.settled_event, null);
  assert.equal(result.job_id, "B");
});

test("terminal event present before waiting settles immediately for that job", async () => {
  const fetchImpl = async () => jsonResponse(200, { events: [
    { job_id: "B", revision: 1, status: "completed", outcome: "passed",
      terminal: true }], cursor: 3 });
  const core = new BridgeCore({ supervisorUrl: "http://x", fetchImpl });
  const result = await core.pollUntilTerminal({ jobId: "B", maxWaitMs: 500 });
  assert.equal(result.inconclusive, false);
  assert.equal(result.settled_event.job_id, "B");
});

// Review round 2: the Pi bridge originally dropped the auth token on POST
// (duplicate headers key). Lock the launch-auth contract in for both cores.
test("launch sends the auth token header (regression: duplicate headers key)", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push(init.headers);
    return jsonResponse(200, { job_id: "j9", replayed: false, state: "running" });
  };
  const core = new BridgeCore({ supervisorUrl: "http://x", token: "tok-abc",
    fetchImpl });
  await core.launchOperation("demo.op", { requestId: "r1" });
  assert.equal(seen[0]["X-Agent-Exec-Token"], "tok-abc",
    "POST /jobs must carry the token");
  assert.equal(seen[0]["Content-Type"], "application/json");
  // Poll path too.
  await core.pollOnce();
  assert.equal(seen[1]["X-Agent-Exec-Token"], "tok-abc");
});
