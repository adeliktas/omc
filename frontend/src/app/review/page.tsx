"use client";

export const dynamic = "force-dynamic";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  XCircle,
  Trash2,
  RefreshCw,
  ClipboardList,
  Clock,
  Pencil,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { cn } from "@/lib/utils";

type ReviewStatus = "pending" | "approved" | "rejected" | "revision_requested" | "resolved";

interface ReviewAutomation {
  enabled: boolean;
  models: { provider: string; model: string; eligible: boolean; block_reason: string | null }[];
  policy: string;
}

interface ReviewItem {
  id: string;
  task_id: string | null;
  updated_at: string;
  title: string;
  // Backend emits an open-ended set (e.g. "infrastructure", "capacity") beyond the
  // labels below — unknown values fall back to the raw string, not a blank badge.
  type: string;
  summary: string;
  reason: string;
  proposed_action: string;
  submitted_by: string;
  submitted_at: string;
  status: ReviewStatus;
  reviewed_at: string | null;
  reviewer_note: string | null;
  snooze_until: string | null;
  escalated_to_board_id: string | null;
  reject_options: string[] | null;
  // T105 (2026-05-25): selectable options parsed from `proposed_action`.
  proposed_options?: { label: string; action: string; note: string | null }[] | null;
  // The linked task's live state, so a decision can act on the task itself.
  task_board_id?: string | null;
  task_status?: string | null;
  task_resume_status?: string | null;
  task_tokens_used?: number | null;
}

// Longer text moves to the item's own page (/review/<id>); a card stays decidable at a glance.
const GIST_MAX = 280;
const RETRY_TOKENS = 800_000;

type TaskChoice = "retry" | "cancel";

/** "MAJOR: task failed 3 times: <task>" -> ["task failed 3 times", "<task>"] */
function splitTitle(title: string): [string | null, string] {
  const m = title.match(/^MAJOR: ([^:]{3,60}): (.+)$/);
  return m ? [m[1], m[2]] : [null, title];
}

function gist(item: ReviewItem): string {
  // Task items put the decision-ready line in `reason`; other submitters in a short summary.
  const text = item.task_id && item.reason ? item.reason
    : item.summary.length <= GIST_MAX ? item.summary : item.reason || item.summary;
  return text.length <= GIST_MAX ? text : `${text.slice(0, GIST_MAX)}…`;
}

/** Fleet failures: one line on what happened and three choices that act on the task. */
function DecisionCard({
  item,
  onTask,
  onAction,
  busy,
}: {
  item: ReviewItem;
  onTask: (item: ReviewItem, choice: TaskChoice) => void;
  onAction: (id: string, action: string, note: string) => void;
  busy: boolean;
}) {
  const [kind, headline] = splitTitle(item.title);
  const stuck = item.task_status === "paused" || item.task_status === "blocked";
  const tokens = item.task_tokens_used ? `${(item.task_tokens_used / 1e6).toFixed(1)}M tokens` : null;
  const choice = "px-2.5 py-1.5 text-xs rounded-md border font-medium transition-colors disabled:opacity-50";
  return (
    <div className="rounded-xl border border-yellow-200 bg-white p-4 shadow-sm flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2 flex-wrap text-[11px] text-slate-500">
        <span className="flex items-center gap-1.5 flex-wrap">
          <span className="font-semibold uppercase tracking-wide text-red-600">{kind ?? TYPE_LABELS[item.type] ?? item.type}</span>
          {item.task_status && <span className="rounded-full bg-slate-100 px-2 py-0.5">task {item.task_status}</span>}
          {tokens && <span className="rounded-full bg-slate-100 px-2 py-0.5">{tokens}</span>}
        </span>
        <span>{fmt(item.submitted_at)}</span>
      </div>
      <h3 className="font-semibold text-slate-800 text-sm leading-snug">{headline}</h3>
      <p className="text-xs text-slate-600 break-words">{gist(item)}</p>
      {item.status === "pending" && (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {stuck && item.task_board_id ? (
            <>
              <button className={cn(choice, "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100")}
                disabled={busy} onClick={() => onTask(item, "retry")}>
                Retry (+{RETRY_TOKENS / 1000}k tokens)
              </button>
              <button className={cn(choice, "border-red-300 bg-red-50 text-red-800 hover:bg-red-100")}
                disabled={busy} onClick={() => onTask(item, "cancel")}>
                Cancel task
              </button>
              <button className={cn(choice, "border-slate-300 bg-slate-50 text-slate-700 hover:bg-slate-100")}
                disabled={busy} onClick={() => onAction(item.id, "defer", "Decide later")}>
                Decide later
              </button>
            </>
          ) : (
            <>
              <button className={cn(choice, "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100")}
                disabled={busy} onClick={() => onAction(item.id, "resolve", "Owner marked this handled.")}>
                Mark handled
              </button>
              <button className={cn(choice, "border-slate-300 bg-slate-50 text-slate-700 hover:bg-slate-100")}
                disabled={busy} onClick={() => onAction(item.id, "defer", "Decide later")}>
                Decide later
              </button>
            </>
          )}
        </div>
      )}
      {item.reviewer_note && item.status !== "pending" && (
        <p className="text-xs text-slate-500">Outcome: {item.reviewer_note}</p>
      )}
      <div className="flex gap-3 text-xs">
        <Link className="text-blue-700 hover:underline" href={`/review/${item.id}`}>Full report</Link>
        {item.task_board_id && (
          <Link className="text-blue-700 hover:underline" href={`/boards/${item.task_board_id}?taskId=${item.task_id}`}>Open task</Link>
        )}
      </div>
    </div>
  );
}

