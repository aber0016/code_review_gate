/**
 * The /gate command (§6.3) and the bounded fix loop (§7.2).
 *
 * The extension only orchestrates: every deterministic check lives in the
 * `gauntlet` CLI. The gate walks tiers sequentially with early exit; auto-fix
 * findings dispatch a fix round (mode "fix" — the test-lock arms itself),
 * `agent_settled` re-runs the failed tier, and the budget (`fix.max_rounds`)
 * bounds the loop. Ask-user findings park for a human (Layer 7).
 */
import type {
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import type { GateRunRecord } from "./render";
import { tierSummary } from "./render";
import { runCrossReview } from "./review";
import type { Finding, GauntletReport } from "./state";
import {
  dismiss,
  gauntletBin,
  getState,
  isBlocking,
  readGauntletConfig,
  setLastReport,
  setMode,
  setRound,
} from "./state";

const TIER_TIMEOUT_MS = 900_000;
const PARK_DIALOG_CAP = 10;

export interface GateArgs {
  base?: string;
  deep: boolean;
  gen: boolean;
  bugfix: boolean;
  reviewOnly: boolean;
  intent?: string;
}

/** Parse `/gate [base] [--deep] [--gen] [--bugfix] [--review-only] [--intent "…"]`. */
export function parseGateArgs(raw: string): GateArgs {
  const args: GateArgs = {
    base: undefined,
    deep: false,
    gen: false,
    bugfix: false,
    reviewOnly: false,
    intent: undefined,
  };
  const tokens = raw.match(/"[^"]*"|\S+/g) ?? [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === "--deep") args.deep = true;
    else if (token === "--gen") args.gen = true;
    else if (token === "--bugfix") args.bugfix = true;
    else if (token === "--review-only") args.reviewOnly = true;
    else if (token === "--intent") {
      const next = tokens[i + 1];
      if (next !== undefined) {
        args.intent = next.replace(/^"|"$/g, "");
        i++;
      }
    } else if (!token.startsWith("--") && args.base === undefined) {
      args.base = token;
    }
  }
  return args;
}

export interface TierRun {
  tier: string;
  report?: GauntletReport;
  exitCode: number;
  errorText?: string;
}

/** Run one gauntlet tier via the CLI; exit 2 or unparseable JSON = crash. */
export async function runTierCli(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  tier: string,
  base: string | undefined,
): Promise<TierRun> {
  const argv = ["run", "--tier", tier, "--json"];
  if (base) argv.push("--base", base);
  const result = await pi.exec(gauntletBin(ctx.cwd), argv, {
    timeout: TIER_TIMEOUT_MS,
    cwd: ctx.cwd,
  });
  if (result.code === 2) {
    return { tier, exitCode: 2, errorText: (result.stderr || result.stdout).slice(0, 2000) };
  }
  try {
    const report = JSON.parse(result.stdout) as GauntletReport;
    return { tier, report, exitCode: result.code };
  } catch {
    return {
      tier,
      exitCode: 2,
      errorText: `unparseable gauntlet output (exit ${result.code}): ${(
        result.stderr || result.stdout
      ).slice(0, 2000)}`,
    };
  }
}

export interface Partition {
  noop: Finding[];
  autoFix: Finding[];
  askUser: Finding[];
}

/** Drop dismissed ids, then split by action. */
export function partitionFindings(findings: Finding[], dismissed: ReadonlySet<string>): Partition {
  const kept = findings.filter((f) => !dismissed.has(f.id));
  return {
    noop: kept.filter((f) => f.action === "no-op"),
    autoFix: kept.filter((f) => f.action === "auto-fix" && f.severity !== "info"),
    askUser: kept.filter((f) => f.action === "ask-user" && f.severity !== "info"),
  };
}

// ---------------------------------------------------------------------------
// Gate loop state (survives across agent_settled continuations).
// ---------------------------------------------------------------------------

