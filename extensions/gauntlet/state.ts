/**
 * Shared mode/session state for the gauntlet extension (§6.2 of the plan).
 *
 * Every mode transition goes through this module so the test-lock, the
 * widget, and the fix loop can never disagree. `dismissed` is persisted as
 * `gauntlet-dismissed` custom entries and restored on session_start.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// ---------------------------------------------------------------------------
// The finding contract (§2) as seen from TypeScript.
// ---------------------------------------------------------------------------

export type Severity = "error" | "warning" | "info";
export type FindingAction = "auto-fix" | "ask-user" | "no-op";

export interface Finding {
  id: string;
  layer: string;
  tool: string;
  severity: Severity;
  action: FindingAction;
  file: string;
  line: number;
  message: string;
  evidence: string;
  fix_hint: string;
}

export interface GauntletReport {
  version: number;
  tier: string;
  base: string;
  head: string;
  changed_files: string[];
  findings: Finding[];
  stats: { duration_s: number; runners: Record<string, string> };
}

/** Errors and warnings block unless the finding is informational (no-op). */
export function isBlocking(f: Finding): boolean {
  return f.severity !== "info" && f.action !== "no-op";
}

// ---------------------------------------------------------------------------
// Gate state singleton.
// ---------------------------------------------------------------------------

export type Mode = "author" | "fix" | "test-author";

export interface GateState {
  mode: Mode;
  round: number; // current fix round, 0 = not in a loop
  maxRounds: number; // default 3
  dismissed: Set<string>; // finding ids the user chose to ignore
  lastReport?: GauntletReport;
}

const state: GateState = {
  mode: "author",
  round: 0,
  maxRounds: 3,
  dismissed: new Set(),
  lastReport: undefined,
};

let api: ExtensionAPI | undefined;

/** Bind the singleton to the extension API (call once from the factory). */
export function initState(pi: ExtensionAPI): void {
  api = pi;
}

export function getState(): Readonly<GateState> {
  return state;
}

/** Set the mode; emits `gauntlet:mode` on the extension event bus. */
export function setMode(mode: Mode): void {
  state.mode = mode;
  api?.events.emit("gauntlet:mode", { mode, round: state.round });
}

export function setRound(round: number, maxRounds?: number): void {
  state.round = round;
  if (maxRounds !== undefined) state.maxRounds = maxRounds;
  api?.events.emit("gauntlet:mode", { mode: state.mode, round: state.round });
}

export function setLastReport(report: GauntletReport): void {
  state.lastReport = report;
}

/** Dismiss a finding id (Layer 7 approval) and persist the full set. */
export function dismiss(id: string): void {
  state.dismissed.add(id);
  api?.appendEntry("gauntlet-dismissed", { ids: [...state.dismissed] });
}

/** Restore `dismissed` from the session branch (union of all entries). */
export function registerStateRestoration(pi: ExtensionAPI): void {
  pi.on("session_start", async (_event, ctx: ExtensionContext) => {
    state.mode = "author";
    state.round = 0;
    state.dismissed = new Set();
    for (const entry of ctx.sessionManager.getBranch()) {
      if (entry.type === "custom" && entry.customType === "gauntlet-dismissed") {
        const data = entry.data as { ids?: string[] } | undefined;
        for (const id of data?.ids ?? []) state.dismissed.add(id);
      }
    }
  });
}

// ---------------------------------------------------------------------------
// Minimal .gauntlet.toml reader (the tiny subset the extension needs).
//
// The extension never re-implements a *check* — the CLI owns those — but the
// test-lock must know `test_paths`/`src_paths` (§7.1) and the fix loop needs
// `fix.max_rounds` before any CLI run has happened.
// ---------------------------------------------------------------------------

export interface GauntletConfig {
  base?: string;
  testPaths: string[];
  srcPaths: string[];
  maxRounds: number;
  reviewProvider?: string;
  reviewModel?: string;
}

export const DEFAULT_TEST_PATHS = ["tests", "test", "conftest.py"];
export const DEFAULT_SRC_PATHS = ["src"];

function parseTomlValue(raw: string): string | number | boolean | string[] | undefined {
  const value = raw.trim();
  if (value.startsWith("[")) {
    const items = value.replace(/^\[|\]$/g, "").trim();
    if (!items) return [];
    return items
      .split(",")
      .map((item) => item.trim().replace(/^["']|["']$/g, ""))
      .filter((item) => item.length > 0);
  }
  if (/^["']/.test(value)) return value.replace(/^["']|["']$/g, "");
  if (value === "true") return true;
  if (value === "false") return false;
  const num = Number(value);
  return Number.isNaN(num) ? undefined : num;
}

/** Read the small .gauntlet.toml subset the extension itself needs. */
export function readGauntletConfig(cwd: string): GauntletConfig {
  const config: GauntletConfig = {
    testPaths: [...DEFAULT_TEST_PATHS],
    srcPaths: [...DEFAULT_SRC_PATHS],
    maxRounds: 3,
  };
  const path = join(cwd, ".gauntlet.toml");
  if (!existsSync(path)) return config;
  let section = "";
  for (const rawLine of readFileSync(path, "utf-8").split("\n")) {
    const line = rawLine.replace(/#(?=(?:[^"]*"[^"]*")*[^"]*$).*$/, "").trim();
    if (!line) continue;
    const sectionMatch = line.match(/^\[(.+)\]$/);
    if (sectionMatch) {
      section = sectionMatch[1].trim();
      continue;
    }
    const kv = line.match(/^([A-Za-z0-9_-]+)\s*=\s*(.+)$/);
    if (!kv) continue;
    const key = kv[1];
    const value = parseTomlValue(kv[2]);
    if (value === undefined) continue;
    if (section === "" && key === "base" && typeof value === "string") {
      config.base = value;
    } else if (section === "" && key === "test_paths" && Array.isArray(value)) {
      config.testPaths = value;
    } else if (section === "" && key === "src_paths" && Array.isArray(value)) {
      config.srcPaths = value;
    } else if (section === "fix" && key === "max_rounds" && typeof value === "number") {
      config.maxRounds = value;
    } else if (section === "review" && key === "provider" && typeof value === "string") {
      config.reviewProvider = value;
    } else if (section === "review" && key === "model" && typeof value === "string") {
      config.reviewModel = value;
    }
  }
  return config;
}

/** Resolve the gauntlet binary: target repo venv first, then PATH. */
export function gauntletBin(cwd: string): string {
  for (const candidate of [
    join(cwd, ".venv", "bin", "gauntlet"),
    join(cwd, "venv", "bin", "gauntlet"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return "gauntlet";
}