const TYPE_LABELS: Record<string, string> = {
  "frontend-feature": "Frontend",
  "backend-change": "Backend",
  "llm-engine": "LLM Engine",
  "security": "Security",
  "infrastructure": "Infrastructure",
  "capacity": "Capacity",
  "other": "Other",
  "major_decision": "Owner decision",
  "task-failure": "Task failure",
};

const TYPE_COLORS: Record<string, string> = {
  "frontend-feature": "bg-blue-100 text-blue-700",
  "backend-change": "bg-purple-100 text-purple-700",
  "llm-engine": "bg-amber-100 text-amber-700",
  "security": "bg-red-100 text-red-700",
  "task-failure": "bg-red-600 text-white",
  "infrastructure": "bg-cyan-100 text-cyan-700",
  "capacity": "bg-indigo-100 text-indigo-700",
  "other": "bg-slate-100 text-slate-600",
};
const DEFAULT_TYPE_COLOR = "bg-slate-100 text-slate-600";

const STATUS_COLORS: Record<ReviewStatus, string> = {
  pending: "bg-yellow-100 text-yellow-700 border-yellow-200",
  approved: "bg-emerald-100 text-emerald-700 border-emerald-200",
  rejected: "bg-red-100 text-red-700 border-red-200",
  revision_requested: "bg-orange-100 text-orange-700 border-orange-200",
  resolved: "bg-slate-100 text-slate-700 border-slate-200",
};

const STATUS_LABELS: Record<ReviewStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  revision_requested: "Revision Requested",
  resolved: "Resolved",
};

function authHeaders(): Record<string, string> {
  if (isLocalAuthMode()) {
    const t = getLocalAuthToken();
    if (t) return { Authorization: `Bearer ${t}`, "Content-Type": "application/json" };
  }
  return { "Content-Type": "application/json" };
}