interface ActiveGate {
  args: GateArgs;
  base?: string;
  baseLabel: string;
  tiers: string[];
  tierIndex: number;
  latestRuns: Map<string, TierRun>;
  parked: Finding[];
  /** Ids the user explicitly sent to the fix round (Layer 7 decision). */
  decidedFixIds: Set<string>;
  /** Findings from the Layer-6 cross-model review (for the record entry). */
  reviewFindings: Finding[];
}

let activeGate: ActiveGate | undefined;

/** Test seam / introspection: the currently active gate loop, if any. */
export function gateInProgress(): boolean {
  return activeGate !== undefined;
}

// ---------------------------------------------------------------------------
// Parking (Layer 7).
// ---------------------------------------------------------------------------

/**
 * Park ask-user findings for a human decision. Walks at most
 * PARK_DIALOG_CAP dialogs per pass; the rest stay parked. Returns "abort"
 * if the user aborted the gate.
 */
async function parkFindings(
  ctx: ExtensionContext,
  gate: ActiveGate,
  findings: Finding[],
  suffix = "",
): Promise<Finding[] | "abort"> {
  if (!ctx.hasUI) return findings; // §6.3.7: headless = all ask-user block
  const parked: Finding[] = [];
  let asked = 0;
  let bailed = false;
  for (const finding of findings) {
    if (bailed || asked >= PARK_DIALOG_CAP) {
      parked.push(finding);
      continue;
    }
    asked++;
    const location = finding.file ? ` [${finding.file}:${finding.line}]` : "";
    // The suffix (e.g. "auto-fix budget exhausted") must survive truncation —
    // it is the context the human decides on.
    const body = `${finding.tool}${location}: ${finding.message}`.slice(
      0,
      Math.max(0, 200 - suffix.length),
    );
    const title = `${body}${suffix}`;
    const choice = await ctx.ui.select(title, [
      "Approve (dismiss)",
      "Send to fix round",
      "Abort gate",
    ]);
    if (choice === "Approve (dismiss)") {
      dismiss(finding.id);
    } else if (choice === "Send to fix round") {
      gate.decidedFixIds.add(finding.id);
    } else if (choice === "Abort gate") {
      return "abort";
    } else {
      // Esc / no decision: stop asking, keep the rest parked.
      bailed = true;
      parked.push(finding);
    }
  }
  const remaining = findings.length - asked;
  if (remaining > 0) {
    ctx.ui.notify(
      `gauntlet: ${remaining} more ask-user finding(s) not shown (cap ${PARK_DIALOG_CAP}); they stay parked`,
      "warning",
    );
  }
  return parked;
}

// ---------------------------------------------------------------------------
// Widget / status / record.
// ---------------------------------------------------------------------------

function updateWidget(ctx: ExtensionContext, gate: ActiveGate): void {
  if (!ctx.hasUI) return;
  const state = getState();
  const segments = gate.tiers
    .map((tier) => gate.latestRuns.get(tier))
    .filter((run): run is TierRun => run !== undefined)
    .map((run) =>
      run.report
        ? tierSummary(run.report, state.dismissed)
        : `${run.tier} ✗ crashed`,
    );
  ctx.ui.setWidget("gauntlet", [
    `⛩ gate ${gate.baseLabel}..HEAD — ${segments.join(" | ") || "(no tiers run)"} | ` +
      `${gate.parked.length} parked | round ${state.round}/${state.maxRounds}`,
  ]);
}

function setGateStatus(ctx: ExtensionContext, blocking: number, crashed: boolean): void {
  if (!ctx.hasUI) return;
  if (crashed) ctx.ui.setStatus("gauntlet", "gate: CRASHED (fail closed)");
  else if (blocking > 0) ctx.ui.setStatus("gauntlet", `gate: RED (${blocking} blocking)`);
  else ctx.ui.setStatus("gauntlet", "gate: green");
}

