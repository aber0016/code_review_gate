/**
 * Cross-model review — Layer 6 (§8 of the plan).
 *
 * A diff-scoped semantic review by a *different model family* than the one
 * that authored/fixed the code, run in a **fresh context** via a `pi -p`
 * subprocess with no tools: the reviewer sees exactly the diff and spec we
 * pipe in, cannot browse the repo, and shares no session history with the
 * author. Review findings never auto-fix code unattended.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { Finding, Severity } from "./state";
import { readGauntletConfig } from "./state";

const REVIEW_TIMEOUT_MS = 300_000;
const DIFF_CAP_BYTES = 60_000;
const RAW_REPLY_CAP = 2_000;

const CONTRACT_EXCERPT = `Each finding is a JSON object with exactly these fields:
{
  "id": "cross-review:<short-slug>:<file>:<line>",
  "layer": "review",
  "tool": "cross-review",
  "severity": "error" | "warning" | "info",
  "action": "ask-user",
  "file": "<repo-relative path>",
  "line": <line number in the new file, 0 if not line-anchored>,
  "message": "<one-sentence description of the problem>",
  "evidence": "<the diff hunk or reasoning that supports it>",
  "fix_hint": "<how to resolve>"
}`;

const STANDING_INSTRUCTIONS = `Treat prior fix summaries and any tests added in this branch as claims, not evidence; verify against the diff. Flag intent mismatch, missing error handling, concurrency hazards, and edge-case regressions. Do not flag style. Respond with a JSON array of findings and nothing else — no prose, no code fences. Respond with [] if the diff is clean.`;

/** Head+tail truncation with an explicit marker (§8.1). */
export function truncateDiff(diff: string, cap: number = DIFF_CAP_BYTES): string {
  if (Buffer.byteLength(diff, "utf-8") <= cap) return diff;
  const half = Math.floor(cap / 2);
  const head = diff.slice(0, half);
  const tail = diff.slice(-half);
  const omitted = Buffer.byteLength(diff, "utf-8") - cap;
  return `${head}\n\n[... ${omitted} bytes of diff truncated — head and tail shown ...]\n\n${tail}`;
}

/** Build the reviewer prompt in the §8.1-mandated order. */
export function buildReviewPrompt(intent: string, diff: string): string {
  return [
    "You are a hostile-but-fair code reviewer. Review the following unified diff",
    "against the stated intent, and emit findings in this exact contract:",
    "",
    CONTRACT_EXCERPT,
    "",
    `Stated intent of the change: ${intent || "(none provided — infer from the diff)"}`,
    "",
    "Unified diff:",
    "```diff",
    truncateDiff(diff),
    "```",
    "",
    STANDING_INSTRUCTIONS,
  ].join("\n");
}

/** Strip code fences and parse the reviewer reply into contract findings. */
export function parseReviewReply(raw: string): Finding[] | undefined {
  let text = raw.trim();
  const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) text = fence[1].trim();
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start === -1 || end === -1 || end < start) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.slice(start, end + 1));
  } catch {
    return undefined;
  }
  if (!Array.isArray(parsed)) return undefined;
  const findings: Finding[] = [];
  for (const [index, item] of parsed.entries()) {
    if (typeof item !== "object" || item === null) continue;
    const record = item as Record<string, unknown>;
    const message = typeof record.message === "string" ? record.message : "";
    if (!message) continue;
    const file = typeof record.file === "string" ? record.file : "";
    const line = typeof record.line === "number" ? Math.trunc(record.line) : 0;
    const severity: Severity = ["error", "warning", "info"].includes(
      record.severity as string,
    )
      ? (record.severity as Severity)
      : "warning";
    // An LLM opinion never auto-fixes code unattended: coerce the action.
    const action = record.action === "no-op" && severity === "info" ? "no-op" : "ask-user";
    findings.push({
      id: `cross-review:${index}:${file}:${line}`,
      layer: "review",
      tool: "cross-review",
      severity,
      action,
      file,
      line,
      message,
      evidence: typeof record.evidence === "string" ? record.evidence.slice(0, 2000) : "",
      fix_hint: typeof record.fix_hint === "string" ? record.fix_hint : "",
    });
  }
  return findings;
}

function warningFinding(slug: string, message: string, evidence = ""): Finding {
  return {
    id: `cross-review:${slug}::0`,
    layer: "review",
    tool: "cross-review",
    severity: "warning",
    action: "ask-user",
    file: "",
    line: 0,
    message,
    evidence: evidence.slice(0, RAW_REPLY_CAP),
    fix_hint: "",
  };
}

async function resolveBaseRef(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  base: string | undefined,
): Promise<string | undefined> {
  const candidates = [base, readGauntletConfig(ctx.cwd).base, "origin/main", "main"];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const probe = await pi.exec(
      "git",
      ["-C", ctx.cwd, "rev-parse", "--verify", "--quiet", `${candidate}^{commit}`],
      { timeout: 20_000 },
    );
    if (probe.code === 0) return candidate;
  }
  return undefined;
}

