"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  RefreshCw,
  Play,
  TestTube2,
  ShieldCheck,
} from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { filterTests, isNotVerified, type TestOutcomeFilter } from "@/lib/test-outcome";
import { cn } from "@/lib/utils";

type Status = "pass" | "fail" | "error" | "pending" | "never" | "stale" | "skipped";
type DisplayStatus = Status | "not-verified";

interface TestEntry {
  id: string;
  name: string;
  category: string;
  description: string;
  kind: "auto" | "agent";
  agent_role: string | null;
  status: Status;
  // T86 (2026-05-16): set when status === "stale" — human-readable reason
  // (sig changed OR run too old) explaining why the prior pass is no
  // longer considered valid.
  stale_reason?: string;
  requires_external?: boolean;
  requires_external_reason?: string;
  last_run: {
    status: Status;
    detail: string | null;
    finished_at: string | null;
    duration_ms: number | null;
  } | null;
}

interface TestList {
  tests: TestEntry[];
  summary: Record<Status, number>;
  total: number;
  system_signature?: string;
  stale_after_seconds?: number;
  fully_autonomous: boolean;
}

async function apiGet<T>(path: string, token: string | null): Promise<T> {
  const r = await fetch(`${getApiBaseUrl()}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!r.ok) throw new Error(`${r.status}`);
  return (await r.json()) as T;
}

async function apiPost<T>(path: string, token: string | null): Promise<T> {
  const r = await fetch(`${getApiBaseUrl()}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (!r.ok) throw new Error(`${r.status}`);
  return (await r.json()) as T;
}

// T86: `stale` is rendered as UNKNOWN — last run was on a different
// system signature OR is too old to trust. `skipped` is for external-dep
// tests that the batch endpoint declines to run.
const STATUS_META: Record<DisplayStatus, { color: string; bg: string; icon: typeof CheckCircle2; label?: string }> = {
  pass:    { color: "text-emerald-700", bg: "bg-emerald-50 dark:bg-emerald-900/30", icon: CheckCircle2, label: "verified" },
  "not-verified": {
    color: "text-slate-600 dark:text-slate-300",
    bg: "bg-slate-100 dark:bg-slate-800",
    icon: AlertTriangle,
    label: "not verified",
  },
  fail:    { color: "text-rose-700",    bg: "bg-rose-50 dark:bg-rose-900/30",       icon: XCircle },
  error:   { color: "text-amber-700",   bg: "bg-amber-50 dark:bg-amber-900/30",     icon: AlertTriangle },
  pending: { color: "text-blue-700",    bg: "bg-blue-50 dark:bg-blue-900/30",       icon: Clock },
  never:   { color: "text-slate-500",   bg: "bg-slate-100 dark:bg-slate-800",       icon: TestTube2 },
  stale:   { color: "text-violet-700",  bg: "bg-violet-50 dark:bg-violet-900/30",   icon: AlertTriangle, label: "unknown" },
  skipped: { color: "text-slate-500",   bg: "bg-slate-100 dark:bg-slate-800",       icon: Clock,         label: "external" },
};

function _formatRelative(iso: string | null): string {
  if (!iso) return "never";
  try {
    const d = new Date(iso);
    const ago = Date.now() - d.getTime();
    if (ago < 0) return iso;
    const s = Math.floor(ago / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  } catch {
    return iso;
  }
}

function TestsContent() {
  const { getToken } = useAuth();
  const [data, setData] = useState<TestList | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<TestOutcomeFilter>("");
  const [running, setRunning] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const token = isLocalAuthMode() ? getLocalAuthToken() : await getToken();
      const d = await apiGet<TestList>("/api/v1/tests", token);
      setData(d);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    }
  }, [getToken]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [refresh]);

  async function runAll() {
    setBusy(true);
    try {
      const token = isLocalAuthMode() ? getLocalAuthToken() : await getToken();
      await apiPost("/api/v1/tests/run", token);
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "run failed");
    } finally {
      setBusy(false);
    }
  }

  async function runOne(id: string) {
    setRunning(id);
    try {
      const token = isLocalAuthMode() ? getLocalAuthToken() : await getToken();
      await apiPost(`/api/v1/tests/run/${encodeURIComponent(id)}`, token);
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "run failed");
    } finally {
      setRunning(null);
    }
  }

  const tests = data?.tests ?? [];
  const summary = data?.summary;
  const notVerifiedCount = tests.filter(isNotVerified).length;
  const verifiedPassCount = Math.max(0, (summary?.pass ?? 0) - notVerifiedCount);
  const displayFullyAutonomous = Boolean(data?.fully_autonomous) && notVerifiedCount === 0;
  const filtered = filterTests(tests, filter);
  const categories = [...new Set(tests.map((t) => t.category))].sort();

  return (
    <main className="flex-1 overflow-y-auto bg-gradient-to-br from-slate-100 to-slate-200">
      <div className="flex items-center gap-2 px-4 py-3 surface-panel border-b flex-wrap">
        <TestTube2 className="h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-strong">Tests</span>
        {displayFullyAutonomous ? (
          <span className="ml-2 inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 font-semibold">
            <ShieldCheck className="h-3.5 w-3.5" />
            FULLY AUTONOMOUS
          </span>
        ) : (
          <span className="ml-2 text-xs px-2 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-semibold">
            Not yet autonomous
          </span>
        )}
        <div className="flex-1" />
        <Button variant="ghost" size="sm" onClick={refresh} className="h-7 px-2" title="Refresh">
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        <Button variant="primary" size="sm" onClick={runAll} disabled={busy} className="h-7">
          <Play className="h-3.5 w-3.5 mr-1" />
          {busy ? "Running…" : "Run all"}
        </Button>
      </div>

      {err && (
        <div className="px-4 py-2 text-xs text-rose-600 dark:text-rose-400 bg-rose-50 dark:bg-rose-900/20 border-b border-rose-100 dark:border-rose-900">
          Error: {err}
        </div>
      )}

      {notVerifiedCount > 0 && (
        <div className="flex items-start gap-2 px-4 py-2 text-xs text-slate-700 dark:text-slate-200 bg-slate-100 dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>
            {notVerifiedCount} check{notVerifiedCount === 1 ? "" : "s"} had an absent precondition.
            They are neither failures nor evidence that the capability works, so they are excluded from verified passes and autonomy.
          </span>
        </div>
      )}

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3 p-4">
          {(["pass", "not-verified", "fail", "error", "pending", "never", "stale", "skipped"] as DisplayStatus[]).map((s) => {
            const Meta = STATUS_META[s];
            const Icon = Meta.icon;
            const label = Meta.label ?? s;
            const count = s === "pass" ? verifiedPassCount : s === "not-verified" ? notVerifiedCount : summary[s] ?? 0;
            return (
              <button
                key={s}
                onClick={() => setFilter(filter === s ? "" : s)}
                className={cn(
                  "rounded-lg border p-3 text-left transition",
                  Meta.bg,
                  filter === s ? "ring-2 ring-blue-500" : "",
                )}
                title={s === "stale" ? "Last pass was recorded against a different system signature OR is too old — re-run to confirm." : undefined}
              >
                <div className={cn("flex items-center gap-2 text-xs uppercase tracking-wide font-semibold", Meta.color)}>
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </div>
                <div className={cn("text-2xl font-bold mt-1", Meta.color)}>
                  {count}
                </div>
              </button>
            );
          })}
        </div>
      )}

      <div className="flex gap-2 px-4 pb-2 flex-wrap items-center text-xs">
        <span className="text-muted">Category:</span>
        <button
          onClick={() => setFilter("")}
          className={cn(
            "px-2 py-1 rounded border",
            !filter
              ? "bg-blue-600 text-white border-blue-600"
              : "bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200",
          )}
        >
          all
        </button>
        {categories.map((c) => (
          <button
            key={c}
            onClick={() => setFilter(c)}
            className={cn(
              "px-2 py-1 rounded border",
              filter === c
                ? "bg-blue-600 text-white border-blue-600"
                : "bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200",
            )}
          >
            {c}
          </button>
        ))}
        <span className="ml-auto text-muted">{filtered.length} of {tests.length}</span>
      </div>

      <div className="px-4 pb-4">
        <div className="surface-card rounded-xl overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="surface-muted text-left">
                <th className="px-3 py-2 text-muted font-semibold w-20">Status</th>
                <th className="px-3 py-2 text-muted font-semibold">Test</th>
                <th className="px-3 py-2 text-muted font-semibold w-24">Category</th>
                <th className="px-3 py-2 text-muted font-semibold w-16">Kind</th>
                <th className="px-3 py-2 text-muted font-semibold">Last detail</th>
                <th className="px-3 py-2 text-muted font-semibold w-20">Run</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t) => {
                const displayStatus = isNotVerified(t) ? "not-verified" : t.status;
                const Meta = STATUS_META[displayStatus];
                const Icon = Meta.icon;
                return (
                  <tr key={t.id} className="border-t border-slate-100 dark:border-slate-700">
                    <td className="px-3 py-2">
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase",
                          Meta.bg,
                          Meta.color,
                        )}
                        title={
                          displayStatus === "not-verified"
                            ? "NOT VERIFIED — the check's precondition was absent"
                            : t.status === "stale"
                              ? `UNKNOWN — ${t.stale_reason || "system changed since last run"}`
                              : t.last_run?.finished_at
                              ? `last run ${_formatRelative(t.last_run.finished_at)} (${t.last_run.finished_at})`
                              : undefined
                        }
                      >
                        <Icon className="h-3 w-3" />
                        {Meta.label ?? displayStatus}
                      </span>
                      {t.last_run?.finished_at && (
                        <div className="text-[10px] text-quiet mt-0.5" title={t.last_run.finished_at}>
                          {_formatRelative(t.last_run.finished_at)}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="font-medium text-strong flex items-center gap-2">
                        {t.name}
                        {t.requires_external && (
                          <span
                            className="text-[9px] font-mono px-1 py-0.5 rounded bg-amber-100 text-amber-700 border border-amber-200"
                            title={t.requires_external_reason || "Requires external prep — run individually"}
                          >
                            EXTERNAL
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-quiet font-mono">{t.id}</div>
                    </td>
                    <td className="px-3 py-2">
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-medium uppercase">
                        {t.category}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-muted">{t.kind}</td>
                    <td className="px-3 py-2 text-muted">
                      {t.status === "stale" && t.stale_reason && (
                        <div className="text-[10px] text-violet-700 italic mb-0.5">
                          stale: {t.stale_reason}
                        </div>
                      )}
                      {t.last_run?.detail ?? <span className="text-quiet">—</span>}
                      {t.last_run?.duration_ms != null && (
                        <span className="text-[10px] text-quiet ml-2">
                          ({t.last_run.duration_ms}ms)
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => runOne(t.id)}
                        disabled={running === t.id}
                        className="h-6 px-2 text-[10px]"
                      >
                        {running === t.id ? "…" : "▶"}
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </main>
  );
}

export default function TestsPage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 p-10 text-center">
          <p className="text-sm text-muted">Sign in to run tests.</p>
          <SignInButton mode="modal" forceRedirectUrl="/tests" signUpForceRedirectUrl="/tests">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <TestsContent />
      </SignedIn>
    </DashboardShell>
  );
}