function finalize(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  gate: ActiveGate,
  verdict: GateRunRecord["verdict"],
  blocking: number,
): void {
  setMode("author");
  setGateStatus(ctx, blocking, verdict === "crashed");
  updateWidget(ctx, gate);
  const state = getState();
  const record: GateRunRecord = {
    base: gate.baseLabel,
    reports: [...gate.latestRuns.values()]
      .map((run) => run.report)
      .filter((r): r is GauntletReport => r !== undefined),
    parked: gate.parked.length,
    dismissed: state.dismissed.size,
    round: state.round,
    maxRounds: state.maxRounds,
    verdict,
    reviewFindings: gate.reviewFindings,
  };
  pi.appendEntry("gauntlet-report", record);
  activeGate = undefined;
}

// ---------------------------------------------------------------------------
// The fix round (§7.2).
// ---------------------------------------------------------------------------

/** Build the fix prompt: findings JSON + base + standing instructions. */
export function buildFixPrompt(
  findings: Finding[],
  base: string,
  round: number,
  maxRounds: number,
): string {
  const trimmed = findings.map((f) => ({
    ...f,
    evidence: f.evidence.length > 500 ? `${f.evidence.slice(0, 500)}…` : f.evidence,
  }));
  return [
    `The gauntlet gate found ${findings.length} fixable finding(s) — fix round ` +
      `${round}/${maxRounds}, diff base ${base}.`,
    "",
    "```json",
    JSON.stringify(trimmed, null, 2),
    "```",
    "",
    "Instructions:",
    "- Fix ONLY these findings, with the smallest change that resolves each root cause.",
    "- Work in src/. Test files are read-only in this round (mechanically enforced —",
    "  do not try to edit, skip, or revert tests).",
    "- Confirm each fix by re-running just the named check for that finding, e.g.",
    "  `.venv/bin/ruff check <file>`, `.venv/bin/mypy <file>`,",
    "  `.venv/bin/python -m pytest <test-id>`. Do NOT run the full test suite and do",
    "  NOT run `gauntlet` yourself — the gate re-runs automatically when you finish.",
    "- Never install a package to make an import resolve.",
  ].join("\n");
}

function startFixRound(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  gate: ActiveGate,
  findings: Finding[],
): void {
  const state = getState();
  const round = state.round + 1;
  setRound(round);
  setMode("fix"); // arms the test-lock BEFORE any fixing tool call can fire
  pi.appendEntry("gauntlet-round", {
    round,
    maxRounds: state.maxRounds,
    findingIds: findings.map((f) => f.id),
  });
  updateWidget(ctx, gate);
  if (ctx.hasUI) {
    ctx.ui.notify(
      `gauntlet: fix round ${round}/${state.maxRounds} — ${findings.length} finding(s)`,
      "info",
    );
  }
  const base = gate.latestRuns.get(gate.tiers[gate.tierIndex])?.report?.base ?? gate.baseLabel;
  pi.sendUserMessage(buildFixPrompt(findings, base, round, state.maxRounds), {
    deliverAs: "followUp",
  });
}

// ---------------------------------------------------------------------------
// The gate pass: run tiers from the current index until green/red/fixing.
// ---------------------------------------------------------------------------

/**
 * Whether any tier run so far flagged a critical-path (red-list) file. The id
 * prefix comes from the CLI's critical-paths runner — the extension never
 * re-implements the glob matching (design principle 1: one binary decides).
 */
function criticalPathTouched(gate: ActiveGate): boolean {
  return [...gate.latestRuns.values()].some(
    (run) => run.report?.findings.some((f) => f.id.startsWith("critical-paths:")) ?? false,
  );
}

