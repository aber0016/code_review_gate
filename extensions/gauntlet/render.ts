/**
 * Findings widget/entry rendering (§6.3 step 5 of the plan).
 *
 * `gauntlet-report` custom entries render as a compact per-tier table;
 * expanded view shows per-finding evidence. Entries never participate in
 * LLM context.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import type { Finding, GauntletReport } from "./state";
import { isBlocking } from "./state";

export interface GateRunRecord {
  base: string;
  reports: GauntletReport[];
  parked: number;
  dismissed: number;
  round: number;
  maxRounds: number;
  verdict: "green" | "red" | "crashed";
  /** Layer-6 cross-model review findings, when the review stage ran. */
  reviewFindings?: Finding[];
}

function severityColor(severity: Finding["severity"]): "error" | "warning" | "dim" {
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  return "dim";
}

/** One-line summary for a tier report (user-dismissed findings excluded). */
export function tierSummary(
  report: GauntletReport,
  dismissed?: ReadonlySet<string>,
): string {
  const blocking = report.findings.filter(
    (f) => isBlocking(f) && !dismissed?.has(f.id),
  ).length;
  const mark = blocking === 0 ? "✓" : "✗";
  const dur = report.stats.duration_s.toFixed(1);
  return `${report.tier} ${mark} ${dur}s${blocking ? ` ${blocking} blocking` : ""}`;
}

export function registerRenderers(pi: ExtensionAPI): void {
  pi.registerEntryRenderer<GateRunRecord>("gauntlet-report", (entry, { expanded }, theme) => {
    const record = entry.data;
    if (!record) return new Text(theme.fg("dim", "⛩ gauntlet: (empty report)"), 0, 0);
    const head =
      `⛩ gauntlet gate ${record.base}..HEAD — ` +
      record.reports.map((report) => tierSummary(report)).join(" | ") +
      ` | ${record.parked} parked | round ${record.round}/${record.maxRounds}`;
    const verdictColor = record.verdict === "green" ? "success" : "error";
    let text = theme.fg(verdictColor, theme.bold(head));
    const findings = [
      ...record.reports.flatMap((r) => r.findings),
      ...(record.reviewFindings ?? []),
    ];
    if (!expanded) {
      const blocking = findings.filter(isBlocking);
      if (blocking.length > 0) {
        text += theme.fg(
          "dim",
          `\n  ${blocking.length} blocking finding(s) — expand for evidence`,
        );
      }
      return new Text(text, 0, 0);
    }
    for (const f of findings) {
      const loc = f.file ? `${f.file}:${f.line}` : "(repo)";
      text +=
        "\n" +
        theme.fg(severityColor(f.severity), `  [${f.severity}] `) +
        `${f.tool} ${loc} — ${f.message} ` +
        theme.fg("dim", `(${f.action})`);
      if (f.evidence) {
        const evidence = f.evidence.length > 400 ? `${f.evidence.slice(0, 400)}…` : f.evidence;
        text += "\n" + theme.fg("dim", `      ${evidence.replaceAll("\n", "\n      ")}`);
      }
    }
    return new Text(text, 0, 0);
  });

  pi.registerEntryRenderer<{ ids: string[] }>("gauntlet-dismissed", (entry, _options, theme) => {
    const count = entry.data?.ids?.length ?? 0;
    return new Text(theme.fg("dim", `⛩ gauntlet: ${count} finding(s) dismissed by user`), 0, 0);
  });

  pi.registerEntryRenderer<{ round: number; maxRounds: number; findingIds: string[] }>(
    "gauntlet-round",
    (entry, _options, theme) => {
      const data = entry.data;
      if (!data) return undefined;
      return new Text(
        theme.fg(
          "warning",
          `⛩ gauntlet: fix round ${data.round}/${data.maxRounds} — ${data.findingIds.length} auto-fix finding(s)`,
        ),
        0,
        0,
      );
    },
  );
}
