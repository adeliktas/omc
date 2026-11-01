"use client";

export const dynamic = "force-dynamic";

import { useEffect, useMemo, useState, useCallback } from "react";
import { Zap, RefreshCw, Cpu, Cloud, CloudOff, Activity, Award } from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type BenchmarkRow = {
  provider: string;
  model: string;
  ts: string | null;
  duration_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  tps: number | null;
  ok: boolean | null;
  error: string | null;
};

type ProviderRow = {
  provider_name: string;
  status: string;
  base_url: string | null;
};

type ModelMeta = {
  id: string;
  name?: string;
  iq_score?: number;
  iq_detail?: Record<string, number>;
  iq_evaluated_at?: number;
  disabled?: boolean;
  disabled_reason?: string;
};

const fmtNum = (n: number | null | undefined): string => {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1_000) return n.toLocaleString();
  if (n < 1_000_000) return (n / 1_000).toFixed(2) + "k";
  return (n / 1_000_000).toFixed(2) + "M";
};

const fmtAgo = (iso: string | null): string => {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`;
  return `${Math.round(ms / 86_400_000)}d ago`;
};

function BenchmarkContent() {
  const { getToken } = useAuth();
  const [benchmarks, setBenchmarks] = useState<BenchmarkRow[]>([]);
  const [providers, setProviders] = useState<ProviderRow[]>([]);
  const [models, setModels] = useState<Record<string, ModelMeta[]>>({});
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [interval, setIntervalState] = useState<number | null>(null);
  const [intervalDraft, setIntervalDraft] = useState<string>("");
  const [savingInterval, setSavingInterval] = useState(false);
  const [loopEnabled, setLoopEnabled] = useState<boolean | null>(null);

  const authHeader = useCallback(async (): Promise<HeadersInit> => {
    if (isLocalAuthMode()) {
      const t = getLocalAuthToken();
      return t ? { Authorization: `Bearer ${t}` } : {};
    }
    const t = await getToken();
    return t ? { Authorization: `Bearer ${t}` } : {};
  }, [getToken]);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const base = getApiBaseUrl();
      const auth = await authHeader();
      const [bRes, pRes, sRes] = await Promise.all([
        fetch(`${base}/api/v1/providers/benchmarks`, { headers: auth }),
        fetch(`${base}/api/v1/providers`, { headers: auth }),
        fetch(`${base}/api/v1/settings/runtime`, { headers: auth }),
      ]);
      if (sRes.ok) {
        const sd = await sRes.json();
        const found = (sd.settings || []).find(
          (e: { key: string; value: number | null }) =>
            e.key === "BENCHMARK_INTERVAL_SECONDS",
        );
        if (found) {
          setIntervalState(found.value as number);
          setIntervalDraft(String(found.value));
        }
        const enabledSetting = (sd.settings || []).find(
          (e: { key: string; value: unknown }) => e.key === "BENCHMARK_ENABLED",
        );
        if (enabledSetting) setLoopEnabled(Boolean(enabledSetting.value));
      }
      if (!bRes.ok) throw new Error(`benchmarks: ${bRes.status}`);
      if (!pRes.ok) throw new Error(`providers: ${pRes.status}`);
      const bRaw = await bRes.json();
      // `/providers/benchmarks` returns `{benchmarks: [...], count: N}`.
      const b: BenchmarkRow[] = Array.isArray(bRaw)
        ? bRaw
        : Array.isArray(bRaw?.benchmarks)
          ? bRaw.benchmarks
          : [];
      const pRaw = await pRes.json();
      const p: ProviderRow[] = Array.isArray(pRaw) ? pRaw : [];
      setBenchmarks(b);
      setProviders(p);

      // Fetch curated models per provider for IQ + disabled metadata
      const modelMap: Record<string, ModelMeta[]> = {};
      await Promise.all(
        p.map(async (prov) => {
          const r = await fetch(
            `${base}/api/v1/providers/${encodeURIComponent(prov.provider_name)}/models`,
            { headers: auth },
          );
          if (r.ok) {
            modelMap[prov.provider_name] = (await r.json()) as ModelMeta[];
          }
        }),
      );
      setModels(modelMap);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [authHeader]);

  useEffect(() => {
    reload();
  }, [reload]);

  const runBenchmark = useCallback(async () => {
    setRunning(true);
    try {
      const base = getApiBaseUrl();
      const auth = await authHeader();
      const r = await fetch(`${base}/api/v1/providers/benchmark-run`, {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
      });
      if (!r.ok) throw new Error(`run failed: ${r.status}`);
      await new Promise((res) => setTimeout(res, 1500));
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [authHeader, reload]);

  const runIqEval = useCallback(async () => {
    setRunning(true);
    try {
      const base = getApiBaseUrl();
      const auth = await authHeader();
      const r = await fetch(`${base}/api/v1/providers/eval`, {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
      });
      if (!r.ok) throw new Error(`iq-eval failed: ${r.status}`);
      await new Promise((res) => setTimeout(res, 2000));
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [authHeader, reload]);

  const providerStatus = useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of providers) m[p.provider_name] = p.status;
    return m;
  }, [providers]);

  const [showOffline, setShowOffline] = useState(false);

  // Default: show ACTIVE providers only (T103 v34 — benchmark + IQ skip
  // offline so historical rows are stale anyway). Toggle to view history.
  const visibleRows = useMemo(() => {
    const rows: Array<{
      provider: string;
      model: string;
      bench: BenchmarkRow | undefined;
      meta: ModelMeta | undefined;
      online: boolean;
    }> = [];
    for (const prov of providers) {
      const online = prov.status === "online";
      if (!online && !showOffline) continue;
      const provModels = models[prov.provider_name] || [];
      for (const m of provModels) {
        const bench = benchmarks.find(
          (b) => b.provider === prov.provider_name && b.model === m.id,
        );
        rows.push({
          provider: prov.provider_name,
          model: m.id,
          bench,
          meta: m,
          online,
        });
      }
    }
    return rows.sort((a, b) => {
      if (a.online !== b.online) return a.online ? -1 : 1;
      return (b.bench?.tps || 0) - (a.bench?.tps || 0);
    });
  }, [providers, models, benchmarks, showOffline]);

  const activeCount = providers.filter((p) => p.status === "online").length;
  const totalModelCount = visibleRows.length;
  const activeModelCount = visibleRows.filter((r) => r.online).length;
  // /providers/benchmark-run silently no-ops below 2 online providers, and
  // /providers/eval skips disabled models — reflect both preconditions here
  // instead of letting the button look actionable when it will do nothing.
  const eligibleEvalModelCount = visibleRows.filter((r) => r.online && !r.meta?.disabled).length;

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Zap className="h-6 w-6 text-amber-500" />
            Benchmark & IQ Eval
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Per-(provider, model) latency, throughput, IQ score for routable API endpoints.
            Only <strong>active</strong> endpoints are benchmarked / scored — offline ones
            are skipped to save tokens. Coding harness subscriptions are measured on Ranking.
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <label className="text-xs text-slate-600 flex items-center gap-1 mr-2">
            <input
              type="checkbox"
              checked={showOffline}
              onChange={(e) => setShowOffline(e.target.checked)}
            />
            show offline history
          </label>
          <Button onClick={reload} disabled={loading} variant="outline" size="sm">
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button
            onClick={runBenchmark}
            disabled={running || activeCount < 2}
            size="sm"
            title={activeCount < 2 ? "Needs 2+ online providers to compare — only " + activeCount + " online" : undefined}
          >
            <Activity className="h-4 w-4 mr-2" />
            Run benchmark now
          </Button>
          <Button
            onClick={runIqEval}
            disabled={running || eligibleEvalModelCount === 0}
            size="sm"
            variant="secondary"
            title={eligibleEvalModelCount === 0 ? "No online, enabled models to evaluate" : undefined}
          >
            <Award className="h-4 w-4 mr-2" />
            Run IQ eval
          </Button>
        </div>
      </header>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      {!loading && (activeCount < 2 || eligibleEvalModelCount === 0) && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 text-amber-800 p-3 text-sm space-y-1">
          {activeCount < 2 && (
            <p>
              Comparison benchmark needs 2+ online providers; only {activeCount} online.
              &quot;Run benchmark now&quot; is disabled.
            </p>
          )}
          {eligibleEvalModelCount === 0 && (
            <p>
              No online provider has an enabled curated model. &quot;Run IQ eval&quot; is
              disabled — check <a href="/providers" className="underline">/providers</a>.
            </p>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatBox label="Active API endpoints" value={`${activeCount}/${providers.length}`} icon={<Cloud className="h-4 w-4" />} />
        <StatBox label="Models tracked" value={`${activeModelCount}/${totalModelCount}`} icon={<Cpu className="h-4 w-4" />} />
        <StatBox label="Latest benchmark result" value={(() => {
          if (benchmarks.length === 0) return "—";
          const newest = benchmarks.reduce((max, b) =>
            !max || (b.ts && b.ts > (max.ts || "")) ? b : max, benchmarks[0]);
          return `${fmtAgo(newest.ts)} · ${newest.ok ? "ok" : "failed"}`;
        })()} icon={<Activity className="h-4 w-4" />} />
        <div className="rounded-lg border border-slate-200 bg-white p-3 flex items-center gap-3">
          <div className="rounded-md bg-slate-100 p-2 text-slate-600">
            <RefreshCw className="h-4 w-4" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs text-slate-500">Bench interval (sec)</div>
            <div className="flex gap-1 items-center mt-0.5">
              <input
                type="number"
                value={intervalDraft}
                onChange={(e) => setIntervalDraft(e.target.value)}
                className="w-24 px-1.5 py-0.5 text-sm border border-slate-300 rounded font-mono tabular-nums"
              />
              <Button
                size="sm"
                disabled={
                  savingInterval ||
                  intervalDraft === "" ||
                  Number(intervalDraft) === interval
                }
                onClick={async () => {
                  setSavingInterval(true);
                  try {
                    const base = getApiBaseUrl();
                    const auth = await authHeader();
                    await fetch(
                      `${base}/api/v1/settings/runtime/BENCHMARK_INTERVAL_SECONDS`,
                      {
                        method: "PATCH",
                        headers: { ...auth, "Content-Type": "application/json" },
                        body: JSON.stringify({ value: Number(intervalDraft) }),
                      },
                    );
                    setIntervalState(Number(intervalDraft));
                  } finally {
                    setSavingInterval(false);
                  }
                }}
              >
                {savingInterval ? "..." : "Save"}
              </Button>
            </div>
            <div className="text-[10px] text-slate-400 mt-0.5">
              {interval ? `≈${Math.round(interval / 60)}min` : ""} · live, no
              restart
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-700 text-left">
            <tr>
              <th className="px-3 py-2">Provider</th>
              <th className="px-3 py-2">Model</th>
              <th className="px-3 py-2 text-right">IQ</th>
              <th className="px-3 py-2 text-right">TPS</th>
              <th className="px-3 py-2 text-right">Latency</th>
              <th className="px-3 py-2 text-right">In / Out / Total</th>
              <th className="px-3 py-2 text-right">Last bench</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.length === 0 && !loading && (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-slate-500">
                  No models curated. Add models in{" "}
                  <a href="/providers" className="text-blue-600 underline">
                    /providers
                  </a>{" "}
                  to see benchmarks.
                </td>
              </tr>
            )}
            {visibleRows.map((row) => (
              <tr
                key={`${row.provider}::${row.model}`}
                className={`border-t border-slate-100 ${!row.online ? "opacity-50" : ""}`}
              >
                <td className="px-3 py-2 flex items-center gap-2">
                  {row.online ? (
                    <Cloud className="h-3.5 w-3.5 text-emerald-500" />
                  ) : (
                    <CloudOff className="h-3.5 w-3.5 text-slate-400" />
                  )}
                  <span className="font-medium">{row.provider}</span>
                </td>
                <td className="px-3 py-2 font-mono text-xs">{row.model}</td>
                <td className="px-3 py-2 text-right">
                  {row.meta?.iq_score != null ? (
                    <IqBadge score={row.meta.iq_score} detail={row.meta.iq_detail} />
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.bench?.tps != null ? row.bench.tps.toFixed(1) : "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.bench?.duration_ms != null ? `${row.bench.duration_ms}ms` : "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600">
                  {row.bench
                    ? `${fmtNum(row.bench.prompt_tokens)} / ${fmtNum(row.bench.completion_tokens)} / ${fmtNum(row.bench.total_tokens)}`
                    : "—"}
                </td>
                <td className="px-3 py-2 text-right text-slate-500">
                  {fmtAgo(row.bench?.ts || null)}
                </td>
                <td className="px-3 py-2">
                  {row.meta?.disabled ? (
                    <span className="text-amber-700 text-xs">
                      disabled: {row.meta?.disabled_reason || ""}
                    </span>
                  ) : row.bench?.error ? (
                    <span className="text-red-600 text-xs">{row.bench.error.slice(0, 40)}</span>
                  ) : row.bench?.ok ? (
                    <span className="text-emerald-600 text-xs">ok</span>
                  ) : row.online ? (
                    <span className="text-slate-400 text-xs">pending</span>
                  ) : (
                    <span className="text-slate-400 text-xs">skipped (offline)</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4 text-xs text-slate-600 space-y-2">
        <p>
          <strong>How it works:</strong>{" "}
          {loopEnabled === false ? (
            <span className="text-amber-700">
              The scheduled loop is currently <strong>disabled</strong>{" "}
              (<code>BENCHMARK_ENABLED=false</code>) — each cycle would post a real
              generation per online provider × curated model, which steals GPU decode
              from live agents.
            </span>
          ) : (
            <>
              Benchmark loop runs every <code>BENCHMARK_INTERVAL_SECONDS</code> against
              every curated model on every <strong>online</strong> provider.
            </>
          )}{" "}
          Sends a fixed prompt (&quot;why is the sky blue&quot;), measures TPS, latency,
          token counts. Records to <code>provider_benchmark</code> table.
        </p>
        <p>
          <strong>IQ eval</strong> (5-section knowledge test: math, logic, code, json,
          trivia; 0–100) lives in <code>~/.openclaw/openclaw.mc.json</code> sidecar.
          Triggered manually via <em>Run IQ eval</em> button. Skips offline providers.
        </p>
        <p>
          <strong>Auto-disable:</strong> 3 benchmark failures in 5 min → model marked
          disabled in sidecar + skipped in subsequent passes. Owner can re-enable it in{" "}
          <a href="/providers" className="text-blue-600 underline">
            /providers
          </a>
          .
        </p>
      </div>
    </div>
  );
}

function StatBox({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 flex items-center gap-3">
      <div className="rounded-md bg-slate-100 p-2 text-slate-600">{icon}</div>
      <div>
        <div className="text-xs text-slate-500">{label}</div>
        <div className="text-lg font-semibold tabular-nums">{value}</div>
      </div>
    </div>
  );
}

function IqBadge({ score, detail }: { score: number; detail?: Record<string, number> }) {
  const tier = score >= 80 ? "emerald" : score >= 50 ? "amber" : "rose";
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold bg-${tier}-100 text-${tier}-700`}
      title={detail ? Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join("\n") : ""}
    >
      <Award className="h-3 w-3" />
      {score}
    </span>
  );
}

export default function BenchmarkPage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="p-8 text-center">
          <SignInButton mode="modal">
            <Button>Sign in to view benchmarks</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <main className="flex-1 overflow-y-auto bg-slate-50 p-6">
          <BenchmarkContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
