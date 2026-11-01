import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { matchesKey, Key } from "@earendil-works/pi-tui";
// ponytail: tied to pinned pi-goal-x 0.32.3; reuse its parser instead of duplicating goal storage.
import { readActiveGoalPool, parseGoalFile, isSafeActivePath, resolveGoalPath } from "../../npm/node_modules/pi-goal-x/extensions/storage/goal-files.ts";
import { goalStorageRoot } from "../../npm/node_modules/pi-goal-x/extensions/storage/goal-root.ts";

export function isUsageLimit(error: string): boolean {
  return /usage[_ -]?limit|rate[_ -]?limit|too many requests|quota (?:exceeded|limit)|try again in|retry after/i.test(error)
    && !/insufficient[_ -]?quota|billing|payment|out of credits|invalid.*(?:key|token)|unauthorized|forbidden/i.test(error);
}

function activeGoal(ctx: ExtensionContext) {
  const focus = [...ctx.sessionManager.getBranch()].reverse().find(
    entry => entry.type === "custom" && entry.customType === "pi-goal-focus",
  ) as { data?: { focusedGoalId?: string; storageRoot?: string } } | undefined;
  const id = focus?.data?.focusedGoalId;
  if (!id || (focus?.data?.storageRoot && focus.data.storageRoot !== goalStorageRoot(ctx))) return;
  const saved = readActiveGoalPool(ctx).get(id);
  if (!saved || !isSafeActivePath(ctx, saved.activePath)) return;
  const goal = parseGoalFile(resolveGoalPath(ctx, ".pi/goals", saved.activePath));
  return goal?.status === "active" && goal.autoContinue ? goal : undefined;
}

export default function (pi: ExtensionAPI) {
  let enabled = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let unsubscribe: (() => void) | undefined;
  let ui: ExtensionContext["ui"] | undefined;
  const cancel = () => {
    clearTimeout(timer);
    timer = undefined;
    ui?.setStatus("goal-limit-retry", undefined);
  };
  pi.on("agent_start", cancel);
  pi.on("input", cancel);
  pi.on("session_switch", cancel);
  pi.on("session_tree", cancel);
  pi.on("session_start", (_event, ctx) => {
    cancel();
    unsubscribe?.();
    ui = ctx.ui;
    if (ctx.mode === "tui") unsubscribe = ctx.ui.onTerminalInput(data => {
      if (matchesKey(data, Key.escape)) cancel();
      return undefined;
    });
  });
  pi.on("session_shutdown", () => { cancel(); unsubscribe?.(); ui = undefined; });
  pi.on("agent_settled", (event, ctx) => {
    cancel();
    if (!enabled || event.aborted) return;
    const last = [...ctx.sessionManager.getBranch()].reverse().find(
      entry => entry.type === "message" && entry.message.role === "assistant",
    ) as { message?: { stopReason?: string; errorMessage?: string } } | undefined;
    if (last?.message?.stopReason !== "error" || !isUsageLimit(last.message.errorMessage ?? "")) return;
    const goal = activeGoal(ctx);
    if (!goal) return;
    const leaf = ctx.sessionManager.getLeafId();
    // ponytail: poll every 30 minutes; no provider-specific reset-time parser.
    timer = setTimeout(() => {
      cancel();
      try {
        if (!enabled || !ctx.isIdle() || ctx.hasPendingMessages()
          || ctx.sessionManager.getLeafId() !== leaf || activeGoal(ctx)?.id !== goal.id) return;
        pi.sendUserMessage("Retry after the provider usage-limit wait. Continue the current goal from its saved progress.");
      } catch (error) {
        ctx.ui.notify(`Goal retry stopped: ${String(error)}`, "error");
      }
    }, 30 * 60 * 1000);
    timer.unref?.();
    ctx.ui.setStatus("goal-limit-retry", "usage limit: retry at " + new Date(Date.now() + 30 * 60 * 1000).toLocaleTimeString());
    ctx.ui.notify("Usage limit: retrying this active goal in 30 minutes. Esc or /goal-retry off cancels the wait.", "warning");
  });
  pi.registerCommand("goal-retry", {
    description: "Usage-limit retry status; on/off for this session",
    handler: async (args, ctx) => {
      if (args.trim() === "off") { enabled = false; cancel(); }
      else if (args.trim() === "on") enabled = true;
      else if (args.trim() && args.trim() !== "status") { ctx.ui.notify("Usage: /goal-retry [status|on|off]", "warning"); return; }
      ctx.ui.notify(`Usage-limit retry: ${enabled ? "on" : "off"}; ${timer ? "waiting" : "not waiting"}. Interval: 30 minutes.`, "info");
    },
  });
}
