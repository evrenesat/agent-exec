// Execution-bridge core: cursor polling, dedupe, retry, bounded receipts.
// Pure logic with an injectable fetch — unit-testable via `node --test`
// without OpenCode. The plugin wrapper (exec_bridge.mjs) binds this to the
// 1.18.25 plugin API.

export const DEFAULTS = {
  pollIntervalMs: 250,
  pollTimeoutMs: 3000,
  maxWaitMs: 60000,
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class BridgeCore {
  constructor({ supervisorUrl, token = null, log = () => {}, options = {},
                fetchImpl = fetch }) {
    this.supervisorUrl = supervisorUrl.replace(/\/$/, "");
    this.token = token;
    this.log = log;
    this.options = { ...DEFAULTS, ...options };
    this.fetchImpl = fetchImpl.bind(globalThis);
    this.seen = new Set(); // job_id:revision dedupe
    this.cursor = 0;
    this.polls = 0;
    this.pollErrors = 0;
  }

  headers(extra = {}) {
    return this.token
      ? { "X-Agent-Exec-Token": this.token, ...extra } : extra;
  }

  async pollOnce() {
    this.polls += 1;
    const res = await this.fetchImpl(
      `${this.supervisorUrl}/events?cursor=${this.cursor}`,
      { headers: this.headers(),
        signal: AbortSignal.timeout(this.options.pollTimeoutMs) });
    if (!res.ok) throw new Error(`supervisor_http_${res.status}`);
    const data = await res.json();
    this.cursor = data.cursor ?? this.cursor;
    const fresh = [];
    for (const ev of data.events ?? []) {
      const key = `${ev.job_id}:${ev.revision}`;
      if (this.seen.has(key)) continue;
      this.seen.add(key);
      fresh.push(ev);
    }
    this.log({ phase: "poll", polls: this.polls, cursor: this.cursor,
      received: (data.events ?? []).length, fresh: fresh.length });
    return fresh;
  }

  // Wait for THIS job's terminal event only. The supervisor stream carries
  // every job's events, so an unfiltered wait can attribute another job's
  // completion to ours. With jobId, only that job's terminal event settles
  // the wait; expiry yields an explicit inconclusive, never another job's
  // receipt. Without jobId (generic event drain) no terminal matching occurs.
  async pollUntilTerminal({ jobId = null, maxWaitMs, pollIntervalMs } = {}) {
    const deadline = Date.now() + (maxWaitMs ?? this.options.maxWaitMs);
    const collected = [];
    let settled = null;
    while (Date.now() < deadline) {
      let fresh = [];
      try {
        fresh = await this.pollOnce();
      } catch (error) {
        this.pollErrors += 1;
        this.log({ phase: "poll_error", error: String(error).slice(0, 120) });
      }
      for (const ev of fresh) {
        collected.push(ev);
        if (ev.terminal && (jobId === null || ev.job_id === jobId) && !settled) {
          settled = ev;
        }
      }
      if (settled) break;
      await sleep(pollIntervalMs ?? this.options.pollIntervalMs);
    }
    return {
      job_id: jobId,
      events: collected.map((e) => ({ job_id: e.job_id, revision: e.revision,
        status: e.status, terminal: e.terminal === true, outcome: e.outcome,
        cleanup: e.cleanup })),
      settled_event: settled ? { job_id: settled.job_id, revision: settled.revision,
        status: settled.status, outcome: settled.outcome, cleanup: settled.cleanup } : null,
      inconclusive: !settled,
      poll_errors: this.pollErrors,
      remaining_ms: Math.max(0, deadline - Date.now()),
    };
  }

  async launchOperation(operation, { requestId, deadlineSeconds } = {}) {
    const res = await this.fetchImpl(`${this.supervisorUrl}/jobs`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ operation, request_id: requestId,
        deadline_seconds: deadlineSeconds }),
      signal: AbortSignal.timeout(this.options.pollTimeoutMs),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`launch_failed_${res_status(res)}: ` +
      (data.error ?? "unknown"));
    return data; // { job_id, replayed, state }
  }

  async fetchJob(jobId) {
    const res = await this.fetchImpl(`${this.supervisorUrl}/jobs/${jobId}`,
      { headers: this.headers(),
        signal: AbortSignal.timeout(this.options.pollTimeoutMs) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`job_fetch_failed_${res.status}`);
    return data;
  }

  stats() {
    return { polls: this.polls, poll_errors: this.pollErrors,
      distinct_events: this.seen.size, cursor: this.cursor };
  }
}

function res_status(res) {
  return res.status;
}
