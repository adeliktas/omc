"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, RefreshCw, Building2, Wifi } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { cn } from "@/lib/utils";

type AgentStatus = "working" | "active" | "heartbeat" | "idle" | "standby" | "problem" | "offline";

interface Agent {
  id: string;
  name: string;
  department: string;
  status: AgentStatus;
  label: string;
  last_active: number | null;
  last_message: string;
  heartbeat_interval: string;
  model: string;
  // T73 (2026-05-15): specialization metadata pulled from openclaw.json
  // identity_profile + per-agent model block. Surfaces what each agent
  // knows, what fallback chain it uses, and what tools it's tagged with.
  role?: string;
  fallbacks?: string[];
  skills?: string[];
  workspace?: string;
  db_id?: string;
  // T105 Phase 2 (2026-05-25): tier-based routing
  tier?: "planner" | "executor" | "verifier" | "mixed";
}

interface Department {
  label: string;
  color: string;
  agents: Agent[];
}

interface OfficeData {
  departments: Record<string, Department>;
  agents: Agent[];
  total: number;
  problem_count: number;
  active_count: number;
}

const STATUS_CONFIG: Record<AgentStatus, { dot: string; ring: string; label: string; pulse: boolean }> = {
  working:   { dot: "bg-emerald-500", ring: "ring-emerald-300", label: "Working",    pulse: true },
  active:    { dot: "bg-emerald-400", ring: "ring-emerald-200", label: "Active",     pulse: true },
  heartbeat: { dot: "bg-yellow-400",  ring: "ring-yellow-200",  label: "Idle Loop",  pulse: false },
  idle:      { dot: "bg-slate-400",   ring: "ring-slate-200",   label: "Idle",       pulse: false },
  standby:   { dot: "bg-slate-300",   ring: "ring-slate-100",   label: "Standby",    pulse: false },
  problem:   { dot: "bg-red-500",     ring: "ring-red-300",     label: "Problem",    pulse: true },
  offline:   { dot: "bg-slate-200",   ring: "ring-slate-100",   label: "Offline",    pulse: false },
};

