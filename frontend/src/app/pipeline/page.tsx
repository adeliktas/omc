"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RefreshCw, Activity, Crown, Gauge, Vote } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { formatRelativeTimestamp } from "@/lib/formatters";
import { cn } from "@/lib/utils";

interface StaleTask {
  id: string;
  title: string;
  status: string;
  board_name: string | null;
  agent_name: string | null;
  age_hours: number;
  assignee_traffic_age_hours: number | null;
  reason: string;
  recommended_action: string;
}

interface WipViolation {
  agent_name: string;
  wip_count: number;
  limit: number;
  task_ids: string[];
}

interface WatchdogData {
  generated_at: string;
  stale_tasks: StaleTask[];
  unassigned_inbox: StaleTask[];
  review_queue: StaleTask[];
  wip_violations: WipViolation[];
  wip_by_agent: Record<string, number>;
  summary: {
    stale_count: number;
    unassigned_count: number;
    review_count: number;
    wip_violation_count: number;
  };
}

interface ActingLead {
  acting_lead: string | null;
  agent_id: string | null;
  reason: string;
  atlas_age_seconds: number | null;
}

interface ScorecardAgent {
  agent_name: string;
  agent_id: string | null;
  calls: number;
  total_tokens: number;
  avg_latency_ms: number | null;
  errors: number;
  error_rate_pct: number;
  last_ts: string | null;
  first_ts: string | null;
}

interface Ballot {
  id: string;
  topic: string;
  description: string | null;
  proposed_action: string;
  required_quorum: number;
  status: string;
  expires_at: string | null;
  tally: { approve: number; reject: number; abstain: number; total: number };
}