async function executePass(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  gate: ActiveGate,
): Promise<void> {
  while (gate.tierIndex < gate.tiers.length) {
    const tier = gate.tiers[gate.tierIndex];
    const run = await runTierCli(pi, ctx, tier, gate.base);
    gate.latestRuns.set(tier, run);
    if (run.exitCode === 2 || !run.report) {
      const message = `gauntlet ${tier} tier crashed: ${run.errorText ?? "?"}`;
      if (ctx.hasUI) ctx.ui.notify(message, "error");
      else console.log(message);
      finalize(pi, ctx, gate, "crashed", 0);
      return;
    }
    setLastReport(run.report);
    if (!ctx.hasUI) console.log(JSON.stringify(run.report, null, 2));

    const partition = partitionFindings(run.report.findings, getState().dismissed);
    const undecided = partition.askUser.filter((f) => !gate.decidedFixIds.has(f.id));
    const parkResult = await parkFindings(ctx, gate, undecided);
    if (parkResult === "abort") {
      if (ctx.hasUI) ctx.ui.notify("gauntlet: gate aborted by user", "warning");
      gate.parked = undecided;
      finalize(pi, ctx, gate, "red", undecided.length || 1);
      return;
    }
    gate.parked = parkResult;

    const pendingFix = [
      ...partition.autoFix,
      ...partition.askUser.filter((f) => gate.decidedFixIds.has(f.id)),
    ];
    const parkedBlocking = parkResult.filter(isBlocking);

    if (pendingFix.length > 0) {
      const state = getState();
      if (state.round < state.maxRounds) {
        updateWidget(ctx, gate);
        startFixRound(pi, ctx, gate, pendingFix);
        return; // resumed by agent_settled
      }
      // §7.2.3: budget exhausted — park everything remaining as ask-user.
      const exhaustedPark = await parkFindings(
        ctx,
        gate,
        pendingFix,
        " (auto-fix budget exhausted)",
      );
      if (exhaustedPark === "abort") {
        gate.parked = [...parkedBlocking, ...pendingFix];
        finalize(pi, ctx, gate, "red", gate.parked.length);
        return;
      }
      // Dismissal is the only clearing outcome here; "send to fix round"
      // cannot help once the budget is gone.
      const remaining = pendingFix.filter((f) => !getState().dismissed.has(f.id));
      gate.parked = [...parkedBlocking, ...remaining];
      if (gate.parked.length > 0) {
        finalize(pi, ctx, gate, "red", gate.parked.length);
        return;
      }
      updateWidget(ctx, gate);
      gate.tierIndex++; // everything left was dismissed — tier is clear
      continue;
    }

    if (parkedBlocking.length > 0) {
      updateWidget(ctx, gate);
      if (criticalPathTouched(gate)) {
        // Blast-radius rule: a red-list diff gets the Layer-6 cross-review
        // even on a red tier, so the human decides with all findings on the
        // table. reviewStage parks additively — the verdict stays red.
        await reviewStage(pi, ctx, gate);
        return;
      }
      finalize(pi, ctx, gate, "red", parkedBlocking.length);
      return; // early exit: don't run the next tier while this one is red
    }

    updateWidget(ctx, gate);
    gate.tierIndex++;
  }
  await reviewStage(pi, ctx, gate);
}

/**
 * Layer 6: cross-model review, run once the deterministic tiers are green
 * (§8.2), on `--review-only`, or forced on a red tier when the diff touched
 * a critical path (blast-radius rule). Findings merge into the same parking
 * flow; a "send to fix round" decision resets to tier 0 so the deterministic
 * gates re-verify whatever the fix changes.
 */
async function reviewStage(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  gate: ActiveGate,
): Promise<void> {
  if (ctx.hasUI) ctx.ui.setStatus("gauntlet", "gate: cross-review running…");
  const findings = await runCrossReview(pi, ctx, gate.base, gate.args.intent);
  gate.reviewFindings = findings;
  if (!ctx.hasUI && findings.length > 0) {
    console.log(JSON.stringify({ review_findings: findings }, null, 2));
  }
  const partition = partitionFindings(findings, getState().dismissed);
  const undecided = partition.askUser.filter((f) => !gate.decidedFixIds.has(f.id));
  const parkResult = await parkFindings(ctx, gate, undecided);
  if (parkResult === "abort") {
    if (ctx.hasUI) ctx.ui.notify("gauntlet: gate aborted by user", "warning");
    gate.parked = [...gate.parked, ...undecided];
    finalize(pi, ctx, gate, "red", gate.parked.length);
    return;
  }
  gate.parked = [...gate.parked, ...parkResult];
  const decidedFix = partition.askUser.filter((f) => gate.decidedFixIds.has(f.id));
  if (decidedFix.length > 0 && getState().round < getState().maxRounds) {
    gate.tierIndex = 0; // re-verify deterministically after the fix
    startFixRound(pi, ctx, gate, decidedFix);
    return;
  }
  const blocking =
    gate.parked.filter(isBlocking).length +
    (getState().round >= getState().maxRounds ? decidedFix.length : 0);
  finalize(pi, ctx, gate, blocking > 0 ? "red" : "green", blocking);
}