function relativeTime(ts: number | null): string {
  if (!ts) return "never";
  const diff = (Date.now() / 1000 - ts);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function AgentDesk({ agent }: { agent: Agent }) {
  const [hovered, setHovered] = useState(false);
  const cfg = STATUS_CONFIG[agent.status] ?? STATUS_CONFIG.offline;
  const shortName = agent.name.replace(/ (Engineer|Operator|Architect|Analyst)$/, "").slice(0, 14);

  // T104 (2026-05-24): click desk → jump to agent config page.
  // Prefer DB UUID (`db_id`) so navigation works even from the
  // openclaw.json-unreadable fallback path. When no UUID is known
  // (synthetic placeholder rows), fall back to non-clickable card.
  const dbId = agent.db_id || (agent.id?.startsWith("db:") ? "" : agent.id);
  const href = dbId ? `/agents/${dbId}` : null;

  const deskInner = (
    <div className={cn(
      "rounded-lg border-2 bg-white shadow-sm select-none transition-all",
      href ? "cursor-pointer hover:border-blue-400" : "cursor-default",
      agent.status === "problem" ? "border-red-300 shadow-red-100" : "border-slate-200",
      "hover:shadow-md",
    )} style={{ width: 88, minHeight: 72 }}>
      {/* Monitor */}
      <div className={cn(
        "mx-auto mt-2 rounded-sm border flex items-center justify-center text-[8px] font-mono text-slate-400",
        agent.status === "working" || agent.status === "active" ? "border-emerald-300 bg-emerald-50" : "border-slate-200 bg-slate-50",
      )} style={{ width: 52, height: 34 }}>
        {agent.status === "working" || agent.status === "active" ? (
          <span className="text-emerald-600 text-[7px]">● ● ●</span>
        ) : agent.status === "problem" ? (
          <span className="text-red-500 text-[10px]">!</span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </div>
      {/* Monitor stand */}
      <div className="mx-auto bg-slate-200 rounded-sm" style={{ width: 4, height: 6, marginTop: 1 }} />
      <div className="mx-auto bg-slate-200 rounded-sm" style={{ width: 16, height: 2 }} />

      {/* Status dot + name */}
      <div className="flex items-center gap-1 px-1.5 pb-1.5 pt-1 mt-0.5">
        <span className={cn(
          "h-2 w-2 rounded-full shrink-0",
          cfg.dot,
          cfg.pulse && "animate-pulse",
        )} />
        <span className="text-[9px] font-medium text-slate-600 leading-tight truncate">{shortName}</span>
      </div>
    </div>
  );

  return (
    <div
      className="relative"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {href ? (
        <Link href={href} className="block">
          {deskInner}
        </Link>
      ) : (
        deskInner
      )}

      {/* Chair below desk */}
      <div className="mx-auto mt-0.5 rounded-full bg-slate-200 border border-slate-300" style={{ width: 28, height: 10 }} />

      {/* Tooltip — T73 expanded with specialization metadata */}
      {hovered && (
        <div className="absolute z-50 bottom-full mb-2 left-1/2 -translate-x-1/2 bg-slate-900 dark:bg-slate-950 text-white rounded-lg shadow-xl p-2.5 text-xs w-64 pointer-events-none border border-slate-700">
          <div className="font-semibold">{agent.name}</div>
          {agent.role && agent.role !== agent.name && (
            <div className="text-slate-400 text-[10px] mt-0.5 italic">{agent.role}</div>
          )}
          <div className={cn("mt-0.5 font-medium", {
            "text-emerald-400": agent.status === "working" || agent.status === "active",
            "text-yellow-400": agent.status === "heartbeat",
            "text-red-400": agent.status === "problem",
            "text-slate-400": agent.status === "standby" || agent.status === "idle" || agent.status === "offline",
          })}>{agent.label}</div>
          {agent.tier && agent.tier !== "mixed" && (
            <div className="text-[10px] mt-1">
              <span className={
                "inline-flex items-center px-1.5 py-0.5 rounded font-medium " + (
                  agent.tier === "planner"
                    ? "bg-indigo-500/30 text-indigo-200"
                    : agent.tier === "verifier"
                      ? "bg-emerald-500/30 text-emerald-200"
                      : "bg-amber-500/30 text-amber-200"
                )
              }>
                tier: {agent.tier}
              </span>
            </div>
          )}
          {agent.model && (
            <div className="text-slate-300 text-[10px] mt-1 font-mono">
              <span className="text-slate-500">primary:</span> {agent.model.replace(/^omniroute\//, "")}
            </div>
          )}
          {agent.fallbacks && agent.fallbacks.length > 0 && (
            <div className="text-slate-400 text-[10px] mt-0.5 font-mono">
              <span className="text-slate-500">fallback:</span> {agent.fallbacks.join(" → ")}
            </div>
          )}
          {agent.skills && agent.skills.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {agent.skills.map((s) => (
                <span key={s} className="px-1.5 py-0.5 rounded bg-slate-700/60 text-[9px] text-slate-200">
                  {s}
                </span>
              ))}
            </div>
          )}
          <div className="text-slate-400 text-[10px] mt-1 border-t border-slate-700 pt-1">
            Heartbeat: {agent.heartbeat_interval} · Last: {relativeTime(agent.last_active)}
          </div>
          {agent.last_message && (
            <div className="text-slate-300 text-[10px] mt-1 line-clamp-2">{agent.last_message}</div>
          )}
          {href && (
            <div className="text-blue-300 text-[10px] mt-1 italic">
              Click to view config
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const DEPT_ICONS: Record<string, string> = {
  "management":          "🏛️",
  "product":             "📦",
  "project-management":  "📋",
  "development":         "⚙️",
  "operations":          "🔧",
  "security":            "🛡️",
  "research":            "🔬",
  "documentation":       "📚",
  "system":              "🖥️",
};

function DepartmentRoom({ deptId, dept }: { deptId: string; dept: Department }) {
  const hasProblems = dept.agents.some(a => a.status === "problem");
  const icon = DEPT_ICONS[deptId] ?? "🏢";

  return (
    <div className={cn(
      "relative rounded-2xl border-2 p-3 transition-all",
      hasProblems ? "border-red-300 bg-red-50/40" : "border-slate-200 bg-white/60",
    )} style={{ borderLeft: `4px solid ${dept.color}` }}>
      {/* Room label */}
      <div className="flex items-center gap-1.5 mb-3">
        <span className="text-base leading-none">{icon}</span>
        <span className="text-xs font-semibold text-slate-700 uppercase tracking-wide">{dept.label}</span>
        {hasProblems && (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-red-600 font-semibold bg-red-100 rounded-full px-1.5 py-0.5">
            <AlertTriangle className="h-2.5 w-2.5" /> Alert
          </span>
        )}
        <span className="ml-auto text-[10px] text-slate-400">{dept.agents.length} agent{dept.agents.length !== 1 ? "s" : ""}</span>
      </div>

      {/* Desks */}
      {dept.agents.length === 0 ? (
        <div className="text-[10px] text-slate-400 text-center py-3">No agents assigned</div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {dept.agents.map(agent => (
            <AgentDesk key={agent.id} agent={agent} />
          ))}
        </div>
      )}

      {/* Floor pattern (subtle) */}
      <div className="absolute inset-0 rounded-2xl opacity-[0.03] pointer-events-none"
        style={{
          backgroundImage: "repeating-linear-gradient(0deg, #000 0px, #000 1px, transparent 1px, transparent 24px), repeating-linear-gradient(90deg, #000 0px, #000 1px, transparent 1px, transparent 24px)",
        }}
      />
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-3 text-[10px]">
      {(Object.entries(STATUS_CONFIG) as [AgentStatus, typeof STATUS_CONFIG[AgentStatus]][]).map(([status, cfg]) => (
        <div key={status} className="flex items-center gap-1">
          <span className={cn("h-2 w-2 rounded-full", cfg.dot)} />
          <span className="text-slate-500">{cfg.label}</span>
        </div>
      ))}
    </div>
  );
}

function OfficeInner() {
  const { isSignedIn } = useAuth();
  const [data, setData] = useState<OfficeData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    if (!isSignedIn) return;
    try {
      const headers: Record<string, string> = {};
      if (isLocalAuthMode()) {
        const t = getLocalAuthToken();
        if (t) headers["Authorization"] = `Bearer ${t}`;
      }
      const res = await fetch(`${getApiBaseUrl()}/api/v1/office/agents`, { headers, cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json() as OfficeData);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [isSignedIn]);

  useEffect(() => {
    void load();
    intervalRef.current = setInterval(() => void load(), 120_000);  // 120s (was 15s)
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [load]);

  return (
    <main className="flex-1 overflow-y-auto bg-gradient-to-br from-slate-100 to-slate-200">
      <div className="p-4 md:p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <Building2 className="h-5 w-5 text-slate-500" />
            <h1 className="text-lg font-semibold text-slate-800">Agent Office</h1>
            {data && (
              <div className="flex items-center gap-2 ml-2">
                {data.problem_count > 0 && (
                  <span className="flex items-center gap-1 text-[10px] bg-red-100 text-red-700 border border-red-200 rounded-full px-2 py-0.5 font-semibold">
                    <AlertTriangle className="h-3 w-3" /> {data.problem_count} alert{data.problem_count > 1 ? "s" : ""}
                  </span>
                )}
                {data.active_count > 0 && (
                  <span className="flex items-center gap-1 text-[10px] bg-emerald-100 text-emerald-700 border border-emerald-200 rounded-full px-2 py-0.5 font-semibold">
                    <Wifi className="h-3 w-3" /> {data.active_count} active
                  </span>
                )}
                <span className="text-[10px] text-slate-400">{data.total} agents total</span>
              </div>
            )}
          </div>
          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={cn("h-3 w-3 mr-1", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>

        <Legend />

        {error && (
          <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-xs text-red-600">{error}</div>
        )}

        {loading && !data ? (
          <div className="text-sm text-slate-400 text-center py-12 mt-4">Loading office…</div>
        ) : data ? (
          <>
            {/* Building floor plan */}
            <div className="mt-4 grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
              {Object.entries(data.departments).map(([deptId, dept]) => (
                <DepartmentRoom key={deptId} deptId={deptId} dept={dept} />
              ))}
            </div>

            {/* Building exterior decoration */}
            <div className="mt-6 rounded-2xl border border-slate-200 bg-white/40 p-4">
              <div className="flex items-center gap-2 mb-3">
                <span className="text-sm">🏢</span>
                <span className="text-xs font-semibold text-slate-600 uppercase tracking-wide">DN Systems HQ — Agent Roster</span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-1.5">
                {data.agents.map(agent => {
                  const cfg = STATUS_CONFIG[agent.status] ?? STATUS_CONFIG.offline;
                  return (
                    <div key={agent.id} className="flex items-center gap-1.5 text-[10px] text-slate-600 bg-white rounded-lg px-2 py-1.5 border border-slate-100">
                      <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", cfg.dot, cfg.pulse && "animate-pulse")} />
                      <span className="truncate font-medium">{agent.name}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        ) : null}
      </div>
    </main>
  );
}

export default function OfficePage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 p-10 text-center">
          <p className="text-sm text-muted">Sign in to view the agent office.</p>
          <SignInButton mode="modal" forceRedirectUrl="/office" signUpForceRedirectUrl="/office">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <OfficeInner />
      </SignedIn>
    </DashboardShell>
  );
}