function fmt(iso: string | null): string {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function ReviewCard({
  item,
  onApprove,
  onReject,
  onAction,
  onDelete,
  busy,
}: {
  item: ReviewItem;
  onApprove: (id: string) => void;
  onReject: (id: string, suggestion: string) => void;
  onAction: (id: string, action: string, note: string) => void;
  onDelete: (id: string) => void;
  busy: boolean;
}) {
  const options = item.reject_options ?? [];
  const proposedOptions = item.proposed_options ?? [];
  const [customText, setCustomText] = useState("");

  const isSnoozed = item.snooze_until && new Date(item.snooze_until) > new Date();

  const handleReject = (text: string) => {
    if (!text.trim()) return;
    onReject(item.id, text.trim());
  };

  return (
    <div className={cn(
      "rounded-xl border bg-white p-4 shadow-sm flex flex-col gap-3",
      item.status === "pending" ? "border-yellow-200"
        : item.status === "approved" ? "border-emerald-200"
        : item.status === "revision_requested" ? "border-orange-200"
        : item.status === "rejected" ? "border-red-200"
        : "border-slate-200"
    )}>
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={cn("text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase tracking-wide", TYPE_COLORS[item.type] ?? DEFAULT_TYPE_COLOR)}>
            {TYPE_LABELS[item.type] ?? item.type}
          </span>
          <span className={cn("text-[10px] font-semibold px-2 py-0.5 rounded-full border uppercase tracking-wide", STATUS_COLORS[item.status])}>
            {STATUS_LABELS[item.status]}
          </span>
          {isSnoozed && (
            <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200">
              Snoozed
            </span>
          )}
        </div>
        <span className="text-[11px] text-slate-400 shrink-0">{fmt(item.submitted_at)}</span>
      </div>

      <div>
        <h3 className="font-semibold text-slate-800 text-sm leading-snug">{item.title}</h3>
        <p className="text-xs text-slate-500 mt-0.5">by {item.submitted_by}</p>
      </div>

      <div className="space-y-1.5 text-xs text-slate-600">
        <div><span className="font-medium text-slate-700">Summary: </span>{gist({ ...item, reason: "" })}
          {item.summary.length > GIST_MAX && <> <Link className="text-blue-700 hover:underline" href={`/review/${item.id}`}>Full report</Link></>}
        </div>
        <div><span className="font-medium text-slate-700">Reason: </span>{item.reason}</div>
        <div><span className="font-medium text-slate-700">Proposed action: </span>{item.proposed_action}</div>
        {item.reviewer_note && (
          <div><span className="font-medium text-slate-700">Note: </span>{item.reviewer_note}</div>
        )}
        {item.snooze_until && (
          <div className="text-slate-400">
            <Clock className="h-3 w-3 inline mr-1" />
            Snoozed until {fmt(item.snooze_until)}
          </div>
        )}
        {item.reviewed_at && item.status !== "pending" && (
          <div className="text-slate-400">Reviewed {fmt(item.reviewed_at)}</div>
        )}
      </div>

      {item.status === "pending" && (
        <div className="flex flex-col gap-2 pt-1">
          {/* T105 (2026-05-25): proposed-action options as clickable buttons.
              Parsed from submitter's text — e.g. watchdog stale-task
              "(a) reassign manually / (b) close as won't-fix / (c) accept and resume". */}
          {proposedOptions.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">
                Submitter&apos;s suggested actions
              </span>
              {proposedOptions.map((opt, idx) => {
                const colorByAction: Record<string, string> = {
                  approve: "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100",
                  reject: "border-red-300 bg-red-50 text-red-800 hover:bg-red-100",
                  request_revision: "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100",
                  escalate_to_lead: "border-blue-300 bg-blue-50 text-blue-800 hover:bg-blue-100",
                  defer: "border-slate-300 bg-slate-50 text-slate-700 hover:bg-slate-100",
                };
                const cls = colorByAction[opt.action] || colorByAction.request_revision;
                return (
                  <button
                    key={idx}
                    className={cn(
                      "flex items-start text-left px-2.5 py-1.5 text-xs rounded-md border transition-colors leading-tight",
                      cls,
                    )}
                    disabled={busy}
                    onClick={() => onAction(item.id, opt.action, opt.note || opt.label)}
                  >
                    <span className="font-mono text-[10px] mr-2 mt-0.5 opacity-60 uppercase">
                      {opt.action.replace("_", " ")}
                    </span>
                    <span>{opt.label}</span>
                  </button>
                );
              })}
            </div>
          )}

          {/* Approve */}
          <Button
            size="sm"
            variant="primary"
            className="h-7 text-xs bg-emerald-600 hover:bg-emerald-700 w-full font-semibold"
            disabled={busy}
            onClick={() => onApprove(item.id)}
          >
            <CheckCircle2 className="h-3 w-3 mr-1" /> Approve
          </Button>

          {/* Generated reject options as clickable buttons */}
          {options.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wider">Reject — select alternative</span>
              {options.map((opt, idx) => (
                <button
                  key={idx}
                  className={cn(
                    "flex items-center text-left px-2.5 py-1.5 text-xs rounded-md border border-red-200",
                    "text-red-700 bg-red-50 hover:bg-red-100 transition-colors",
                    "leading-tight"
                  )}
                  disabled={busy}
                  onClick={() => handleReject(opt)}
                >
                  <XCircle className="h-3 w-3 mr-1.5 shrink-0 mt-0.5" />
                  <span>{opt}</span>
                </button>
              ))}
            </div>
          )}

          {/* Custom rejection */}
          <div className="flex items-center gap-1.5">
            <Input
              className="h-7 text-xs flex-1 border-red-200 focus:border-red-400"
              placeholder="Or type your own rejection reason…"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && customText.trim()) {
                  e.preventDefault();
                  handleReject(customText);
                  setCustomText("");
                }
              }}
            />
            <Button
              size="sm"
              variant="outline"
              className="h-7 px-2 text-xs border-red-300 text-red-600 hover:bg-red-50 shrink-0"
              disabled={busy || !customText.trim()}
              onClick={() => {
                handleReject(customText);
                setCustomText("");
              }}
            >
              <Pencil className="h-3 w-3" />
            </Button>
          </div>

          {/* Separator */}
          <div className="border-t border-slate-100 my-1" />

          {/* Ignore / Delete */}
          <Button
            size="sm"
            variant="ghost"
            className="h-7 text-xs text-slate-400 hover:text-red-500 w-full"
            disabled={busy}
            onClick={() => onDelete(item.id)}
          >
            <Trash2 className="h-3 w-3 mr-1" /> Ignore / Delete
          </Button>
        </div>
      )}

      {item.status === "revision_requested" && (
        <div className="flex items-center gap-2 pt-1">
          <Button
            size="sm"
            variant="primary"
            className="h-7 px-3 text-xs bg-emerald-600 hover:bg-emerald-700"
            disabled={busy}
            onClick={() => onApprove(item.id)}
          >
            <CheckCircle2 className="h-3 w-3 mr-1" /> Approve now
          </Button>
          <div className="flex items-center gap-1.5 flex-1">
            <Input
              className="h-7 text-xs flex-1 border-red-200 focus:border-red-400"
              placeholder="Rejection reason…"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && customText.trim()) {
                  e.preventDefault();
                  handleReject(customText);
                  setCustomText("");
                }
              }}
            />
            <Button
              size="sm"
              variant="outline"
              className="h-7 px-2 text-xs border-red-300 text-red-600 hover:bg-red-50 shrink-0"
              disabled={busy || !customText.trim()}
              onClick={() => { handleReject(customText); setCustomText(""); }}
            >
              <XCircle className="h-3 w-3" />
            </Button>
          </div>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs text-slate-400 hover:text-red-500 ml-auto shrink-0"
            disabled={busy}
            onClick={() => onDelete(item.id)}
          >
            <Trash2 className="h-3 w-3" />
          </Button>
        </div>
      )}
    </div>
  );
}

