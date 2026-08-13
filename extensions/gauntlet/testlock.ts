/**
 * Mechanical test-lock (§7.1 of the plan).
 *
 * During fix rounds the fixing agent is *blocked* — not merely instructed —
 * from editing tests, skipping them selectively, or reverting them via git.
 * The regexes are a tripwire against a lazy/reward-hacking in-harness agent,
 * not a security boundary (see README).
 *
 * TEST_RE is derived from `.gauntlet.toml` `test_paths` at session_start;
 * the literal below is the fallback.
 */
import { isToolCallEventType, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { relative } from "node:path";
import { getState, readGauntletConfig, type Mode } from "./state";

export const DEFAULT_TEST_RE =
  /(^|\/)(tests?|conftest\.py|test_[^/]+\.py|[^/]+_test\.py)(\/|$)?/;

export const PYTEST_EVASION =
  /pytest[^\n]*(\s-k\s|--deselect|--ignore|-p\s+no:)|@pytest\.mark\.skip|unittest\.SkipTest/;

export const GIT_TAMPER =
  /git\s+(checkout|restore|stash)[^\n]*\stests?\b|rm\s+(-\w+\s+)*[^\n]*test/;

const SHELL_WRITE = /(>>?|tee\s|sed\s+-i|>\s*tests?\/)/;

export const DEFAULT_SRC_RE = /(^|\/|\s)src(\/|\s|$)/;

let testRe: RegExp = DEFAULT_TEST_RE;
let srcRe: RegExp = DEFAULT_SRC_RE;

function escapeRegex(raw: string): string {
  return raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Build the test-path regex from configured test_paths (§7.1 note). */
export function buildTestRe(testPaths: string[]): RegExp {
  const alternatives = testPaths
    .map((p) => escapeRegex(p.replace(/\/+$/, "")))
    .filter((p) => p.length > 0);
  alternatives.push("test_[^/]+\\.py", "[^/]+_test\\.py");
  return new RegExp(`(^|\\/)(${alternatives.join("|")})(\\/|$)?`);
}

/** Build the src-path regex from configured src_paths (§9.2 inverse lock). */
export function buildSrcRe(srcPaths: string[]): RegExp {
  const alternatives = srcPaths
    .map((p) => escapeRegex(p.replace(/\/+$/, "")))
    .filter((p) => p.length > 0);
  if (alternatives.length === 0) return DEFAULT_SRC_RE;
  return new RegExp(`(^|\\/|\\s)(${alternatives.join("|")})(\\/|\\s|$)`);
}

/** Normalize a tool path: strip a leading @, make repo-relative. */
export function normalizePath(path: string, cwd: string): string {
  let p = path.startsWith("@") ? path.slice(1) : path;
  if (p.startsWith("/")) p = relative(cwd, p);
  return p;
}

export interface LockDecision {
  block: true;
  reason: string;
}

/**
 * Pure decision core: given the mode and a tool call, should it be blocked?
 * Returns undefined to allow.
 *
 * - mode "fix": test paths are read-only (Layer 4/5 tamper defense).
 * - mode "test-author": the inverse — writes are allowed *only* under test
 *   paths, and shell/git writes into src paths are blocked (§9.2.1).
 */
export function evaluateLock(
  mode: Mode,
  tool: "write" | "edit" | "bash",
  payload: string,
  re: RegExp = testRe,
  srcPattern: RegExp = srcRe,
): LockDecision | undefined {
  if (mode === "fix") {
    if (tool === "write" || tool === "edit") {
      if (re.test(payload)) {
        return {
          block: true,
          reason:
            "Fix rounds are code-only: test files are read-only. " +
            "Make the failing test pass by fixing src/.",
        };
      }
      return undefined;
    }
    // bash
    if (PYTEST_EVASION.test(payload)) {
      return {
        block: true,
        reason: "Selective test skipping is not allowed in fix rounds.",
      };
    }
    if (GIT_TAMPER.test(payload)) {
      return {
        block: true,
        reason: "Reverting or deleting tests is not allowed in fix rounds.",
      };
    }
    if (re.test(payload) && SHELL_WRITE.test(payload)) {
      return {
        block: true,
        reason: "Shell writes into test paths are blocked in fix rounds.",
      };
    }
    return undefined;
  }
  if (mode === "test-author") {
    if (tool === "write" || tool === "edit") {
      if (!re.test(payload)) {
        return {
          block: true,
          reason:
            "Test-author rounds may only write tests: src/ is read-only. " +
            "Put new tests under the configured test paths.",
        };
      }
      return undefined;
    }
    // bash: block shell/git writes into src paths.
    if (SHELL_WRITE.test(payload) && srcPattern.test(payload)) {
      return {
        block: true,
        reason: "Shell writes into src paths are blocked in test-author rounds.",
      };
    }
    if (/git\s+(checkout|restore|stash)/.test(payload) && srcPattern.test(payload)) {
      return {
        block: true,
        reason: "Reverting src is not allowed in test-author rounds.",
      };
    }
    return undefined;
  }
  return undefined;
}

export function registerTestLock(pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    const config = readGauntletConfig(ctx.cwd);
    testRe = buildTestRe(config.testPaths);
    srcRe = buildSrcRe(config.srcPaths);
  });

  pi.on("tool_call", async (event, ctx) => {
    const mode = getState().mode;
    if (mode === "author") return;
    if (isToolCallEventType("write", event)) {
      return evaluateLock(mode, "write", normalizePath(event.input.path, ctx.cwd));
    }
    if (isToolCallEventType("edit", event)) {
      return evaluateLock(mode, "edit", normalizePath(event.input.path, ctx.cwd));
    }
    if (isToolCallEventType("bash", event)) {
      return evaluateLock(mode, "bash", event.input.command ?? "");
    }
    return;
  });
}