// ---------------------------------------------------------------------------
// The test-author (gen) round — §9.2.
// ---------------------------------------------------------------------------

interface GenRound {
  base?: string;
  bugfix: boolean;
  /** `git status --porcelain` over test paths at round start. */
  snapshot: string;
}

let activeGen: GenRound | undefined;

async function testStatusPorcelain(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
): Promise<string> {
  const config = readGauntletConfig(ctx.cwd);
  const result = await pi.exec(
    "git",
    ["-C", ctx.cwd, "status", "--porcelain", "--", ...config.testPaths],
    { timeout: 30_000 },
  );
  return result.stdout;
}

function porcelainPaths(porcelain: string): Set<string> {
  const paths = new Set<string>();
  for (const line of porcelain.split("\n")) {
    if (!line.trim()) continue;
    paths.add(line.slice(3).trim().replace(/^"|"$/g, ""));
  }
  return paths;
}

/** Build the test-author prompt (§9.2.2). */
export function buildGenPrompt(
  base: string,
  changedFiles: string[],
  scaffoldSections: string[],
  untested: Finding[],
  bugfix: boolean,
): string {
  const lines = [
    "This is a gauntlet **test-author round**: grow the test suite against the",
    `diff (base ${base}). src/ is mechanically read-only — you may only write`,
    "under the test paths.",
    "",
    `Changed files: ${changedFiles.join(", ") || "(none)"}`,
  ];
  if (untested.length > 0) {
    lines.push("", "Untested public functions:");
    for (const f of untested) {
      lines.push(`- ${f.message} (${f.file}:${f.line})`);
    }
  }
  if (scaffoldSections.length > 0) {
    lines.push(
      "",
      "Hypothesis scaffolds (suggestions, from .gauntlet/scaffolds/):",
      ...scaffoldSections,
    );
  }
  lines.push(
    "",
    "Rules:",
    "- Each new test must (a) run, (b) pass on the current code, and (c) strictly",
    "  increase diff coverage of the changed lines — tests that fail, error, or add",
    "  zero covered lines are mechanically rejected after this round.",
    "- Prefer properties/invariants (round-trips, idempotence, ordering, bounds)",
    "  over single examples.",
    "- Do not assert on implementation internals or source text.",
    "- Confirm with `.venv/bin/python -m pytest <your test file> -q`. Do NOT run",
    "  `gauntlet` yourself — validation runs automatically when you finish.",
  );
  if (bugfix) {
    lines.push(
      "- BUGFIX MODE: at least one kept test must FAIL on the pre-fix code",
      `  (${base}) and pass on the current code — write the test that demonstrates`,
      "  the fixed bug (fail-before-fix discipline).",
    );
  }
  return lines.join("\n");
}