function ReviewPageInner() {
  const { isSignedIn } = useAuth();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [automation, setAutomation] = useState<ReviewAutomation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<ReviewStatus | "all">("pending");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    if (!isSignedIn) return;
    try {
      const url = `${getApiBaseUrl()}/api/v1/review/items`;
      const res = await fetch(url, { headers: authHeaders(), cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ReviewItem[];
      setItems(data);
      const policy = await fetch(`${getApiBaseUrl()}/api/v1/review/automation`, { headers: authHeaders(), cache: "no-store" });
      if (!policy.ok) throw new Error(`Review policy HTTP ${policy.status}`);
      setAutomation(await policy.json() as ReviewAutomation);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [isSignedIn]);

  useEffect(() => {
    const initial = setTimeout(() => void load(), 0);
    intervalRef.current = setInterval(() => void load(), 30_000);
    return () => { clearTimeout(initial); if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [load]);

  const expected = useCallback((id: string) => {
    const item = items.find((candidate) => candidate.id === id);
    return { expected_status: item?.status, expected_updated_at: item?.updated_at };
  }, [items]);

  const toggleAutomation = async () => {
    if (!automation) return;
    setBusy(true);
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/automation`, {
        method: "PUT", headers: authHeaders(),
        body: JSON.stringify({ enabled: !automation.enabled }),
      });
      if (!res.ok) throw new Error(`Review policy HTTP ${res.status}`);
      setAutomation(await res.json() as ReviewAutomation);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Policy update failed");
    } finally { setBusy(false); }
  };

  const doApprove = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items/${id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ action: "approve", ...expected(id) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = (await res.json()) as ReviewItem;
      setItems((prev) => prev.map((i) => (i.id === id ? updated : i)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      setBusy(false);
    }
  }, [expected]);

  const doReject = useCallback(async (id: string, suggestion: string) => {
    setBusy(true);
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items/${id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({
          action: "reject",
          reviewer_note: suggestion,
          ...expected(id),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = (await res.json()) as ReviewItem;
      setItems((prev) => prev.map((i) => (i.id === id ? updated : i)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reject failed");
    } finally {
      setBusy(false);
    }
  }, [expected]);

  // T105 (2026-05-25): generic action for proposed-option buttons.
  const doAction = useCallback(async (id: string, action: string, note: string) => {
    setBusy(true);
    try {
      const body: { action: string; reviewer_note: string; snooze_hours?: number; expected_status?: ReviewStatus; expected_updated_at?: string } = { action, reviewer_note: note, ...expected(id) };
      if (action === "defer") body.snooze_hours = 24;
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items/${id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = (await res.json()) as ReviewItem;
      setItems((prev) => prev.map((i) => (i.id === id ? updated : i)));
    } catch (e) {
      setError(e instanceof Error ? e.message : `${action} failed`);
    } finally {
      setBusy(false);
    }
  }, [expected]);

  const doTask = useCallback(async (item: ReviewItem, choice: TaskChoice) => {
    setBusy(true);
    try {
      const patch = async (body: Record<string, unknown>) => {
        const res = await fetch(`${getApiBaseUrl()}/api/v1/boards/${item.task_board_id}/tasks/${item.task_id}`, {
          method: "PATCH", headers: authHeaders(), body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(`Task HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      };
      let note = "Owner cancelled the task.";
      if (choice === "cancel") {
        await patch({ status: "cancelled", comment: "Owner cancelled this task from /review." });
      } else {
        note = `Owner retried the task with +${RETRY_TOKENS / 1000}k tokens.`;
        await patch({ max_tokens_budget: (item.task_tokens_used ?? 0) + RETRY_TOKENS });
        // A paused task resumes to the status it was paused from before it can go back to the queue.
        if (item.task_status === "paused" && item.task_resume_status && item.task_resume_status !== "inbox") {
          await patch({ status: item.task_resume_status });
        }
        await patch({ status: "inbox", comment: `${note} (from /review)` });
      }
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items/${item.id}`, {
        method: "PATCH", headers: authHeaders(),
        body: JSON.stringify({ action: "resolve", reviewer_note: note, ...expected(item.id) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : `${choice} failed`);
      void load();
    } finally {
      setBusy(false);
    }
  }, [expected, load]);

  const doDelete = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items/${id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setItems((prev) => prev.filter((i) => i.id !== id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  }, []);

  const visible = filter === "all" ? items : items.filter((i) => i.status === filter);
  const counts = {
    all: items.length,
    pending: items.filter((i) => i.status === "pending").length,
    revision_requested: items.filter((i) => i.status === "revision_requested").length,
    approved: items.filter((i) => i.status === "approved").length,
    rejected: items.filter((i) => i.status === "rejected").length,
    resolved: items.filter((i) => i.status === "resolved").length,
  };

  const FILTER_OPTIONS = [
    { key: "pending", label: "Pending", activeColor: "bg-yellow-100 text-yellow-800 border-yellow-300" },
    { key: "revision_requested", label: "Revision", activeColor: "bg-orange-100 text-orange-800 border-orange-300" },
    { key: "all", label: "All", activeColor: "bg-slate-800 text-white border-transparent" },
    { key: "approved", label: "Approved", activeColor: "bg-emerald-100 text-emerald-800 border-emerald-300" },
    { key: "rejected", label: "Rejected", activeColor: "bg-red-100 text-red-800 border-red-300" },
    { key: "resolved", label: "Resolved", activeColor: "bg-slate-200 text-slate-800 border-slate-300" },
  ] as const;

  return (
    <main className="flex-1 overflow-y-auto bg-gradient-to-br from-slate-50 to-slate-100">
      <div className="p-4 md:p-6 max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <ClipboardList className="h-5 w-5 text-slate-500" />
            <h1 className="text-lg font-semibold text-slate-800">Decisions &amp; autonomous review</h1>
            {counts.pending > 0 && (
              <span className="text-[10px] bg-yellow-500 text-white rounded-full px-1.5 py-0.5 font-bold">
                {counts.pending}
              </span>
            )}
            {counts.revision_requested > 0 && (
              <span className="text-[10px] bg-orange-500 text-white rounded-full px-1.5 py-0.5 font-bold">
                {counts.revision_requested}
              </span>
            )}
          </div>
          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={cn("h-3 w-3 mr-1", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>

        {automation && (
          <section aria-label="Autonomous acceptance policy" className="mb-4 rounded-xl border bg-white p-4 text-sm text-slate-700">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">Routine acceptance {automation.enabled ? "enabled" : "paused"}</h2>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void toggleAutomation()}>
                {automation.enabled ? "Pause routine reviews" : "Enable routine reviews"}
              </Button>
            </div>
            <p className="mt-2 text-xs">{automation.policy}</p>
            <ol className="mt-2 list-inside list-decimal text-xs">
              {automation.models.map((model) => (
                <li key={`${model.provider}/${model.model}`}>
                  {model.model === "gpt-6.1-sol" ? "GPT-6.1 Sol" : model.model === "opus-5-5" ? "Claude Opus 5.5" : model.model === "glm-5.3" ? "GLM-5.3" : model.model}
                  {" — "}{model.eligible ? "eligible" : `unavailable (${model.block_reason ?? "not ready"})`}
                </li>
              ))}
            </ol>
            {automation.enabled && !automation.models.some((model) => model.eligible) && (
              <p className="mt-2 text-xs">Waiting for a qualified model; no weaker model is substituted.</p>
            )}
            <p className="mt-2 text-xs">Only major new system, authority or goal decisions need you. Acceptance evidence and resolved notices remain in the audit history.</p>
          </section>
        )}

        <div className="flex gap-1.5 mb-4 flex-wrap">
          {FILTER_OPTIONS.map(({ key, label, activeColor }) => (
            <button
              key={key}
              onClick={() => setFilter(key as ReviewStatus | "all")}
              className={cn(
                "px-3 py-1 rounded-full text-xs font-medium border transition",
                filter === key
                  ? activeColor
                  : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
              )}
            >
              {label} ({counts[key]})
            </button>
          ))}
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-xs text-red-600">
            {error}
          </div>
        )}

        {loading && items.length === 0 ? (
          <div className="text-sm text-slate-400 text-center py-12">Loading\u2026</div>
        ) : visible.length === 0 ? (
          <div className="text-sm text-slate-400 text-center py-12">
            No {filter === "all" ? "" : filter.replace(/_/g, " ")} items.
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {visible.map((item) => item.type === "task-failure" ? (
              <DecisionCard key={item.id} item={item} onTask={doTask} onAction={doAction} busy={busy} />
            ) : (
              <ReviewCard
                key={item.id}
                item={item}
                onApprove={doApprove}
                onReject={doReject}
                onAction={doAction}
                onDelete={doDelete}
                busy={busy}
              />
            ))}
          </div>
        )}
      </div>
    </main>
  );
}

export default function ReviewPage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 rounded-2xl p-10 text-center">
          <p className="text-sm text-muted">Sign in to view owner review queue.</p>
          <SignInButton mode="modal" forceRedirectUrl="/review" signUpForceRedirectUrl="/review">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <ReviewPageInner />
      </SignedIn>
    </DashboardShell>
  );
}
