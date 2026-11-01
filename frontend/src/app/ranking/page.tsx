"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Ban, CheckCircle2, Cloud, CloudOff, Coins, GripVertical, HardDrive,
  RefreshCw, Save, Settings2, ShieldAlert, Trophy, XCircle, Zap,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";

interface UsageWindow {
  seconds: number;
  used: number;
  limit: number;
  unit: "tokens" | "credits";
  used_pct: number;
  remaining: number | null;
}

interface ObservedUsage {
  calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  total_tokens?: number;
  cost_usd?: number;
  last_at?: string;
  source?: string;
}

interface RankingEntry {
  provider: string;
  model: string;
  level: number;
  rank: number;
  provider_priority: number;
  max_complexity: number;
  cost_class: string;
  iq_score: number | null;
  disabled: boolean;
  fully_trusted: boolean;
  disabled_reason: string | null;
  budget_type: string;
  budget: Record<string, unknown>;
  pricing: Record<string, number> | null;
  provider_status: string;
  execution: "agent_harness" | "model_endpoint";
  usage: UsageWindow[];
  observed_usage: ObservedUsage;
  unavailable_until: string | null;
  availability_reason: string | null;
  eligible: boolean;
  block_reason: string | null;
  picked_for: number[];
  harness_picked_for: number[];
}

interface Pick {
  provider: string;
  model: string;
  level: number;
  iq_score: number | null;
}

interface RankingBoard {
  levels: { level: number; entries: RankingEntry[] }[];
  complexity_picks: Record<string, Pick | null>;
  agent_harness_picks: Record<string, Pick | null>;
  max_level_for_complexity: Record<string, number>;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}
const apiUrl = (path: string) => `${getApiBaseUrl()}${path}`;

const LEVEL_LABELS: Record<number, string> = {
  1: "Level 1 — Frontier / Mastermind",
  2: "Level 2 — Strong Planner",
  3: "Level 3 — Mid",
  4: "Level 4 — Worker",
  5: "Level 5 — Lightweight",
};

function CostIcon({ value }: { value: string }) {
  if (value === "local") return <HardDrive className="h-4 w-4 text-emerald-400" />;
  if (value === "credits") return <Coins className="h-4 w-4 text-amber-400" />;
  return <Cloud className="h-4 w-4 text-sky-400" />;
}

function windowLabel(seconds: number): string {
  if (seconds % 604800 === 0) return `${seconds / 604800}w`;
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  return `${seconds}s`;
}

function localDateTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function EntryCard({
  entry, dragging, onDragStart, onDropOn, onSave,
}: {
  entry: RankingEntry;
  dragging: boolean;
  onDragStart: () => void;
  onDropOn: () => void;
  onSave: (entry: RankingEntry, patch: Record<string, unknown>) => Promise<void>;
}) {
  const activeLevels = entry.execution === "agent_harness"
    ? entry.harness_picked_for
    : entry.picked_for;
  const [priority, setPriority] = useState(entry.provider_priority);
  const [maxComplexity, setMaxComplexity] = useState(entry.max_complexity);
  const [costClass, setCostClass] = useState(entry.cost_class);
  const [disabled, setDisabled] = useState(entry.disabled);
  const [fullyTrusted, setFullyTrusted] = useState(entry.fully_trusted === true);
  const [until, setUntil] = useState(localDateTime(entry.unavailable_until));
  const [reason, setReason] = useState(entry.availability_reason ?? "");
  const [budget, setBudget] = useState(JSON.stringify(entry.budget ?? {}, null, 2));
  const [pricing, setPricing] = useState(JSON.stringify(entry.pricing ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPriority(entry.provider_priority);
    setMaxComplexity(entry.max_complexity);
    setCostClass(entry.cost_class);
    setDisabled(entry.disabled);
    setFullyTrusted(entry.fully_trusted === true);
    setUntil(localDateTime(entry.unavailable_until));
    setReason(entry.availability_reason ?? "");
    setBudget(JSON.stringify(entry.budget ?? {}, null, 2));
    setPricing(JSON.stringify(entry.pricing ?? {}, null, 2));
  }, [entry]);

  const save = async () => {
    try {
      await onSave(entry, {
        provider_priority: priority,
        max_complexity: maxComplexity,
        cost_class: costClass,
        disabled,
        fully_trusted: fullyTrusted,
        unavailable_until: until ? new Date(until).toISOString() : null,
        availability_reason: reason || null,
        budget: JSON.parse(budget || "{}"),
        pricing: JSON.parse(pricing || "{}"),
      });
      setError(null);
    } catch (caught) {
      setError(String(caught));
    }
  };

  const observed = entry.observed_usage ?? {};
  const online = entry.provider_status === "online" || entry.provider_status === "harness";
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragOver={(event) => event.preventDefault()}
      onDrop={onDropOn}
      className={cn(
        "w-80 shrink-0 rounded-xl border p-3 shadow-sm transition",
        dragging && "opacity-40",
        entry.eligible
          ? activeLevels.length
            ? "border-emerald-500/60 bg-emerald-500/5"
            : "border-slate-700 bg-slate-800/50"
          : "border-red-900/60 bg-red-950/20",
      )}
    >
      <div className="flex items-center gap-2">
        <GripVertical className="h-4 w-4 cursor-grab text-slate-500" />
        <span className="flex-1 truncate font-mono text-sm" title={`${entry.provider}/${entry.model}`}>
          {entry.model}
        </span>
        <CostIcon value={entry.cost_class} />
        {entry.iq_score != null && <span className="rounded bg-slate-700 px-1 text-xs">IQ {entry.iq_score}</span>}
      </div>
      <div className="mt-1 flex items-center gap-2 text-xs text-slate-500">
        <span>{entry.provider}</span>
        <span className="rounded bg-slate-900 px-1.5 py-0.5">
          {entry.execution === "agent_harness" ? "coding harness" : "API endpoint"}
        </span>
        <span title="provider priority">P{entry.provider_priority}</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        {online ? (
          <span className="flex items-center gap-1 text-emerald-400"><CheckCircle2 className="h-3 w-3" /> {entry.execution === "agent_harness" ? "CLI lane (auth unchecked)" : "ready"}</span>
        ) : (
          <span className="flex items-center gap-1 text-slate-500"><CloudOff className="h-3 w-3" /> {entry.provider_status}</span>
        )}
        {entry.disabled && <span className="flex items-center gap-1 text-red-400"><Ban className="h-3 w-3" /> disabled</span>}
        {activeLevels.length > 0 && entry.eligible && (
          <span className="flex items-center gap-1 text-emerald-300"><Zap className="h-3 w-3" /> task L{activeLevels.join(", L")}</span>
        )}
      </div>

      {!entry.eligible && (
        <div className="mt-2 rounded bg-red-950/50 px-2 py-1 text-xs text-red-300">
          {entry.block_reason ?? "unavailable"}
          {entry.unavailable_until && ` · retry ${new Date(entry.unavailable_until).toLocaleString()}`}
        </div>
      )}

      {entry.usage.map((window) => (
        <div key={window.seconds} className="mt-2">
          <div className="flex justify-between text-[10px] text-slate-400">
            <span>{windowLabel(window.seconds)} {window.unit === "credits" ? "$" : "tokens"}</span>
            <span>{window.used.toLocaleString()} / {window.limit.toLocaleString()}</span>
          </div>
          <div className="h-1.5 rounded bg-slate-700">
            <div
              className={cn("h-full rounded", window.used_pct >= 100 ? "bg-red-500" : window.used_pct >= 80 ? "bg-amber-500" : "bg-emerald-500")}
              style={{ width: `${Math.min(window.used_pct, 100)}%` }}
            />
          </div>
        </div>
      ))}

      {(observed.calls ?? 0) > 0 && (
        <div className="mt-2 rounded bg-slate-900/70 p-2 text-[11px] text-slate-400">
          <div>{observed.calls} reported calls · {(observed.total_tokens ?? 0).toLocaleString()} reported tokens · {observed.cost_usd ? `$${observed.cost_usd.toFixed(4)} CLI-estimated USD (not billed spend)` : "USD charge unknown"}</div>
          <div>{(observed.input_tokens ?? 0).toLocaleString()} in · {(observed.output_tokens ?? 0).toLocaleString()} out · source {observed.source}</div>
        </div>
      )}

      <details className="mt-3 rounded border border-slate-700 bg-slate-900/60 p-2" onToggle={(event) => { event.currentTarget.parentElement?.setAttribute("draggable", event.currentTarget.open ? "false" : "true"); }}>
        <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-slate-300">
          <Settings2 className="h-3.5 w-3.5" /> Routing, quota, budget and pricing
        </summary>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
          <label>Provider priority
            <input className="mt-1 w-full rounded bg-slate-800 p-1.5" type="number" min={0} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
          </label>
          <label>Maximum task level
            <select className="mt-1 w-full rounded bg-slate-800 p-1.5" value={maxComplexity} onChange={(e) => setMaxComplexity(Number(e.target.value))}>
              {[1, 2, 3, 4, 5].map((level) => <option key={level} value={level}>L{level}</option>)}
            </select>
          </label>
          <label>Cost class
            <select className="mt-1 w-full rounded bg-slate-800 p-1.5" value={costClass} onChange={(e) => setCostClass(e.target.value)}>
              <option value="credits">subscription / credits</option>
              <option value="free">free quota</option>
              <option value="local">local</option>
            </select>
          </label>
          <label className="flex items-end gap-2 pb-1">
            <input type="checkbox" checked={disabled} onChange={(e) => setDisabled(e.target.checked)} /> Disabled
          </label>
          <label className="col-span-2 flex items-center gap-2">
            <input type="checkbox" checked={fullyTrusted} onChange={(e) => setFullyTrusted(e.target.checked)} />
            Fully trusted (Jira read-only; pinned local Qwen lane also required)
          </label>
          <label className="col-span-2">Unavailable until
            <input className="mt-1 w-full rounded bg-slate-800 p-1.5" type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
          </label>
          <label className="col-span-2">Availability reason
            <input className="mt-1 w-full rounded bg-slate-800 p-1.5" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="quota exhausted, maintenance…" />
          </label>
          <label className="col-span-2">Budget JSON
            <textarea className="mt-1 h-20 w-full rounded bg-slate-800 p-1.5 font-mono" value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label className="col-span-2">Pricing JSON
            <textarea className="mt-1 h-20 w-full rounded bg-slate-800 p-1.5 font-mono" value={pricing} onChange={(e) => setPricing(e.target.value)} />
          </label>
        </div>
        {error && <div className="mt-2 text-xs text-red-400">{error}</div>}
        <button onClick={save} className="mt-2 flex items-center gap-1 rounded bg-sky-700 px-2 py-1 text-xs hover:bg-sky-600">
          <Save className="h-3 w-3" /> Save metadata
        </button>
      </details>
    </div>
  );
}