async function startGenRound(
  pi: ExtensionAPI,
  ctx: ExtensionCommandContext,
  gate: ActiveGate,
): Promise<void> {
  const genRun = await runTierCli(pi, ctx, "gen", gate.base);
  gate.latestRuns.set("gen", genRun);
  if (genRun.exitCode === 2 || !genRun.report) {
    const message = `gauntlet gen tier crashed: ${genRun.errorText ?? "?"}`;
    if (ctx.hasUI) ctx.ui.notify(message, "error");
    else console.log(message);
    finalize(pi, ctx, gate, "crashed", 0);
    return;
  }
  setLastReport(genRun.report);
  if (!ctx.hasUI) console.log(JSON.stringify(genRun.report, null, 2));

  const untested = genRun.report.findings.filter((f) => f.id.includes(":untested:"));
  const scaffoldSections: string[] = [];
  const { readFileSync, existsSync, readdirSync } = await import("node:fs");
  const { join } = await import("node:path");
  const scaffoldDir = join(ctx.cwd, ".gauntlet", "scaffolds");
  if (existsSync(scaffoldDir)) {
    for (const name of readdirSync(scaffoldDir)) {
      if (!name.endsWith(".py")) continue;
      const content = readFileSync(join(scaffoldDir, name), "utf-8").slice(0, 4000);
      scaffoldSections.push("", `\`${name}\`:`, "```python", content, "```");
    }
  }

  activeGen = {
    base: gate.base,
    bugfix: gate.args.bugfix,
    snapshot: await testStatusPorcelain(pi, ctx),
  };
  setMode("test-author"); // arms the inverse lock BEFORE the authoring turn
  pi.appendEntry("gauntlet-gen-round", {
    bugfix: gate.args.bugfix,
    untested: untested.map((f) => f.id),
  });
  if (ctx.hasUI) {
    ctx.ui.notify(
      `gauntlet: test-author round — ${untested.length} untested function(s)`,
      "info",
    );
    ctx.ui.setStatus("gauntlet", "gate: test-author round running…");
  }
  pi.sendUserMessage(
    buildGenPrompt(
      genRun.report.base,
      genRun.report.changed_files,
      scaffoldSections,
      untested,
      gate.args.bugfix,
    ),
    { deliverAs: "followUp" },
  );
}

/**
 * §9.2.3: mechanically validate the authored tests.
 *
 * Runs the exec tier twice — once with the new tests stashed, once with
 * them (plus `--verify-fails-on` in bugfix mode) — and rejects (restores/
 * deletes) any new test file that fails, errors, or adds zero covered
 * lines. Kept tests are committed in exactly one commit.
 */
