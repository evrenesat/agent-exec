import { appendFileSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { tool } from "@opencode-ai/plugin";
import { BridgeCore } from "./exec-bridge-core.mjs";

// Execution-layer bridge for OpenCode 1.18.25: named project operations run
// through the supervisor (which alone owns launches, systemd units and
// deadlines). project_run blocks the model turn until a truthful terminal
// receipt or the bounded wait elapses — foreground work never runs without a
// deadline, and failures are never masked. This is the harness-facing shape of
// the 14 October handoff's experiment 1.
export default async function ExecBridge() {
  const config = JSON.parse(readFileSync(
    resolve(process.env.OPENCODE_CONFIG_DIR, "xtx-exec-bridge.json"), "utf8"));
  const log = (entry) => {
    try {
      appendFileSync(config.logPath, JSON.stringify({ t: Date.now(), ...entry }) + "\n",
        { mode: 0o600 });
    } catch { /* bounded best-effort log */ }
  };
  const core = new BridgeCore({
    supervisorUrl: config.supervisorUrl, log,
    options: { pollIntervalMs: config.pollIntervalMs ?? 250,
      pollTimeoutMs: config.pollTimeoutMs ?? 3000 },
  });

  const waitTool = tool({
    description:
      "Wait for external supervisor job events (bounded, deduplicated). " +
      "Returns bounded receipts; never launches or relaunches work.",
    args: { timeout_ms: tool.schema.number().int().positive().max(120000) },
    execute: async (args) => {
      const receipt = await core.pollUntilTerminal({ maxWaitMs: args.timeout_ms });
      return { title: "exec_job_wait", output: JSON.stringify(receipt),
        metadata: { supervisor: config.supervisorName ?? "exec-supervisor" } };
    },
  });

  const runTool = tool({
    description:
      "Run a named project operation (test/build/lint) through the trusted " +
      "supervisor with an OS-enforced deadline, and wait for its truthful " +
      "terminal receipt (outcome, exit status, test totals, cleanup evidence). " +
      "This never edits files, installs packages or repairs failures. If the " +
      "receipt is inconclusive, say so; do not treat it as success.",
    args: {
      operation: tool.schema.string().describe("Named operation id, e.g. aflow.web.unit"),
      request_id: tool.schema.string().describe("Stable id for this request; replays return the same job"),
      timeout_ms: tool.schema.number().int().positive().max(300000)
        .optional().describe("How long to wait for the terminal receipt (default 120000)"),
    },
    execute: async (args) => {
      const launch = await core.launchOperation(args.operation,
        { requestId: args.request_id });
      // Wait for THIS job's terminal event only: the supervisor stream is
      // shared across jobs, so an unfiltered wait could settle on some other
      // job's completion. Receipt fetched by the id launch returned.
      const receipt = await core.pollUntilTerminal(
        { jobId: launch.job_id, maxWaitMs: args.timeout_ms ?? 120000 });
      let job = null;
      if (receipt.settled_event) {
        try {
          job = await core.fetchJob(launch.job_id);
        } catch (error) {
          log({ phase: "job_fetch_error", error: String(error).slice(0, 120) });
        }
      }
      return {
        title: `project_run(${args.operation})`,
        output: JSON.stringify({ launch, receipt, job: job && {
          outcome: job.outcome, exit_code: job.exit_code,
          exit_status: job.exit_status, cleanup: job.cleanup,
          report: job.report ? {
            total: job.report.total, passed: job.report.passed,
            failed: job.report.failed, complete: job.report.complete,
          } : null },
        }),
        metadata: { supervisor: config.supervisorName ?? "exec-supervisor",
          job_id: launch.job_id, replayed: launch.replayed === true },
      };
    },
  });

  return {
    tool: { project_run: runTool, exec_job_wait: waitTool },
    dispose: async () => {
      log({ phase: "dispose", ...core.stats() });
    },
  };
}
