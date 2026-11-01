"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Clock } from "lucide-react";

interface StageTiming {
  id: string;
  status: string;
  agent_id: string | null;
  agent_name: string | null;
  entered_at: string;
  exited_at: string | null;
  duration_seconds: number | null;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;
  return `${hours}h ${remainingMins}m`;
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function statusLabel(status: string): string {
  switch (status) {
    case "inbox":
      return "Idle / Inbox";
    case "in_progress":
      return "In Progress";
    case "review":
      return "Review";
    case "done":
      return "Done";
    default:
      return status;
  }
}

function statusColor(status: string): string {
  switch (status) {
    case "inbox":
      return "bg-slate-400";
    case "in_progress":
      return "bg-emerald-500";
    case "review":
      return "bg-amber-500";
    case "done":
      return "bg-blue-500";
    default:
      return "bg-slate-400";
  }
}

/** Aggregate timings per-status from flat row list. */
function aggregateByStatus(
  timings: StageTiming[],
): Array<{ status: string; totalSeconds: number; rows: StageTiming[] }> {
  const order = ["inbox", "in_progress", "review", "done"];
  const map = new Map<string, { totalSeconds: number; rows: StageTiming[] }>();

  for (const t of timings) {
    if (!map.has(t.status)) {
      map.set(t.status, { totalSeconds: 0, rows: [] });
    }
    const entry = map.get(t.status)!;
    if (t.duration_seconds !== null) {
      entry.totalSeconds += t.duration_seconds;
    }
    entry.rows.push(t);
  }

  return order
    .filter((s) => map.has(s))
    .map((s) => ({ status: s, ...map.get(s)! }));
}

export function ProcessingTimeline({ timings }: { timings: StageTiming[] }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  if (!timings || timings.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        No processing data yet.
      </p>
    );
  }

  const aggregated = aggregateByStatus(timings);
  const totalSeconds = aggregated.reduce((sum, s) => sum + s.totalSeconds, 0);

  // Compute the max totalSeconds across stages for proportional bar
  const maxSeconds = Math.max(...aggregated.map((s) => s.totalSeconds), 1);

  const toggleExpand = (status: string) => {
    setExpanded((prev) => ({ ...prev, [status]: !prev[status] }));
  };

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      {/* Summary bar */}
      <div className="flex h-3 overflow-hidden rounded-t-lg">
        {aggregated.map((stage) => {
          const pct = maxSeconds > 0 ? (stage.totalSeconds / maxSeconds) * 100 : 0;
          return (
            <div
              key={stage.status}
              className={`${statusColor(stage.status)} transition-all`}
              style={{ width: `${Math.max(pct, 2)}%` }}
              title={`${statusLabel(stage.status)}: ${formatDuration(stage.totalSeconds)}`}
            />
          );
        })}
      </div>

      {/* Per-stage summary rows */}
      <div className="divide-y divide-slate-100">
        {aggregated.map((stage) => (
          <div key={stage.status}>
            <button
              type="button"
              className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-slate-50 transition"
              onClick={() => toggleExpand(stage.status)}
            >
              {expanded[stage.status] ? (
                <ChevronDown className="h-3.5 w-3.5 text-slate-400 shrink-0" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5 text-slate-400 shrink-0" />
              )}
              <span className={`h-2.5 w-2.5 rounded-full shrink-0 ${statusColor(stage.status)}`} />
              <span className="text-xs font-semibold text-slate-700">
                {statusLabel(stage.status)}
              </span>
              <span className="ml-auto flex items-center gap-1 text-xs text-slate-500">
                <Clock className="h-3 w-3" />
                {formatDuration(stage.totalSeconds)}
              </span>
            </button>

            {/* Expandable agent-level rows */}
            {expanded[stage.status] && (
              <div className="border-t border-slate-100 bg-slate-50 px-4 py-2">
                <div className="space-y-1.5">
                  {stage.rows.map((row) => {
                    const hasExit = row.exited_at !== null;
                    return (
                      <div
                        key={row.id}
                        className="flex items-center gap-2 text-xs text-slate-600"
                      >
                        <span className="font-medium min-w-0 truncate max-w-[140px]">
                          {row.agent_name || "unknown"}
                        </span>
                        <span className="text-slate-400">
                          {formatTimestamp(row.entered_at)}
                          {hasExit ? ` → ${formatTimestamp(row.exited_at!)}` : " → (active)"}
                        </span>
                        {row.duration_seconds !== null && (
                          <span className="ml-auto tabular-nums text-slate-500">
                            {formatDuration(row.duration_seconds)}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Total line */}
      <div className="border-t border-slate-200 bg-slate-50 px-4 py-2 text-xs text-slate-500">
        <span className="font-semibold text-slate-700">Total:</span>{" "}
        {formatDuration(totalSeconds)}
      </div>
    </div>
  );
}
