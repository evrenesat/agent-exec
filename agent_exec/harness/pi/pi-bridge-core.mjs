// Pi-local execution-bridge core: cursor polling, revision dedupe, transient
// retry, bounded receipts. Private copy for the Pi harness (checkpoint 5
// second harness, plan reliable-agent-execution-20261013.md). Do not import
// across harness dirs at runtime: this mirrors
// agent_exec/harness/opencode/exec-bridge-core.mjs contract with
// three documented deltas: (1) launch POST retries transient failures with
// the same idempotent request_id, (2) pollUntilTerminal reports a truthful
// inconclusive flag when the bounded wait elapses without a terminal event,
// (3) cooperative AbortSignal checks between polls. Pure logic with an
// injectable fetch — exercised offline by the fixture driver without pi.

export const DEFAULTS = {
  pollIntervalMs: 250,
  pollTimeoutMs: 3000,
  maxWaitMs: 60000,
  launchRetries: 2,
  retryBackoffMs: 250,
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function isTransientStatus(status) {
  return status === 429 || status === 502 || status === 503 || status === 504;
}

export class BridgeCore {
  constructor({ supervisorUrl, log = () => {}, options = {}, fetchImpl = fetch }) {
    this.supervisorUrl = supervisorUrl.replace(/\/$/, "");
    this.log = log;
    this.options = { ...DEFAULTS, ...options };
    this.fetchImpl = fetchImpl.bind(globalThis);
    this.seen = new Set(); // "job_id:revision" dedupe — at-least-once delivery
    this.cursor = 0;
    this.polls = 0;
    this.pollErrors = 0;
    this.launchRetries = 0;
  }

  async pollOnce() {
    this.polls += 1;
    const res = await this.fetchImpl(
      `${this.supervisorUrl}/events?cursor=${this.cursor}`,
      { signal: AbortSignal.timeout(this.options.pollTimeoutMs) });
    if (!res.ok) throw new Error(`supervisor_http_${res.status}`);
    const data = await res.json();
    const previous = this.cursor;
    // Cursor is monotonic: never move backwards on a misbehaving response.
    this.cursor = typeof data.cursor === "number" && data.cursor > this.cursor
      ? data.cursor : this.cursor;
    const fresh = [];
    for (const ev of data.events ?? []) {
      const key = `${ev.job_id}:${ev.revision}`;
      if (this.seen.has(key)) continue;
      this.seen.add(key);
      fresh.push(ev);
    }
    this.log({ phase: "poll", polls: this.polls, cursor: this.cursor,
      previous_cursor: previous, received: (data.events ?? []).length,
      fresh: fresh.length });
    return fresh;
  }

  async pollUntilTerminal({ maxWaitMs, pollIntervalMs, signal } = {}) {
    const deadline = Date.now() + (maxWaitMs ?? this.options.maxWaitMs);
    const collected = [];
    let aborted = false;
    let terminal = null;
    while (Date.now() < deadline) {
      if (signal?.aborted) { aborted = true; break; }
      let fresh = [];
      try {
        fresh = await this.pollOnce();
      } catch (error) {
        // Transient supervisor/poll failures never cancel or relaunch work.
        this.pollErrors += 1;
        this.log({ phase: "poll_error", error: String(error).slice(0, 120) });
      }
      for (const ev of fresh) {
        collected.push(ev);
        if (ev.terminal && !terminal) terminal = ev;
      }
      if (terminal) break;
      await sleep(pollIntervalMs ?? this.options.pollIntervalMs);
    }
    return {
      events: collected.map((e) => ({ job_id: e.job_id, revision: e.revision,
        status: e.status, terminal: e.terminal === true, outcome: e.outcome,
        cleanup: e.cleanup })),
      poll_errors: this.pollErrors,
      remaining_ms: Math.max(0, deadline - Date.now()),
      terminal,
      aborted,
      // Truthful: elapsed wait without terminal evidence is inconclusive,
      // never a success and never a failure of the underlying job.
      inconclusive: !terminal,
    };
  }

  async launchOperation(operation, { requestId, deadlineSeconds } = {}) {
    let lastError = null;
    for (let attempt = 0; attempt <= this.options.launchRetries; attempt += 1) {
      if (attempt > 0) {
        this.launchRetries += 1;
        await sleep(this.options.retryBackoffMs);
      }
      try {
        const res = await this.fetchImpl(`${this.supervisorUrl}/jobs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operation, request_id: requestId,
            deadline_seconds: deadlineSeconds }),
          signal: AbortSignal.timeout(this.options.pollTimeoutMs),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) return data; // { job_id, replayed, state }
        if (isTransientStatus(res.status)) {
          lastError = new Error(`launch_transient_${res.status}: ` +
            (data.error ?? "unknown"));
          continue; // safe: request_id idempotency, replays return same job
        }
        throw new Error(`launch_failed_${res.status}: ` + (data.error ?? "unknown"));
      } catch (error) {
        if (error instanceof TypeError) { // network-level transient
          lastError = error;
          continue;
        }
        throw error;
      }
    }
    throw lastError ?? new Error("launch_failed: retries exhausted");
  }

  async fetchJob(jobId) {
    const res = await this.fetchImpl(`${this.supervisorUrl}/jobs/${jobId}`,
      { signal: AbortSignal.timeout(this.options.pollTimeoutMs) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`job_fetch_failed_${res.status}`);
    return data;
  }

  stats() {
    return { polls: this.polls, poll_errors: this.pollErrors,
      launch_retries: this.launchRetries, distinct_events: this.seen.size,
      cursor: this.cursor };
  }
}