async function validateGenRound(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  const gate = activeGate;
  const gen = activeGen;
  if (!gate || !gen) return;
  setMode("author"); // disarm the inverse lock during validation
  activeGen = undefined;

  const after = await testStatusPorcelain(pi, ctx);
  const before = porcelainPaths(gen.snapshot);
  const newFiles = [...porcelainPaths(after)].filter((path) => !before.has(path));
  const notifyOrLog = (message: string, level: "info" | "warning" = "info") => {
    if (ctx.hasUI) ctx.ui.notify(message, level);
    else console.log(message);
  };

  if (newFiles.length === 0) {
    // Park for a human decision: "the suite is already adequate" is a
    // legitimate approval — and --deep must still get its mutation run.
    const noTests: Finding = {
      id: "gauntlet:gen-no-tests::0",
      layer: "test-gen",
      tool: "gauntlet",
      severity: "warning",
      action: "ask-user",
      file: "",
      line: 0,
      message: "test-author round produced no new tests",
      evidence: "",
      fix_hint: "re-run /gate --gen, or write the tests manually",
    };
    const parkResult = await parkFindings(ctx, gate, [noTests]);
    const stillParked = parkResult === "abort" ? [noTests] : parkResult;
    gate.parked = [...gate.parked, ...stillParked];
    const blocking = gate.parked.filter(isBlocking).length;
    if (gate.args.deep) {
      await deepStage(pi, ctx, gate, blocking);
      return;
    }
    finalize(pi, ctx, gate, blocking > 0 ? "red" : "green", blocking);
    return;
  }

  const coveragePct = (run: TierRun): number | undefined => {
    const finding = run.report?.findings.find(
      (f) => f.tool === "diff-cover" && f.id.includes(":coverage:"),
    );
    return finding ? Number.parseFloat(finding.evidence) : undefined;
  };

  // Pass 1: pre-round tree (new tests stashed).
  await pi.exec("git", ["-C", ctx.cwd, "stash", "push", "-u", "--", ...newFiles], {
    timeout: 60_000,
  });
  const withoutRun = await runTierCli(pi, ctx, "exec", gate.base);
  await pi.exec("git", ["-C", ctx.cwd, "stash", "pop"], { timeout: 60_000 });
  const pctBefore = coveragePct(withoutRun) ?? 0;

  // Pass 2: with the new tests (+ fail-before-fix guard in bugfix mode).
  const argv = ["run", "--tier", "exec", "--json"];
  if (gate.base) argv.push("--base", gate.base);
  if (gen.bugfix) {
    argv.push("--verify-fails-on", withoutRun.report?.base ?? gate.base ?? "HEAD~1");
  }
  const withResult = await pi.exec(gauntletBin(ctx.cwd), argv, {
    timeout: TIER_TIMEOUT_MS,
    cwd: ctx.cwd,
  });
  let withReport: GauntletReport | undefined;
  try {
    withReport = JSON.parse(withResult.stdout) as GauntletReport;
  } catch {
    withReport = undefined;
  }
  if (!withReport) {
    notifyOrLog("gauntlet: exec validation crashed after the test-author round", "warning");
    finalize(pi, ctx, gate, "crashed", 0);
    return;
  }
  gate.latestRuns.set("exec", { tier: "exec", report: withReport, exitCode: 0 });
  setLastReport(withReport);
  if (!ctx.hasUI) console.log(JSON.stringify(withReport, null, 2));

  const pctAfter =
    coveragePct({ tier: "exec", report: withReport, exitCode: 0 }) ?? 0;

  // Reject failing/erroring new test files.
  const failingFiles = new Set(
    withReport.findings
      .filter((f) => f.tool === "pytest" && f.severity === "error" && f.file)
      .map((f) => f.file),
  );
  const rejected: string[] = [];
  const kept: string[] = [];
  const rejectFile = async (path: string, why: string) => {
    rejected.push(`${path} (${why})`);
    const tracked = await pi.exec(
      "git",
      ["-C", ctx.cwd, "ls-files", "--error-unmatch", path],
      { timeout: 15_000 },
    );
    if (tracked.code === 0) {
      await pi.exec("git", ["-C", ctx.cwd, "restore", "--", path], { timeout: 15_000 });
    } else {
      const { rmSync } = await import("node:fs");
      const { join } = await import("node:path");
      rmSync(join(ctx.cwd, path), { force: true });
    }
  };

  for (const path of newFiles) {
    if (failingFiles.has(path)) await rejectFile(path, "fails or errors");
    else kept.push(path);
  }
  // §9.2.3: reject tests that add zero covered lines — except when diff
  // coverage is already saturated (100%): "strictly increase" is then
  // unsatisfiable, and mutation-killing boundary tests (Layer 5's whole
  // point) must not be rejected for a metric that cannot move.
  if (kept.length > 0 && pctAfter <= pctBefore && pctBefore < 100) {
    for (const path of [...kept]) {
      await rejectFile(path, "adds zero covered lines");
    }
    kept.length = 0;
  }

  notifyOrLog(
    `gauntlet test-author validation: kept ${kept.length} (${kept.join(", ") || "-"}), ` +
      `rejected ${rejected.length} (${rejected.join("; ") || "-"}); ` +
      `diff coverage ${pctBefore.toFixed(1)}% → ${pctAfter.toFixed(1)}%`,
    rejected.length > 0 ? "warning" : "info",
  );

  // Kept tests land in exactly one commit (§9 Done-when).
  if (kept.length > 0) {
    await pi.exec("git", ["-C", ctx.cwd, "add", "--", ...kept], { timeout: 30_000 });
    await pi.exec(
      "git",
      [
        "-C",
        ctx.cwd,
        "-c",
        "user.name=gauntlet",
        "-c",
        "user.email=gauntlet@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "test: gauntlet test-author round",
      ],
      { timeout: 30_000 },
    );
  }

  // The fail-before-fix guard (and any other blocking finding) parks.
  const partition = partitionFindings(withReport.findings, getState().dismissed);
  const undecided = partition.askUser.filter((f) => !gate.decidedFixIds.has(f.id));
  const parkResult = await parkFindings(ctx, gate, undecided);
  const stillParked = parkResult === "abort" ? undecided : parkResult;
  gate.parked = [...gate.parked, ...stillParked];
  const blocking =
    gate.parked.filter(isBlocking).length + (kept.length === 0 ? 1 : 0);
  if (gate.args.deep) {
    // §10: --deep implies gen-first so newly generated tests get scored.
    await deepStage(pi, ctx, gate, blocking);
    return;
  }
  finalize(pi, ctx, gate, blocking > 0 ? "red" : "green", blocking);
}

