// Run: node ~/.pi/agent/extensions/goal-limit-retry/check.mjs
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadExtensions } from "../../install/releases/1.1.0/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";

const cwd = mkdtempSync(join(tmpdir(), "pi-goal-retry-check-"));
const file = join(cwd, ".pi/goals/active_goal_check.md");
mkdirSync(join(cwd, ".pi/goals"), { recursive: true });
const saveGoal = status => writeFileSync(file, JSON.stringify({
  id: "check", objective: "Test retry", status, autoContinue: true,
  activePath: ".pi/goals/active_goal_check.md",
}));
saveGoal("active");
const result = await loadExtensions([new URL("index.ts", import.meta.url).pathname], cwd);
assert.deepEqual(result.errors, []);
const ext = result.extensions[0];
let sent = 0, leaf = "one", pending, terminalInput;
result.runtime.sendUserMessage = () => { sent++; };
const branch = [
  { type: "custom", customType: "pi-goal-focus", data: { focusedGoalId: "check" } },
  { type: "message", message: { role: "assistant", stopReason: "error", errorMessage: "You have hit your ChatGPT usage limit. Try again in 5 minutes." } },
];
const ctx = {
  cwd, mode: "tui", isIdle: () => true, hasPendingMessages: () => false,
  sessionManager: { getBranch: () => branch, getLeafId: () => leaf, getSessionId: () => "check" },
  ui: { setStatus() {}, notify() {}, onTerminalInput(handler) { terminalInput = handler; return () => {}; } },
};
const emit = async (event, data = {}) => {
  for (const handler of ext.handlers.get(event) ?? []) await handler(data, ctx);
};
const realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout;
globalThis.setTimeout = (fn, ms) => {
  assert.equal(ms, 1800000);
  pending = fn;
  return { unref() {} };
};
globalThis.clearTimeout = () => { pending = undefined; };
try {
  await emit("session_start");
  await emit("agent_settled");
  assert.ok(pending);
  pending();
  assert.equal(sent, 1);
  await emit("agent_settled");
  assert.ok(pending, "Another limit failure must schedule another retry");
  leaf = "changed";
  pending();
  assert.equal(sent, 1, "Do not retry a changed branch");
  await emit("agent_settled");
  await emit("input");
  assert.equal(pending, undefined);
  await emit("agent_settled");
  terminalInput("\x1b");
  assert.equal(pending, undefined, "Escape cancels the wait");
  await emit("agent_settled");
  await ext.commands.get("goal-retry").handler("off", ctx);
  assert.equal(pending, undefined);
  await emit("agent_settled");
  assert.equal(pending, undefined);
  await ext.commands.get("goal-retry").handler("on", ctx);
  for (const error of ["billing payment required", "insufficient_quota", "invalid API key", "context too long"]) {
    branch[1].message.errorMessage = error;
    await emit("agent_settled");
    assert.equal(pending, undefined, error);
  }
  branch[1].message.errorMessage = "usage_limit_reached";
  await emit("agent_settled");
  assert.ok(pending);
  saveGoal("paused");
  pending();
  assert.equal(sent, 1, "Never wake a paused goal");
  await emit("session_shutdown");
  assert.equal(pending, undefined);
  console.log("PASS: extension loads; 30-minute retries, repeated limits, cancellation, stale branches, pause, and billing exclusions.");
} finally {
  globalThis.setTimeout = realSet;
  globalThis.clearTimeout = realClear;
  rmSync(cwd, { recursive: true, force: true });
}
