"use client";

/** Live department structure and agent roster. */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Bot,
  Boxes,
  Building2,
  Layers,
  Network,
  Sparkles,
} from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

interface Agent {
  id: string;
  name: string;
  department: string;
  status: string;
  label: string;
  last_active: number | null;
  last_message: string;
  heartbeat_interval: string;
  model: string;
  role?: string;
  fallbacks?: string[];
  skills?: string[];
  workspace?: string;
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

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const t = getLocalAuthToken();
    if (t) h["Authorization"] = `Bearer ${t}`;
  }
  return h;
}

const DEPT_ICONS: Record<string, string> = {
  management: "🏛️",
  product: "📦",
  "project-management": "📋",
  development: "⚙️",
  operations: "🔧",
  security: "🛡️",
  research: "🔬",
  documentation: "📚",
  system: "🖥️",
};

const DEPT_DESCRIPTIONS: Record<string, string> = {
  management: "Board leadership and cross-team coordination.",
  "project-management": "Project planning and delivery coordination.",
  product:
    "Product managers + owners — define WHAT to build (roadmap, requirements). Separate from the build-focused development pool.",
  development: "Software architecture and quality assurance.",
  operations: "SRE, DevOps, platform reliability, and networking.",
  documentation:
    "Technical writers + knowledge managers — wiki, journal, and owner-facing docs.",
  security: "Security review and penetration testing.",
  research: "Reverse engineering and binary analysis.",
  system: "System control agents.",
};

function StatusDot({ status }: { status: string }) {
  const c =
    status === "working" || status === "active"
      ? "bg-emerald-500"
      : status === "heartbeat"
        ? "bg-yellow-400"
        : status === "problem"
          ? "bg-red-500"
          : status === "standby"
            ? "bg-slate-300"
            : "bg-slate-200";
  return (
    <span
      className={cn(
        "inline-block h-1.5 w-1.5 rounded-full mr-1.5",
        c,
        (status === "working" || status === "active" || status === "problem") &&
          "animate-pulse",
      )}
    />
  );
}