/**
 * Layer 5: mutation testing over the changed files (§10). Runs only when
 * explicitly requested via /gate --deep — never as part of a plain /gate.
 */
async function deepStage(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  gate: ActiveGate,
  blockingSoFar: number,
): Promise<void> {
  if (ctx.hasUI) ctx.ui.setStatus("gauntlet", "gate: deep tier (mutation) running…");
  const run = await runTierCli(pi, ctx, "deep", gate.base);
  gate.latestRuns.set("deep", run);
  if (run.exitCode === 2 || !run.report) {
    const message = `gauntlet deep tier crashed: ${run.errorText ?? "?"}`;
    if (ctx.hasUI) ctx.ui.notify(message, "error");
    else console.log(message);
    finalize(pi, ctx, gate, "crashed", blockingSoFar);
    return;
  }
  setLastReport(run.report);
  if (!ctx.hasUI) console.log(JSON.stringify(run.report, null, 2));
  const partition = partitionFindings(run.report.findings, getState().dismissed);
  const undecided = partition.askUser.filter((f) => !gate.decidedFixIds.has(f.id));
  const parkResult = await parkFindings(ctx, gate, undecided);
  const stillParked = parkResult === "abort" ? undecided : parkResult;
  gate.parked = [...gate.parked, ...stillParked];
  const blocking = blockingSoFar + stillParked.filter(isBlocking).length;
  finalize(pi, ctx, gate, blocking > 0 ? "red" : "green", blocking);
}

// ---------------------------------------------------------------------------
// Entry points.
// ---------------------------------------------------------------------------

/** The /gate command body. */
export async function runGate(
  pi: ExtensionAPI,
  ctx: ExtensionCommandContext,
  args: GateArgs,
): Promise<void> {
  await ctx.waitForIdle();
  const config = readGauntletConfig(ctx.cwd);
  setRound(0, config.maxRounds);
  setMode("author");

  if (args.deep) args.gen = true; // §10: --deep implies --gen first
  const base = args.base ?? config.base;
  activeGate = {
    args,
    base,
    baseLabel: base ?? "(auto)",
    tiers: args.reviewOnly || args.gen ? [] : ["fast", "exec"],
    tierIndex: 0,
    latestRuns: new Map(),
    parked: [],
    decidedFixIds: new Set(),
    reviewFindings: [],
  };
  if (args.gen) {
    await startGenRound(pi, ctx, activeGate);
    return;
  }
  await executePass(pi, ctx, activeGate);
}

/** agent_settled: continue whichever round (fix / test-author) just ran. */
async function continueGate(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  const gate = activeGate;
  if (!gate) return;
  const mode = getState().mode;
  if (mode === "fix") {
    setMode("author"); // disarm the lock before re-running tiers
    await executePass(pi, ctx, gate);
  } else if (mode === "test-author") {
    await validateGenRound(pi, ctx);
  }
}

export function registerGate(pi: ExtensionAPI): void {
  pi.registerCommand("gate", {
    description:
      "Run the gauntlet review gate (fast → exec; flags: [base] --deep --gen --bugfix --review-only --intent \"…\")",
    handler: async (args, ctx) => {
      await runGate(pi, ctx, parseGateArgs(args));
    },
  });
  pi.on("agent_settled", async (_event, ctx) => {
    await continueGate(pi, ctx);
  });
}