function PickStrip({ title, picks }: { title: string; picks: Record<string, Pick | null> }) {
  return (
    <div>
      <h2 className="mb-2 text-sm font-semibold text-slate-300">{title}</h2>
      <div className="flex flex-wrap gap-2">
        {[1, 2, 3, 4, 5].map((complexity) => {
          const pick = picks[String(complexity)];
          return (
            <div key={complexity} className={cn("min-w-36 rounded border p-2 text-xs", pick ? "border-emerald-700 bg-emerald-950/30" : "border-red-800 bg-red-950/30")}>
              <div className="text-slate-400">Task L{complexity}</div>
              {pick ? (
                <><div className="font-mono text-emerald-300">{pick.model}</div><div className="text-slate-500">{pick.provider}</div></>
              ) : (
                <div className="flex items-center gap-1 text-red-400"><XCircle className="h-3 w-3" /> no eligible model</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function RankingPage() {
  const { isSignedIn } = useAuth();
  const [board, setBoard] = useState<RankingBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState<{ provider: string; model: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch(apiUrl("/api/v1/ranking"), { headers: authHeaders() });
      if (!response.ok) throw new Error(`${response.status}`);
      setBoard(await response.json());
      setError(null);
    } catch (caught) {
      setError(String(caught));
    }
  }, []);

  useEffect(() => { if (isSignedIn) load(); }, [isSignedIn, load]);
  useEffect(() => {
    if (!isSignedIn) return;
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [isSignedIn, load]);

  const request = useCallback(async (path: string, method: string, body?: unknown) => {
    setSaving(true);
    try {
      const response = await fetch(apiUrl(path), {
        method,
        headers: authHeaders(),
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      await load();
    } finally {
      setSaving(false);
    }
  }, [load]);

  const moveTo = async (level: number, rank: number) => {
    if (!dragging) return;
    try {
      await request("/api/v1/ranking", "PUT", { moves: [{ ...dragging, level, rank }] });
    } finally {
      setDragging(null);
    }
  };

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut><div className="flex min-h-[60vh] flex-col items-center justify-center gap-4"><Trophy className="h-12 w-12" /><SignInButton /></div></SignedOut>
      </DashboardShell>
    );
  }

  return (
    <DashboardShell>
      <SignedIn>
        <DashboardSidebar />
        <main className="p-6">
          <div className="flex flex-wrap items-center gap-3">
            <Trophy className="h-6 w-6 text-amber-400" />
            <h1 className="text-2xl font-bold">Model Routing & Harness Control</h1>
            <button onClick={load} className="rounded p-1 hover:bg-slate-700" aria-label="refresh"><RefreshCw className="h-4 w-4" /></button>
            <button onClick={() => request("/api/v1/ranking/policy/cloud-first", "POST")} className="rounded bg-indigo-700 px-3 py-1.5 text-xs hover:bg-indigo-600">Apply cloud-first policy</button>
            {saving && <span className="text-xs text-slate-400">saving…</span>}
          </div>
          <p className="mt-1 max-w-5xl text-sm text-slate-400">
            Coding harnesses are selected by availability, provider priority, task complexity and model level. Temporary quota resets automatically remove a provider and restore it at the displayed time. API endpoints remain separate from subscription harnesses.
          </p>
          {error && <div className="mt-2 rounded bg-red-950/50 p-2 text-sm text-red-300">load error: {error}</div>}

          <div className="mt-5 grid gap-5 xl:grid-cols-2">
            <PickStrip title="Coding-agent execution now" picks={board?.agent_harness_picks ?? {}} />
            <PickStrip title="OmniRoute endpoint routing now" picks={board?.complexity_picks ?? {}} />
          </div>

          {board?.levels.map(({ level, entries }) => (
            <section
              key={level}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => { event.preventDefault(); moveTo(level, entries.length); }}
              className="mt-5 min-h-24 rounded-xl border border-slate-700 bg-slate-900/40 p-3"
            >
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-300">
                <ShieldAlert className={cn("h-4 w-4", level === 1 ? "text-amber-400" : "text-slate-500")} />
                {LEVEL_LABELS[level] ?? `Level ${level}`}
              </div>
              <div className="mt-2 flex gap-3 overflow-x-auto pb-2">
                {entries.length === 0 && <div className="text-xs text-slate-600">empty — drop a model here</div>}
                {entries.map((entry, index) => (
                  <div key={`${entry.provider}/${entry.model}`} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); event.stopPropagation(); moveTo(level, index); }}>
                    <EntryCard
                      entry={entry}
                      dragging={dragging?.model === entry.model && dragging?.provider === entry.provider}
                      onDragStart={() => setDragging({ provider: entry.provider, model: entry.model })}
                      onDropOn={() => moveTo(level, index)}
                      onSave={(target, patch) => request("/api/v1/ranking/entry", "PUT", { provider: target.provider, model: target.model, ...patch })}
                    />
                  </div>
                ))}
              </div>
            </section>
          ))}
        </main>
      </SignedIn>
      <SignedOut>{null}</SignedOut>
    </DashboardShell>
  );
}