function AgentChip({ agent }: { agent: Agent }) {
  const variantTag = (() => {
    for (const t of [
      " Senior",
      "-vllm",
      "-ollama",
      "-cloud",
      "-hauhau",
      "-Alpha",
      "-Beta",
      "-Gamma",
      "-Delta",
    ]) {
      if (agent.name.includes(t)) return t.trim();
    }
    return null;
  })();

  return (
    <div
      className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/60 px-2.5 py-1.5 text-xs hover:shadow-sm hover:-translate-y-0.5 transition-all"
      title={[
        agent.name,
        agent.role && agent.role !== agent.name ? `role: ${agent.role}` : "",
        agent.model ? `model: ${agent.model}` : "",
        agent.fallbacks?.length
          ? `fallback: ${agent.fallbacks.join(" → ")}`
          : "",
        agent.skills?.length ? `skills: ${agent.skills.join(", ")}` : "",
        `status: ${agent.label}`,
      ]
        .filter(Boolean)
        .join("\n")}
    >
      <div className="flex items-center gap-1.5">
        <StatusDot status={agent.status} />
        <span className="font-medium text-slate-800 dark:text-slate-100 truncate">
          {agent.name}
        </span>
        {variantTag && (
          <span className="ml-auto text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-300 font-mono">
            {variantTag}
          </span>
        )}
      </div>
      {agent.role && agent.role !== agent.name && (
        <div className="text-[10px] italic text-slate-500 dark:text-slate-400 mt-0.5 truncate">
          {agent.role}
        </div>
      )}
      {agent.model && (
        <div className="text-[9px] font-mono text-slate-400 dark:text-slate-500 truncate mt-0.5">
          {agent.model.replace(/^omniroute\//, "").replace(/^vllm\//, "")}
        </div>
      )}
    </div>
  );
}

function DepartmentBlock({
  deptId,
  dept,
}: {
  deptId: string;
  dept: Department;
}) {
  const icon = DEPT_ICONS[deptId] ?? "🏢";
  const desc = DEPT_DESCRIPTIONS[deptId] ?? "";
  return (
    <section
      className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4"
      style={{ borderLeft: `4px solid ${dept.color}` }}
    >
      <header className="flex items-center gap-2 mb-2">
        <span className="text-lg leading-none">{icon}</span>
        <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100 uppercase tracking-wide">
          {dept.label}
        </h3>
        <span className="ml-auto text-[10px] text-slate-500 dark:text-slate-400 font-mono">
          {dept.agents.length} agent{dept.agents.length === 1 ? "" : "s"}
        </span>
      </header>
      {desc && (
        <p className="text-xs text-slate-600 dark:text-slate-300 mb-3 leading-snug">
          {desc}
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
        {dept.agents.length === 0 ? (
          <div className="text-xs text-slate-400 dark:text-slate-500 italic col-span-full">
            no agents currently assigned to this department
          </div>
        ) : (
          dept.agents
            .slice()
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((a) => <AgentChip key={a.id} agent={a} />)
        )}
      </div>
    </section>
  );
}

// ── Page ──────────────────────────────────────────────────────────

export default function OrgChartPage() {
  const { isSignedIn } = useAuth();
  const [data, setData] = useState<OfficeData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch(`${getApiBaseUrl()}/api/v1/office/agents`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchData();
    const id = setInterval(() => void fetchData(), 30_000);
    return () => clearInterval(id);
  }, [fetchData]);

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut>
          <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8">
            <Network className="h-12 w-12 text-slate-300" />
            <p className="text-slate-500 text-lg font-medium">
              Sign in to view the organization chart
            </p>
            <SignInButton />
          </div>
        </SignedOut>
      </DashboardShell>
    );
  }

  const departments = data ? Object.entries(data.departments) : [];

  return (
    <DashboardShell>
      <SignedIn>
        <DashboardSidebar />
        <main className="overflow-y-auto bg-slate-50 dark:bg-slate-950 p-6">
          <div className="max-w-6xl mx-auto space-y-6">
            {/* Header */}
            <header className="flex items-center gap-3">
              <Network className="h-6 w-6 text-slate-700 dark:text-slate-200" />
              <div>
                <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
                  Organization Chart
                </h1>
                <p className="text-sm text-slate-500 dark:text-slate-400">
                  Live department structure and agent roster.
                </p>
              </div>
              {loading && (
                <span className="ml-auto text-xs text-slate-400 dark:text-slate-500 animate-pulse">
                  refreshing…
                </span>
              )}
              {error && (
                <span className="ml-auto text-xs text-rose-600 dark:text-rose-400">
                  error: {error}
                </span>
              )}
            </header>

            {/* Summary bar */}
            {data && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <SummaryCard
                  icon={<Bot className="h-4 w-4" />}
                  label="Total agents"
                  value={data.total.toString()}
                />
                <SummaryCard
                  icon={<Sparkles className="h-4 w-4 text-emerald-500" />}
                  label="Active right now"
                  value={data.active_count.toString()}
                />
                <SummaryCard
                  icon={<AlertTriangle className="h-4 w-4 text-rose-500" />}
                  label="In trouble"
                  value={data.problem_count.toString()}
                  accent={data.problem_count > 0 ? "danger" : undefined}
                />
                <SummaryCard
                  icon={<Building2 className="h-4 w-4" />}
                  label="Departments"
                  value={departments
                    .filter(([, d]) => d.agents.length > 0)
                    .length.toString()}
                />
              </div>
            )}

            {/* Department roster */}
            <div>
              <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200 uppercase tracking-wider mb-3 flex items-center gap-2">
                <Building2 className="h-4 w-4" />
                Departments + Roster
              </h2>
              <div className="space-y-4">
                {departments
                  .filter(([, d]) => d.agents.length > 0)
                  .map(([id, dept]) => (
                    <DepartmentBlock key={id} deptId={id} dept={dept} />
                  ))}
                {departments.filter(([, d]) => d.agents.length === 0).length >
                  0 && (
                  <div className="text-xs text-slate-400 dark:text-slate-500 italic">
                    Empty departments (
                    {departments
                      .filter(([, d]) => d.agents.length === 0)
                      .map(([id]) => id)
                      .join(", ")}
                    ) hidden — no agents currently assigned.
                  </div>
                )}
              </div>
            </div>

            {/* Live operational views */}
            <section>
              <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200 uppercase tracking-wider mb-3 flex items-center gap-2">
                <Layers className="h-4 w-4" />
                Live operational views
              </h2>
              <p className="text-xs text-slate-600 dark:text-slate-300 mb-3">
                Routing, automation, reviews, and runtime settings change
                independently of this roster. Use their live views rather than a
                static runbook.
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <OperationalLink
                  href="/pipeline"
                  label="Pipeline"
                  description="Current queue, watchdog state, and ballots."
                />
                <OperationalLink
                  href="/ranking"
                  label="Ranking"
                  description="Current routing and harness policy."
                />
                <OperationalLink
                  href="/review"
                  label="Review"
                  description="Items requiring owner intervention."
                />
                <OperationalLink
                  href="/tests"
                  label="Tests"
                  description="Current automation evidence and failures."
                />
                <OperationalLink
                  href="/settings/runtime"
                  label="Runtime settings"
                  description="Enabled flags and thresholds."
                />
              </div>
            </section>

            {/* Footer reference */}
            <footer className="text-xs text-slate-500 dark:text-slate-400 border-t border-slate-200 dark:border-slate-700 pt-4">
              <Boxes className="h-3.5 w-3.5 inline mr-1" />
              Roster live from <code>GET /api/v1/office/agents</code> (refreshes
              30s). For per-agent detail, hover any agent chip.
            </footer>
          </div>
        </main>
      </SignedIn>
    </DashboardShell>
  );
}

function OperationalLink({
  href,
  label,
  description,
}: {
  href: string;
  label: string;
  description: string;
}) {
  return (
    <Link
      href={href}
      className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3 hover:border-slate-400 dark:hover:border-slate-500 transition-colors"
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
        {label}
        <span aria-hidden="true" className="ml-auto text-slate-400">
          →
        </span>
      </div>
      <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
        {description}
      </p>
    </Link>
  );
}

function SummaryCard({
  icon,
  label,
  value,
  accent,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  accent?: "danger";
}) {
  return (
    <div
      className={cn(
        "rounded-xl border p-3",
        accent === "danger"
          ? "border-rose-200 dark:border-rose-700 bg-rose-50 dark:bg-rose-950/30"
          : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900",
      )}
    >
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400 font-semibold">
        {icon}
        {label}
      </div>
      <div className="text-2xl font-bold text-slate-800 dark:text-slate-100 mt-1">
        {value}
      </div>
    </div>
  );
}