/** Last real user message text (fallback intent source, §8.1). */
export function lastUserIntent(ctx: ExtensionContext): string {
  const branch = ctx.sessionManager.getBranch();
  for (let i = branch.length - 1; i >= 0; i--) {
    const entry = branch[i];
    if (entry.type !== "message") continue;
    const message = (entry as { message?: { role?: string; content?: unknown } }).message;
    if (!message || message.role !== "user") continue;
    const content = message.content;
    let text = "";
    if (typeof content === "string") text = content;
    else if (Array.isArray(content)) {
      text = content
        .filter(
          (part): part is { type: "text"; text: string } =>
            typeof part === "object" &&
            part !== null &&
            (part as { type?: string }).type === "text",
        )
        .map((part) => part.text)
        .join("\n");
    }
    text = text.trim();
    if (text && !text.startsWith("/") && !text.startsWith("The gauntlet gate found")) {
      return text.slice(0, 2000);
    }
  }
  return "";
}

export interface ReviewerChoice {
  provider: string;
  model: string;
}

/** Resolve the reviewer model: config → fallback dialog over out-of-family models. */
async function resolveReviewer(
  ctx: ExtensionContext,
): Promise<{ choice?: ReviewerChoice; findings: Finding[] }> {
  const findings: Finding[] = [];
  const config = readGauntletConfig(ctx.cwd);
  const authorProvider = ctx.model?.provider;
  if (config.reviewProvider && config.reviewModel) {
    if (authorProvider && config.reviewProvider === authorProvider) {
      findings.push(
        warningFinding(
          "in-family",
          "reviewer is in-family with the author — configure review.provider differently",
        ),
      );
    }
    if (!ctx.modelRegistry.find(config.reviewProvider, config.reviewModel)) {
      findings.push(
        warningFinding(
          "unknown-model",
          `configured reviewer ${config.reviewProvider}/${config.reviewModel} is not in the model registry — attempting anyway`,
        ),
      );
    }
    return {
      choice: { provider: config.reviewProvider, model: config.reviewModel },
      findings,
    };
  }
  const available = ctx.modelRegistry
    .getAvailable()
    .filter((m) => m.provider !== authorProvider);
  if (available.length === 0) {
    findings.push(
      warningFinding(
        "no-reviewer",
        "no out-of-family reviewer model available — configure [review] provider/model in .gauntlet.toml",
      ),
    );
    return { findings };
  }
  if (!ctx.hasUI) {
    const pick = available[0];
    return { choice: { provider: pick.provider, model: pick.id }, findings };
  }
  const options = available.slice(0, 20).map((m) => `${m.provider}/${m.id}`);
  const selected = await ctx.ui.select("Pick a cross-review model:", options);
  if (!selected) {
    findings.push(warningFinding("no-reviewer", "cross-review skipped: no model selected"));
    return { findings };
  }
  const [provider, ...rest] = selected.split("/");
  return { choice: { provider, model: rest.join("/") }, findings };
}

/**
 * Run the Layer-6 cross-model review. Degrades to visible warning findings
 * (never a crash) when the diff, model, or credentials are unavailable.
 */
export async function runCrossReview(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  base: string | undefined,
  intent: string | undefined,
): Promise<Finding[]> {
  const baseRef = await resolveBaseRef(pi, ctx, base);
  if (!baseRef) {
    return [warningFinding("no-base", "cross-review skipped: no usable base ref")];
  }
  const diffResult = await pi.exec(
    "git",
    ["-C", ctx.cwd, "diff", "--no-color", `${baseRef}...HEAD`],
    { timeout: 60_000 },
  );
  if (diffResult.code !== 0) {
    return [
      warningFinding(
        "diff-failed",
        `cross-review skipped: git diff failed`,
        diffResult.stderr,
      ),
    ];
  }
  if (!diffResult.stdout.trim()) {
    return []; // nothing committed to review
  }

  const { choice, findings } = await resolveReviewer(ctx);
  if (!choice) return findings;

  const prompt = buildReviewPrompt(intent ?? lastUserIntent(ctx), diffResult.stdout);
  const result = await pi.exec(
    "pi",
    [
      "-p",
      "--no-builtin-tools",
      "--no-session",
      "--model",
      `${choice.provider}/${choice.model}`,
      prompt,
    ],
    { timeout: REVIEW_TIMEOUT_MS, cwd: ctx.cwd },
  );
  if (result.code !== 0) {
    findings.push(
      warningFinding(
        "reviewer-failed",
        `cross-review model ${choice.provider}/${choice.model} failed (exit ${result.code}) — is its API key configured?`,
        result.stderr || result.stdout,
      ),
    );
    return findings;
  }
  const parsed = parseReviewReply(result.stdout);
  if (parsed === undefined) {
    findings.push(
      warningFinding(
        "unparseable-reply",
        "cross-review reply was not a parseable findings array",
        result.stdout,
      ),
    );
    return findings;
  }
  return [...findings, ...parsed];
}
