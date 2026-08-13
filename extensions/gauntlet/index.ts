/**
 * pi-gauntlet extension entry point (§6.4): wire-up only.
 *
 * - state singleton + session_start restoration
 * - /gate command (gate.ts)
 * - entry renderers (render.ts)
 * - gauntlet_status tool so the model itself can consult the gate
 * - test-lock (testlock.ts, Phase 4)
 */
import {
  DEFAULT_MAX_BYTES,
  DEFAULT_MAX_LINES,
  truncateTail,
  type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { registerGate } from "./gate";
import { registerRenderers } from "./render";
import { getState, initState, isBlocking, registerStateRestoration } from "./state";
import { registerTestLock } from "./testlock";

function statusSummary(): string {
  const state = getState();
  const report = state.lastReport;
  if (!report) {
    return "No gauntlet gate has run in this session yet. Run /gate (or `gauntlet run --tier fast --json` via bash) to produce a report.";
  }
  const blocking = report.findings.filter(isBlocking);
  const lines = [
    `gauntlet last report: tier=${report.tier} base=${report.base} head=${report.head}`,
    `verdict: ${blocking.length === 0 ? "green" : `RED (${blocking.length} blocking)`}`,
    `mode: ${state.mode}, fix round ${state.round}/${state.maxRounds}, ` +
      `${state.dismissed.size} finding(s) dismissed by user`,
    `runners: ${Object.entries(report.stats.runners)
      .map(([name, status]) => `${name}=${status}`)
      .join(", ")}`,
  ];
  for (const f of blocking) {
    const loc = f.file ? `${f.file}:${f.line}` : "(repo)";
    lines.push(`[${f.severity}] ${f.tool} ${loc} — ${f.message} (${f.action})`);
  }
  return lines.join("\n");
}

export default function (pi: ExtensionAPI) {
  initState(pi);
  registerStateRestoration(pi);
  registerRenderers(pi);
  registerGate(pi);
  registerTestLock(pi);

  pi.registerTool({
    name: "gauntlet_status",
    label: "Gauntlet Status",
    description:
      "Summary of the most recent gauntlet gate report: per-tier verdicts, blocking findings, fix-round state, and user dismissals.",
    promptSnippet: "Check the current gauntlet gate status",
    promptGuidelines: [
      "Use gauntlet_status before claiming the code is ready to push.",
    ],
    parameters: Type.Object({}),
    async execute(_toolCallId, _params) {
      const truncation = truncateTail(statusSummary(), {
        maxBytes: DEFAULT_MAX_BYTES,
        maxLines: DEFAULT_MAX_LINES,
      });
      return {
        content: [{ type: "text", text: truncation.content }],
        details: {},
      };
    },
  });
}