async function apiGet<T>(path: string, token: string | null): Promise<T> {
  const res = await fetch(`${getApiBaseUrl()}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`${res.status}`);
  return (await res.json()) as T;
}

// 45 -> "45s", 11520 -> "3h 12m", 11923200 -> "138d"
function formatAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return d < 7 ? `${d}d ${h % 24}h` : `${d}d`;
}

function PipelineContent() {
  const { getToken } = useAuth();
  const [watchdog, setWatchdog] = useState<WatchdogData | null>(null);
  const [lead, setLead] = useState<ActingLead | null>(null);
  const [scorecard, setScorecard] = useState<ScorecardAgent[] | null>(null);
  const [ballots, setBallots] = useState<Ballot[]>([]);
  const [watchdogActive, setWatchdogActive] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const token = isLocalAuthMode() ? getLocalAuthToken() : await getToken();
      const [w, l, s, v] = await Promise.all([
        apiGet<WatchdogData>("/api/v1/pipeline/watchdog", token),
        apiGet<ActingLead>("/api/v1/pipeline/acting_lead", token),
        apiGet<{ agents: ScorecardAgent[] }>("/api/v1/pipeline/scorecard", token),
        apiGet<Ballot[]>("/api/v1/votes?status=open", token),
      ]);
      setWatchdog(w);
      setLead(l);
      setScorecard(s.agents);
      setBallots(v);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    }
  }, [getToken]);

  useEffect(() => {
    let cancelled = false;
    // One-shot watchdog-liveness signal. /health/full probes providers and can
    // take ~10s (stalling the backend meanwhile), so fetch it once, only after
    // the panels' first load instead of alongside it.
    refresh().then(async () => {
      try {
        const token = isLocalAuthMode() ? getLocalAuthToken() : await getToken();
        const h = await apiGet<{ watchdog: { active: boolean } }>(
          "/api/v1/health/full",
          token,
        );
        if (!cancelled) setWatchdogActive(h.watchdog.active);
      } catch {
        // unknown — leave null
      }
    });
    const id = setInterval(refresh, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [refresh, getToken]);

  return (
    <main className="flex-1 overflow-y-auto bg-gradient-to-br from-slate-100 to-slate-200">
      <div className="flex items-center gap-2 px-4 py-3 bg-white border-b border-slate-200">
        <Activity className="h-4 w-4 text-slate-400" />
        <span className="text-sm font-semibold text-slate-700">Pipeline</span>
        <div className="flex-1" />
        <Button variant="ghost" size="sm" onClick={refresh} className="h-7 px-2">
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs text-rose-600 bg-rose-50 border-b border-rose-100">
          Error: {err}
        </div>
      )}

      {watchdogActive === false && (
        <div className="px-4 py-2 text-xs text-amber-700 bg-amber-50 border-b border-amber-100">
          Watchdog loop is not ticking — the recommendations below are informational
          only and will not auto-escalate.
        </div>
      )}

      <div className="p-4 space-y-4">
        {/* Acting Lead */}
        <div className="bg-white rounded-lg border border-slate-200 p-4">
          <div className="flex items-center gap-2 mb-2">
            <Crown className="h-4 w-4 text-amber-500" />
            <span className="font-semibold text-sm text-slate-700">Acting Lead</span>
          </div>
          {lead ? (
            <div className="text-sm">
              <span
                className={cn(
                  "inline-block px-2 py-0.5 rounded-full text-xs font-medium mr-2",
                  lead.acting_lead === "Atlas"
                    ? "bg-emerald-50 text-emerald-700"
                    : lead.acting_lead
                      ? "bg-amber-50 text-amber-700"
                      : "bg-rose-50 text-rose-700",
                )}
              >
                {lead.acting_lead ?? "NONE"}
              </span>
              <span className="text-slate-600">{lead.reason}</span>
              {lead.atlas_age_seconds != null && (
                <span className="text-slate-400 ml-2">
                  (Atlas last call: {formatAge(lead.atlas_age_seconds)} ago)
                </span>
              )}
            </div>
          ) : (
            <div className="text-slate-400 text-sm">loading…</div>
          )}
        </div>

        {/* Open ballots */}
        {ballots.length > 0 && (
          <div className="bg-white rounded-lg border border-violet-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <Vote className="h-4 w-4 text-violet-500" />
              <span className="font-semibold text-sm text-slate-700">Open Ballots</span>
            </div>
            <ul className="space-y-3">
              {ballots.map((b) => (
                <li key={b.id} className="text-sm">
                  <div className="font-medium text-slate-800">{b.topic}</div>
                  {b.description && (
                    <div className="text-slate-600 mt-0.5">{b.description}</div>
                  )}
                  <div className="text-xs text-slate-500 mt-0.5">{b.proposed_action}</div>
                  <div className="text-xs text-slate-400 mt-1">
                    Votes {b.tally.total}/{b.required_quorum}
                    {b.expires_at &&
                      ` · expires ${new Date(b.expires_at).toLocaleString()}`}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Summary cards */}
        {watchdog && (
          <div className="grid grid-cols-4 gap-3">
            <SummaryCard
              label="Stale Tasks"
              value={watchdog.summary.stale_count}
              tone={watchdog.summary.stale_count > 0 ? "warn" : "ok"}
            />
            <SummaryCard
              label="Review Queue"
              value={watchdog.summary.review_count}
              tone={watchdog.summary.review_count > 3 ? "warn" : "ok"}
            />
            <SummaryCard
              label="Unassigned Inbox"
              value={watchdog.summary.unassigned_count}
              tone={watchdog.summary.unassigned_count > 0 ? "warn" : "ok"}
            />
            <SummaryCard
              label="WIP Violations"
              value={watchdog.summary.wip_violation_count}
              tone={watchdog.summary.wip_violation_count > 0 ? "alert" : "ok"}
            />
          </div>
        )}

        {/* Stale tasks */}
        {watchdog && watchdog.stale_tasks.length > 0 && (
          <div className="bg-white rounded-lg border border-rose-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <AlertTriangle className="h-4 w-4 text-rose-500" />
              <span className="font-semibold text-sm text-slate-700">Stale Tasks</span>
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-slate-100">
                  <th className="text-left py-1">Age</th>
                  <th className="text-left py-1">Status</th>
                  <th className="text-left py-1">Assignee</th>
                  <th className="text-left py-1">Title</th>
                  <th className="text-left py-1">Recommendation</th>
                </tr>
              </thead>
              <tbody>
                {watchdog.stale_tasks.map((t) => (
                  <tr key={t.id} className="border-b border-slate-50">
                    <td className="py-1.5 font-mono text-amber-700">
                      {formatAge(t.age_hours * 3600)}
                    </td>
                    <td className="py-1.5">{t.status}</td>
                    <td className="py-1.5">{t.agent_name || "—"}</td>
                    <td className="py-1.5 text-slate-600">{t.title}</td>
                    <td className="py-1.5 text-slate-500">{t.recommended_action}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* WIP violations */}
        {watchdog && watchdog.wip_violations.length > 0 && (
          <div className="bg-white rounded-lg border border-amber-200 p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-amber-500" />
                <span className="font-semibold text-sm text-slate-700">WIP Violations</span>
              </div>
              <a
                href="/settings/runtime"
                className="text-xs text-slate-500 underline hover:text-slate-700"
              >
                Edit WIP limits →
              </a>
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-slate-100">
                  <th className="text-left py-1">Agent</th>
                  <th className="text-right py-1">Current</th>
                  <th className="text-right py-1">Limit</th>
                  <th className="text-left py-1">Tasks</th>
                </tr>
              </thead>
              <tbody>
                {watchdog.wip_violations.map((v) => (
                  <tr key={v.agent_name} className="border-b border-slate-50">
                    <td className="py-1.5 font-medium">{v.agent_name || "—"}</td>
                    <td className="py-1.5 text-right font-mono text-amber-700">
                      {v.wip_count}
                    </td>
                    <td className="py-1.5 text-right font-mono">{v.limit}</td>
                    <td className="py-1.5 text-slate-500">{v.task_ids.length} task(s)</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Scorecard */}
        {scorecard && scorecard.length > 0 && (
          <div className="bg-white rounded-lg border border-slate-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <Gauge className="h-4 w-4 text-slate-400" />
              <span className="font-semibold text-sm text-slate-700">Agent Scorecard</span>
            </div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-500 border-b border-slate-100">
                  <th className="text-left py-1">Agent</th>
                  <th className="text-right py-1">Calls</th>
                  <th className="text-right py-1">Tokens</th>
                  <th className="text-right py-1">Avg Latency</th>
                  <th className="text-right py-1">Errors</th>
                  <th className="text-left py-1 pl-4">Last Call</th>
                </tr>
              </thead>
              <tbody>
                {scorecard.map((a) => (
                  <tr key={a.agent_name} className="border-b border-slate-50">
                    <td className="py-1.5 font-medium">{a.agent_name || "—"}</td>
                    <td className="py-1.5 text-right font-mono">{a.calls}</td>
                    <td className="py-1.5 text-right font-mono">
                      {a.total_tokens.toLocaleString()}
                    </td>
                    <td className="py-1.5 text-right font-mono">
                      {a.avg_latency_ms ? `${a.avg_latency_ms}ms` : "—"}
                    </td>
                    <td
                      className={cn(
                        "py-1.5 text-right font-mono",
                        a.errors > 0 ? "text-rose-600" : "text-slate-400",
                      )}
                    >
                      {a.errors}
                    </td>
                    <td className="py-1.5 text-slate-500 pl-4 font-mono">
                      {formatRelativeTimestamp(a.last_ts)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "ok" | "warn" | "alert";
}) {
  const toneCls =
    tone === "alert"
      ? "bg-rose-50 border-rose-200 text-rose-700"
      : tone === "warn"
        ? "bg-amber-50 border-amber-200 text-amber-700"
        : "bg-emerald-50 border-emerald-200 text-emerald-700";
  return (
    <div className={cn("rounded-lg border p-3", toneCls)}>
      <div className="text-xs font-medium uppercase tracking-wide opacity-70">{label}</div>
      <div className="text-2xl font-bold mt-1">{value}</div>
    </div>
  );
}

export default function PipelinePage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 p-10 text-center">
          <p className="text-sm text-muted">Sign in to view pipeline.</p>
          <SignInButton mode="modal" forceRedirectUrl="/pipeline" signUpForceRedirectUrl="/pipeline">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <PipelineContent />
      </SignedIn>
    </DashboardShell>
  );
}
