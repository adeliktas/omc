import {
  AlertOctagon,
  CalendarClock,
  Coins,
  Hourglass,
  Layers,
  Lock,
  Repeat,
  Timer,
  UserCircle,
  Settings,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/utils";

// T103 v15 (2026-05-19): aligned with backend Literal incl blocked + cancelled.
type TaskStatus =
  | "inbox"
  | "in_progress"
  | "review"
  | "done"
  | "blocked"
  | "cancelled"
  | "paused"
  | "waiting";

interface TaskCardProps {
  title: string;
  status?: TaskStatus;
  priority?: string;
  assignee?: string;
  due?: string;
  isOverdue?: boolean;
  approvalsPendingCount?: number;
  tags?: Array<{ id: string; name: string; color: string }>;
  isBlocked?: boolean;
  blockedByCount?: number;
  autoCreated?: boolean;
  createdByName?: string | null;
  onClick?: () => void;
  /** Opens the task settings dialog. Rendered as a gear on hover;
   *  omit to hide it (e.g. read-only views). */
  onOpenSettings?: () => void;
  draggable?: boolean;
  isDragging?: boolean;
  onDragStart?: (event: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd?: (event: React.DragEvent<HTMLDivElement>) => void;
  /** Recurrence interval in minutes — renders clock badge if set. */
  repeatIntervalMinutes?: number | null;
  /** ISO task creation timestamp for live elapsed timer. */
  createdAt?: string | null;
  /** Task ID for unique timer hook key. */
  taskId?: string;
  /** Aggregated per-stage durations for compact timeline bar — CURRENT
   *  cycle only (T68). For one-shot tasks this equals lifetime. */
  stageDurations?: Array<{ status: string; totalSeconds: number }> | null;
  /** Lifetime per-stage aggregation, shown when the user expands the bar. */
  stageDurationsLifetime?: Array<{
    status: string;
    totalSeconds: number;
  }> | null;
  /** DAG split metadata (E1 2026-05-14): if `splitType === "dag"`, render
   *  a "DAG: X/Y" badge showing how many children are done out of `splitCount`.
   *  `dagDoneCount` should be precomputed by the board snapshot if available;
   *  if omitted, badge shows "DAG: ?/Y". */
  splitType?: string | null;
  splitCount?: number;
  dagDoneCount?: number;
  // T62 (2026-05-15): surface every watchdog-relevant value on the card.
  /** ITIL-light complexity 1..5 → renders L1..L5 badge near priority. */
  complexity?: number;
  /** Wall-clock budget cap in seconds — renders Hourglass badge. */
  maxSecondsBudget?: number;
  /** Token budget cap — renders progress bar with tokens_used / max. */
  maxTokensBudget?: number;
  /** Rolling token counter from traffic ingest. */
  tokensUsed?: number;
  /** ISO timestamp when watchdog last flagged budget exhausted. */
  budgetExhaustedAt?: string | null;
  /** Parent task UUID — if set, this is a DAG/split child. */
  parentTaskId?: string | null;
  /** Position within parent DAG (1-indexed). */
  splitPosition?: number;
  /** PM agent currently holding the orchestration lock. */
  pmLockAgentId?: string | null;
}

export function TaskCard({
  title,
  status,
  priority,
  assignee,
  due,
  isOverdue = false,
  approvalsPendingCount = 0,
  tags = [],
  isBlocked = false,
  blockedByCount = 0,
  autoCreated = false,
  createdByName,
  onClick,
  onOpenSettings,
  draggable = false,
  isDragging = false,
  onDragStart,
  onDragEnd,
  repeatIntervalMinutes,
  createdAt,
  taskId,
  stageDurations,
  stageDurationsLifetime,
  splitType,
  splitCount,
  dagDoneCount,
  complexity,
  maxSecondsBudget,
  maxTokensBudget,
  tokensUsed,
  budgetExhaustedAt,
  parentTaskId,
  splitPosition,
  pmLockAgentId,
}: TaskCardProps) {
  // ── Live elapsed timer (ticker re-renders, not full card) ──
  const [elapsed, setElapsed] = useState(0);
  // Finished work has no running clock ("Poem scraper 1188h elapsed").
  const live = status !== "done" && status !== "cancelled" && status !== "paused";
  useEffect(() => {
    if (!createdAt || !live) return;
    const tick = () => setElapsed(
      Math.floor((Date.now() - new Date(createdAt).getTime()) / 1000),
    );
    tick();
    const id = setInterval(tick, 1_000);
    return () => clearInterval(id);
  }, [createdAt, taskId, live]);

  const elapsedLabel = useMemo(() => {
    if (!createdAt || !elapsed || !live) return null;
    const m = Math.floor(elapsed / 60);
    const s = elapsed % 60;
    if (m >= 60) {
      const h = Math.floor(m / 60);
      return `${h}h ${m % 60}m`;
    }
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }, [elapsed, createdAt, live]);

  const createdDate = useMemo(() => {
    if (!createdAt) return null;
    const d = new Date(createdAt);
    return d.toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }, [createdAt]);

  const hasPendingApproval = approvalsPendingCount > 0;
  const needsLeadReview =
    status === "review" && !isBlocked && !hasPendingApproval;
  const leftBarClassName = isBlocked
    ? "bg-rose-400"
    : hasPendingApproval
      ? "bg-amber-400"
      : needsLeadReview
        ? "bg-indigo-400"
        : null;
  const priorityBadge = (value?: string) => {
    if (!value) return null;
    const normalized = value.toLowerCase();
    if (normalized === "high") {
      return "bg-rose-100 text-rose-700";
    }
    if (normalized === "medium") {
      return "bg-amber-100 text-amber-700";
    }
    if (normalized === "low") {
      return "bg-emerald-100 text-emerald-700";
    }
    return "bg-slate-100 text-slate-600";
  };

  const priorityLabel = priority ? priority.toUpperCase() : "MEDIUM";
  const visibleTags = tags.slice(0, 3);

  // ── T62 complexity badge (L1..L5) ───────────────────────────────────
  const complexityClasses = (level?: number): string => {
    if (!level) return "";
    if (level <= 1) return "bg-emerald-100 text-emerald-700 border-emerald-200";
    if (level === 2) return "bg-sky-100 text-sky-700 border-sky-200";
    if (level === 3) return "bg-amber-100 text-amber-700 border-amber-200";
    if (level === 4) return "bg-orange-100 text-orange-700 border-orange-200";
    return "bg-rose-100 text-rose-700 border-rose-200"; // L5
  };
  const complexityTitle = (level?: number): string => {
    if (!level) return "Complexity not assessed";
    const labels: Record<number, string> = {
      1: "L1 — trivial / one-shot (bypasses review, LAN-local worker)",
      2: "L2 — simple (LAN-local worker, lead review)",
      3: "L3 — moderate (LAN-local worker, full review pipeline)",
      4: "L4 — complex (cloud-tier worker per agent's fallback chain, auto-split)",
      5: "L5 — major (cloud-tier worker, mandatory DAG split + PM oversight)",
    };
    return labels[level] || `L${level}`;
  };

  // ── T62 budget helpers ──────────────────────────────────────────────
  const tokenPct = useMemo(() => {
    if (!maxTokensBudget || maxTokensBudget <= 0) return 0;
    return Math.min(100, ((tokensUsed ?? 0) / maxTokensBudget) * 100);
  }, [tokensUsed, maxTokensBudget]);

  const formatTokens = (n?: number): string => {
    if (n == null) return "0";
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
    return n.toString();
  };

  const formatSeconds = (s?: number): string => {
    if (!s) return "";
    if (s >= 3600) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
    if (s >= 60) return `${Math.floor(s / 60)}m`;
    return `${s}s`;
  };

  return (
    <div
      className={cn(
        "group relative cursor-pointer rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition-all hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-md",
        isDragging && "opacity-60 shadow-none",
        hasPendingApproval && "border-amber-200 bg-amber-50/40",
        isBlocked && "border-rose-200 bg-rose-50/50",
        needsLeadReview && "border-indigo-200 bg-indigo-50/30",
      )}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick?.();
        }
      }}
    >
      {leftBarClassName ? (
        <span
          className={cn(
            "absolute left-0 top-0 h-full w-1 rounded-l-lg",
            leftBarClassName,
          )}
        />
      ) : null}
      <div className="flex items-start justify-between gap-3">
        {onOpenSettings && (
          <button
            type="button"
            aria-label="Task settings"
            title="Task settings"
            onClick={(event) => {
              // The card itself opens the detail drawer; the gear must not.
              event.stopPropagation();
              onOpenSettings();
            }}
            className="absolute right-1.5 top-1.5 rounded p-1 text-slate-300 opacity-0
                       transition hover:bg-slate-100 hover:text-slate-600
                       focus:opacity-100 focus:outline-none focus:ring-1
                       focus:ring-slate-300 group-hover:opacity-100"
          >
            <Settings className="h-3.5 w-3.5" />
          </button>
        )}
        <div className="min-w-0 space-y-2">
          <p className="text-sm font-medium text-slate-900 line-clamp-2 break-words">
            {title}
          </p>
          {autoCreated && createdByName ? (
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
              <span className="h-1.5 w-1.5 rounded-full bg-slate-400" />
              {createdByName}
            </div>
          ) : null}
          {/* ── Recurrence badge ── */}
          {repeatIntervalMinutes ? (
            <div className="flex items-center gap-1.5 text-[10px] font-semibold text-violet-600">
              <Repeat className="h-3 w-3" />
              Every {repeatIntervalMinutes}min
            </div>
          ) : null}
          {/* ── T62/T63: budget displays. Hidden on repeating tasks —
              cycle resets erase tokens_used + budget_exhausted_at each
              interval, so per-task budgets are conceptually meaningless
              (the cycle window itself is the budget). One-shot tasks
              still see the wall-clock + token displays. */}
          {!repeatIntervalMinutes && maxSecondsBudget ? (
            <div
              className="flex items-center gap-1.5 text-[10px] font-semibold text-amber-700"
              title={`Watchdog auto-fails this task if in_progress exceeds ${maxSecondsBudget}s`}
            >
              <Hourglass className="h-3 w-3" />
              Budget: {formatSeconds(maxSecondsBudget)} wall-clock
            </div>
          ) : null}
          {!repeatIntervalMinutes && (maxTokensBudget || (tokensUsed ?? 0) > 0) ? (
            <div
              className="space-y-0.5"
              title={
                maxTokensBudget
                  ? `Tokens used / max: ${tokensUsed ?? 0} / ${maxTokensBudget}`
                  : `Tokens used so far: ${tokensUsed ?? 0}`
              }
            >
              <div className="flex items-center gap-1.5 text-[10px] font-semibold text-slate-600">
                <Coins className="h-3 w-3" />
                {formatTokens(tokensUsed)}
                {maxTokensBudget ? ` / ${formatTokens(maxTokensBudget)}` : " tokens"}
                {maxTokensBudget && tokenPct > 80 ? (
                  <span className="text-rose-600">· {Math.floor(tokenPct)}%</span>
                ) : null}
              </div>
              {maxTokensBudget ? (
                <div className="h-1 w-full rounded-full bg-slate-100 overflow-hidden">
                  <div
                    className={cn(
                      "h-full transition-all",
                      tokenPct > 90
                        ? "bg-rose-500"
                        : tokenPct > 70
                          ? "bg-amber-500"
                          : "bg-emerald-500",
                    )}
                    style={{ width: `${Math.max(2, tokenPct)}%` }}
                  />
                </div>
              ) : null}
            </div>
          ) : null}
          {/* ── DAG progress badge (E1 2026-05-14) ── */}
          {splitType === "dag" && splitCount && splitCount > 0 ? (
            <div
              className="flex items-center gap-1.5 text-[10px] font-semibold text-sky-700"
              title={`Subtask DAG: ${dagDoneCount ?? "?"} of ${splitCount} children done. Parent auto-promotes to review when all children complete.`}
            >
              <span className="rounded bg-sky-100 px-1.5 py-0.5 font-mono">
                DAG: {dagDoneCount ?? "?"}/{splitCount}
              </span>
            </div>
          ) : null}
          {/* ── Live elapsed timer ── */}
          {createdAt && elapsedLabel ? (
            <div className="flex items-center gap-1.5 text-[10px] font-semibold text-slate-500">
              <Timer className="h-3 w-3" />
              {elapsedLabel} elapsed
              {createdDate ? (
                <span className="text-slate-400 ml-1">· {createdDate}</span>
              ) : null}
            </div>
          ) : null}
          {/* ── Current/last cycle duration (T68) ──
              For repeating tasks: latest cycle only (current if still in
              progress, last completed cycle if currently in done/inbox).
              For one-shot tasks: lifetime total. */}
          {stageDurations && stageDurations.length > 0 ? (
            <StageDurationBlock
              durations={stageDurations}
              lifetime={stageDurationsLifetime ?? null}
              isRepeat={!!repeatIntervalMinutes}
              currentStatus={status}
            />
          ) : null}
          {isBlocked ? (
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-rose-700">
              <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
              Blocked{blockedByCount > 0 ? ` · ${blockedByCount}` : ""}
            </div>
          ) : null}
          {hasPendingApproval ? (
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
              Approval needed · {approvalsPendingCount}
            </div>
          ) : null}
          {needsLeadReview ? (
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-indigo-700">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
              Queued for automatic review
            </div>
          ) : null}
          {visibleTags.length ? (
            <div className="flex flex-wrap items-center gap-1.5">
              {visibleTags.map((tag) => (
                <span
                  key={tag.id}
                  className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-700"
                >
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: `#${tag.color}` }}
                  />
                  {tag.name}
                </span>
              ))}
              {tags.length > visibleTags.length ? (
                <span className="text-[10px] font-semibold text-slate-500">
                  +{tags.length - visibleTags.length}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="flex flex-shrink-0 flex-col items-end gap-1.5">
          <span
            className={cn(
              "inline-flex items-center rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-wide",
              priorityBadge(priority) ?? "bg-slate-100 text-slate-600",
            )}
          >
            {priorityLabel}
          </span>
          {/* T62: complexity L1..L5 — drives routing (vLLM vs ollama),
              auto-split threshold (L4+), review bypass (L1). */}
          {complexity ? (
            <span
              title={complexityTitle(complexity)}
              className={cn(
                "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-mono font-bold tracking-wider",
                complexityClasses(complexity),
              )}
            >
              L{complexity}
            </span>
          ) : (
            <span
              title="Complexity not yet assessed by PM"
              className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-mono text-slate-400"
            >
              L?
            </span>
          )}
          {/* T62: PM orchestration lock — only one PM at a time. */}
          {pmLockAgentId ? (
            <span
              title={`PM lock held by agent ${pmLockAgentId.slice(0, 8)}`}
              className="inline-flex items-center gap-0.5 rounded-full bg-violet-100 text-violet-700 border border-violet-200 px-1.5 py-0.5 text-[10px] font-semibold"
            >
              <Lock className="h-2.5 w-2.5" />
              PM
            </span>
          ) : null}
          {/* T62: subtask of a parent DAG. */}
          {parentTaskId ? (
            <span
              title={`Subtask of parent ${parentTaskId.slice(0, 8)}${splitPosition ? ` (#${splitPosition})` : ""}`}
              className="inline-flex items-center gap-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200 px-1.5 py-0.5 text-[10px] font-semibold"
            >
              <Layers className="h-2.5 w-2.5" />
              SUB{splitPosition ? `#${splitPosition}` : ""}
            </span>
          ) : null}
          {/* T62: budget exhausted — watchdog flagged this task as over the
              wall-clock or token cap. Hidden on repeating tasks since the
              cycle reset clears the flag. */}
          {budgetExhaustedAt && !repeatIntervalMinutes ? (
            <span
              title={`Budget exhausted at ${budgetExhaustedAt}`}
              className="inline-flex items-center gap-0.5 rounded-full bg-rose-100 text-rose-700 border border-rose-200 px-1.5 py-0.5 text-[10px] font-semibold"
            >
              <AlertOctagon className="h-2.5 w-2.5" />
              BUDGET
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
        <div className="flex items-center gap-2">
          <UserCircle className="h-4 w-4 text-slate-400" />
          <span>{assignee ?? "Unassigned"}</span>
        </div>
        {due ? (
          <div
            className={cn(
              "flex items-center gap-2",
              isOverdue && "font-semibold text-rose-600",
            )}
          >
            <CalendarClock
              className={cn(
                "h-4 w-4",
                isOverdue ? "text-rose-500" : "text-slate-400",
              )}
            />
            <span>{due}</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ── T68: cycle-aware stage-timing block ────────────────────────────────
// Default: shows "Last cycle" / "Current cycle" for the most recent run.
// Expand toggle reveals the lifetime aggregation underneath.

function _fmtDuration(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function StageDurationBlock({
  durations,
  lifetime,
  isRepeat,
  currentStatus,
}: {
  durations: Array<{ status: string; totalSeconds: number }>;
  lifetime: Array<{ status: string; totalSeconds: number }> | null;
  isRepeat: boolean;
  currentStatus?: TaskStatus;
}) {
  const [showLifetime, setShowLifetime] = useState(false);
  const total = durations.reduce((a, b) => a + b.totalSeconds, 0);
  if (total < 1) return null;
  const isActiveCycle = currentStatus === "in_progress" || currentStatus === "inbox" || currentStatus === "review";
  const label = isRepeat
    ? isActiveCycle
      ? "Current cycle"
      : "Last cycle"
    : "Total";
  const lifetimeTotal = lifetime ? lifetime.reduce((a, b) => a + b.totalSeconds, 0) : 0;
  const hasMeaningfulLifetime = isRepeat && lifetimeTotal > total + 1;

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold text-emerald-600">
        <span className="h-3 w-3 flex items-center justify-center text-[8px]">
          ✓
        </span>
        {label}: {_fmtDuration(total)}
        {hasMeaningfulLifetime ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setShowLifetime((v) => !v);
            }}
            className="ml-auto text-[10px] font-medium text-slate-400 hover:text-slate-700 underline-offset-2 hover:underline"
            title="Toggle lifetime aggregation across all cycles"
          >
            {showLifetime ? "hide lifetime" : "lifetime"}
          </button>
        ) : null}
      </div>
      {durations.length > 1 ? (
        <div
          className="flex items-center gap-0.5 h-1.5 w-full rounded-full overflow-hidden bg-slate-100"
          title={durations
            .map((d) => `${d.status}: ${_fmtDuration(d.totalSeconds)}`)
            .join("  ·  ")}
        >
          {durations.map((s) => {
            const pct = Math.max(2, (s.totalSeconds / (total || 1)) * 100);
            return (
              <div
                key={s.status}
                className="h-full transition-all"
                style={{ width: `${pct}%` }}
                title={`${s.status}: ${_fmtDuration(s.totalSeconds)}`}
              />
            );
          })}
        </div>
      ) : null}
      {showLifetime && lifetime && lifetime.length > 0 ? (
        <div className="space-y-0.5 pt-1 border-t border-dashed border-slate-200">
          <div className="text-[9px] uppercase tracking-wider text-slate-400">
            Lifetime · {_fmtDuration(lifetimeTotal)}
          </div>
          {lifetime.length > 1 ? (
            <div
              className="flex items-center gap-0.5 h-1 w-full rounded-full overflow-hidden bg-slate-100"
              title={lifetime
                .map((d) => `${d.status}: ${_fmtDuration(d.totalSeconds)}`)
                .join("  ·  ")}
            >
              {lifetime.map((s) => {
                const pct = Math.max(2, (s.totalSeconds / lifetimeTotal) * 100);
                return (
                  <div
                    key={`lt-${s.status}`}
                    className="h-full transition-all"
                    style={{ width: `${pct}%` }}
                    title={`${s.status}: ${_fmtDuration(s.totalSeconds)}`}
                  />
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
