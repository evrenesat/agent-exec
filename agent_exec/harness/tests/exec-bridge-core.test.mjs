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
