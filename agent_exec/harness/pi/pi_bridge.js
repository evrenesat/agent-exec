// Thin Pi execution bridge — checkpoint 5 second harness (plan
// reliable-agent-execution-20261013.md, Design B; F2 decision: thin bridge,
// external trusted jobs delivered into a pi session, never a second process
// manager). Mirrors the OpenCode bridge tool contract:
//
//   project_run    POST <supervisor>/jobs {operation, request_id}, then
//                  bounded deduplicated cursor polling of GET /events?cursor=
//   exec_job_wait  bounded cursor polling only; never launches or relaunches
//
// The supervisor alone owns launches, OS deadlines and cleanup evidence.
// Elapsed waits are inconclusive, never success. Loaded by the pinned pi
// 1.2.0 runtime via `-e <this file>` (jiti default-export factory); plain
// JSON-Schema parameter objects are validated by the runtime's non-TypeBox
// coercion path, so no typebox import is needed.
//
// Config: XTX_EXEC_BRIDGE_CONFIG (JSON file: supervisorUrl, logPath,
// pollIntervalMs, pollTimeoutMs) or XTX_EXEC_SUPERVISOR_URL directly.
import { appendFileSync, readFileSync } from "node:fs";

import { BridgeCore } from "./pi-bridge-core.mjs";

const PROJECT_RUN_PARAMS = {
  type: "object",
  properties: {
    operation: { type: "string",
      description: "Named operation id registered on the supervisor, e.g. aflow.web.unit" },
    request_id: { type: "string",
      description: "Stable id for this request; retransmits return the same job" },
    timeout_ms: { type: "number",
      description: "How long to wait for the terminal receipt (default 120000, max 300000)" },
  },
  required: ["operation", "request_id"],
  additionalProperties: false,
};

const WAIT_PARAMS = {
  type: "object",
  properties: {
    timeout_ms: { type: "number",
      description: "How long to wait for new supervisor events (default 30000, max 120000)" },
  },
  additionalProperties: false,
};

const clampMs = (value, fallback, max) => {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.round(n), max);
};

function loadBridgeConfig() {
  const configPath = process.env.XTX_EXEC_BRIDGE_CONFIG;
  if (configPath) {
    const config = JSON.parse(readFileSync(configPath, "utf8"));
    if (!config.supervisorUrl) {
      throw new Error(`bridge config ${configPath} lacks supervisorUrl`);
    }
    return config;
  }
  if (process.env.XTX_EXEC_SUPERVISOR_URL) {
    return { supervisorUrl: process.env.XTX_EXEC_SUPERVISOR_URL };
  }
  throw new Error(
    "exec bridge not configured: set XTX_EXEC_BRIDGE_CONFIG or XTX_EXEC_SUPERVISOR_URL");
}

export default function execBridge(pi) {
  let core = null;
  let config = null;
  const getCore = () => {
    if (!core) {
      config = loadBridgeConfig();
      const log = (entry) => {
        if (!config.logPath) return;
        try {
          appendFileSync(config.logPath,
            JSON.stringify({ t: Date.now(), ...entry }) + "\n", { mode: 0o600 });
        } catch { /* bounded best-effort log */ }
      };
      core = new BridgeCore({
        supervisorUrl: config.supervisorUrl,
      token: config?.token ?? null, log,
        options: { pollIntervalMs: config.pollIntervalMs ?? 250,
          pollTimeoutMs: config.pollTimeoutMs ?? 3000 },
      });
    }
    return core;
  };

  pi.registerTool({
    name: "project_run",
    label: "Project run",
    description:
      "Run a named project operation (test/build/lint) through the trusted " +
      "supervisor with an OS-enforced deadline, and wait for its truthful " +
      "terminal receipt (outcome, exit status, test totals, cleanup evidence). " +
      "This never edits files, installs packages or repairs failures. If the " +
      "receipt is inconclusive (wait elapsed without terminal evidence), say " +
      "so and do not treat it as success.",
    parameters: PROJECT_RUN_PARAMS,
    async execute(_toolCallId, params, signal) {
      const bridge = getCore();
      const launch = await bridge.launchOperation(params.operation,
        { requestId: params.request_id });
      // Wait for THIS job's terminal event only — the supervisor stream is
      // shared across jobs, and an unfiltered wait could settle on some other
      // job's completion. Receipt is fetched by the id launch returned.
      const receipt = await bridge.pollUntilTerminal({
        jobId: launch.job_id,
        maxWaitMs: clampMs(params.timeout_ms, 120000, 300000), signal });
      let job = null;
      if (receipt.terminal) {
        try {
          job = await bridge.fetchJob(launch.job_id);
        } catch (error) {
          bridge.log({ phase: "job_fetch_error",
            error: String(error).slice(0, 120) });
        }
      }
      const payload = { launch, receipt, job: job && {
        outcome: job.outcome, exit_code: job.exit_code,
        exit_status: job.exit_status, cleanup: job.cleanup,
        report: job.report ? { total: job.report.total,
          passed: job.report.passed, failed: job.report.failed,
          complete: job.report.complete } : null } };
      return {
        content: [{ type: "text",
          text: JSON.stringify(payload) + (receipt.inconclusive
            ? "\nINCONCLUSIVE: the bounded wait elapsed without a terminal " +
              "supervisor event. Do not report this operation as passing." : "") }],
        details: { supervisor: config?.supervisorName ?? "exec-supervisor",
          job_id: launch.job_id, replayed: launch.replayed === true,
          inconclusive: receipt.inconclusive,
          polls: bridge.stats().polls },
      };
    },
  });

  pi.registerTool({
    name: "exec_job_wait",
    label: "Exec job wait",
    description:
      "Wait for external supervisor job events (bounded, deduplicated by " +
      "job revision). Returns bounded receipts; never launches or relaunches " +
      "work. Inconclusive when the wait elapses without new terminal events.",
    parameters: WAIT_PARAMS,
    async execute(_toolCallId, params, signal) {
      const bridge = getCore();
      const receipt = await bridge.pollUntilTerminal({
        maxWaitMs: clampMs(params?.timeout_ms, 30000, 120000), signal });
      return {
        content: [{ type: "text", text: JSON.stringify(receipt) +
          (receipt.inconclusive
            ? "\nINCONCLUSIVE: no new terminal event within the bounded wait." : "") }],
        details: { supervisor: config?.supervisorName ?? "exec-supervisor",
          inconclusive: receipt.inconclusive, polls: bridge.stats().polls },
      };
    },
  });
}
