"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  BarChart3,
  Bot,
  Boxes,
  CheckCircle2,
  ClipboardList,
  Folder,
  Building2,
  LayoutGrid,
  Network,
  Settings,
  Store,
  Tags,
  Radio,
  AlertTriangle,
  Workflow,
  TestTube2,
  BookOpen,
  Cpu,
  SlidersHorizontal,
  Trophy,
  MessageSquare,
  Globe,
  Zap,
  CalendarDays,
  FolderKanban,
  BookMarked,
  Archive,
  ShieldCheck,
} from "lucide-react";

import { useAuth } from "@/auth/clerk";
import { ApiError } from "@/api/mutator";
import { useOrganizationMembership } from "@/lib/use-organization-membership";
import {
  type healthzHealthzGetResponse,
  useHealthzHealthzGet,
} from "@/api/generated/default/default";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";

function useTrafficBadge(isSignedIn: boolean) {
  const [newCount, setNewCount] = useState(0);
  const lastSeen = useRef<number>(0);
  const pathname = usePathname();

  useEffect(() => {
    // Safe initialization: only runs on client
    try {
      const stored = typeof window !== "undefined"
        ? window.localStorage.getItem("traffic_last_seen")
        : null;
      if (stored) lastSeen.current = parseInt(stored, 10);
    } catch { /* ignore localStorage errors */ }
  }, []);

  useEffect(() => {
    if (!isSignedIn) return;
    const poll = async () => {
      try {
        const headers: Record<string, string> = {};
        if (isLocalAuthMode()) {
          const t = getLocalAuthToken();
          if (t) headers["Authorization"] = `Bearer ${t}`;
        }
        const res = await fetch(`${getApiBaseUrl()}/api/v1/traffic/stats`, { headers });
        if (!res.ok) return;
        const data = (await res.json()) as { flow_count: number };
        const total = data.flow_count ?? 0;
        setNewCount(Math.max(0, total - lastSeen.current));
      } catch { /* ignore */ }
    };
    void poll();
    const id = setInterval(() => void poll(), 120_000);  // 120s (was 10s)
    return () => clearInterval(id);
  }, [isSignedIn]);

  useEffect(() => {
    if (pathname.startsWith("/traffic")) {
      fetch(`${getApiBaseUrl()}/api/v1/traffic/stats`, {
        headers: isLocalAuthMode() && getLocalAuthToken()
          ? { "Authorization": `Bearer ${getLocalAuthToken()}` }
          : {},
      })
        .then((r) => r.json())
        .then((d: { flow_count: number }) => {
          lastSeen.current = d.flow_count ?? 0;
          if (typeof window !== "undefined")
            window.localStorage.setItem("traffic_last_seen", String(lastSeen.current));
          setNewCount(0);
        })
        .catch(() => { /* ignore */ });
    }
  }, [pathname]);

  return newCount;
}

function useOfficeBadge(isSignedIn: boolean) {
  const [problemCount, setProblemCount] = useState(0);

  useEffect(() => {
    if (!isSignedIn) return;
    const poll = async () => {
      try {
        const headers: Record<string, string> = {};
        if (isLocalAuthMode()) {
          const t = getLocalAuthToken();
          if (t) headers["Authorization"] = `Bearer ${t}`;
        }
        const res = await fetch(`${getApiBaseUrl()}/api/v1/office/agents`, { headers });
        if (!res.ok) return;
        const data = (await res.json()) as { problem_count?: number };
        setProblemCount(data.problem_count ?? 0);
      } catch { /* ignore */ }
    };
    void poll();
    const id = setInterval(() => void poll(), 300_000);  // 300s (was 30s)
    return () => clearInterval(id);
  }, [isSignedIn]);

  return problemCount;
}

function useReviewBadge(isSignedIn: boolean) {
  const [pendingCount, setPendingCount] = useState(0);
  const pathname = usePathname();

  useEffect(() => {
    if (!isSignedIn) return;
    const poll = async () => {
      try {
        const headers: Record<string, string> = {};
        if (isLocalAuthMode()) {
          const t = getLocalAuthToken();
          if (t) headers["Authorization"] = `Bearer ${t}`;
        }
        const res = await fetch(`${getApiBaseUrl()}/api/v1/review/items?status=pending`, { headers });
        if (!res.ok) return;
        const data = (await res.json()) as unknown[];
        setPendingCount(Array.isArray(data) ? data.length : 0);
      } catch { /* ignore */ }
    };
    void poll();
    const id = setInterval(() => void poll(), 300_000);  // 300s (was 30s)
    return () => clearInterval(id);
  }, [isSignedIn, pathname]);

  return pendingCount;
}

export function DashboardSidebar() {
  const pathname = usePathname();
  const { isSignedIn } = useAuth();
  const trafficBadge = useTrafficBadge(Boolean(isSignedIn));
  const reviewBadge = useReviewBadge(Boolean(isSignedIn));
  const officeBadge = useOfficeBadge(Boolean(isSignedIn));
  const { isAdmin } = useOrganizationMembership(isSignedIn);
  const healthQuery = useHealthzHealthzGet<healthzHealthzGetResponse, ApiError>(
    {
      query: {
        refetchInterval: 300_000,  // 300s (was 30s)
        refetchOnMount: "always",
        retry: false,
      },
      request: { cache: "no-store" },
    },
  );

  const okValue = healthQuery.data?.data?.ok;
  const systemStatus: "unknown" | "operational" | "degraded" =
    okValue === true
      ? "operational"
      : okValue === false
        ? "degraded"
        : healthQuery.isError
          ? "degraded"
          : "unknown";
  const statusLabel =
    systemStatus === "operational"
      ? "Backend API reachable"
      : systemStatus === "unknown"
        ? "System status unavailable"
        : "Backend API unreachable";

  return (
    <aside className="fixed inset-y-0 left-0 z-40 flex w-[280px] -translate-x-full flex-col border-r border-slate-200 bg-white pt-16 shadow-lg transition-transform duration-200 ease-in-out [[data-sidebar=open]_&]:translate-x-0 md:relative md:inset-auto md:z-auto md:w-[260px] md:translate-x-0 md:pt-0 md:shadow-none md:transition-none">
      <div className="flex-1 px-3 py-4">
        <p className="px-3 text-xs font-semibold uppercase tracking-wider text-slate-500">
          Navigation
        </p>
        <nav className="mt-3 space-y-4 text-sm">
          <div>
                        <p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Fleet
            </p>
            <div className="space-y-1">
              <Link
                prefetch={false}
                href="/providers"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/providers")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Cpu className="h-4 w-4" />
                LLM Providers
              </Link>
              <Link
                prefetch={false}
                href="/ranking"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/ranking")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Trophy className="h-4 w-4" />
                Model Ranking
              </Link>
              <Link
                prefetch={false}
                href="/models"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/models")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <SlidersHorizontal className="h-4 w-4" />
                Models &amp; Roles
              </Link>
              <Link
                prefetch={false}
                href="/benchmark"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/benchmark")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Zap className="h-4 w-4" />
                Benchmark
              </Link>
              {isAdmin ? (
                <Link
                  prefetch={false}
                  href="/agents"
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                    pathname.startsWith("/agents")
                      ? "bg-blue-100 text-blue-800 font-medium"
                      : "hover:bg-slate-100",
                  )}
                >
                  <Bot className="h-4 w-4" />
                  Agents
                </Link>
              ) : null}
              <Link
                prefetch={false}
                href="/roles"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/roles")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <ShieldCheck className="h-4 w-4" />
                Roles
              </Link>
            </div>
<p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Overview
            </p>
            <div className="mt-1 space-y-1">
              <Link
                prefetch={false}
                href="/dashboard"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname === "/dashboard"
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <BarChart3 className="h-4 w-4" />
                Dashboard
              </Link>
              <Link
                prefetch={false}
                href="/activity"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/activity")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Activity className="h-4 w-4" />
                Live feed
              </Link>
              <Link
                prefetch={false}
                href="/office"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/office")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Building2 className="h-4 w-4" />
                Office
                {officeBadge > 0 && !pathname.startsWith("/office") && (
                  <span className="ml-auto flex items-center gap-0.5 text-[10px] bg-red-500 text-white rounded-full px-1.5 py-0.5 font-bold">
                    <AlertTriangle className="h-2.5 w-2.5" />{officeBadge}
                  </span>
                )}
              </Link>
              <Link
                prefetch={false}
                href="/org-chart"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/org-chart")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Network className="h-4 w-4" />
                Org Chart
              </Link>
              <Link
                prefetch={false}
                href="/traffic"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/traffic")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Radio className="h-4 w-4" />
                LLM Traffic
                {trafficBadge > 0 && !pathname.startsWith("/traffic") && (
                  <span className="ml-auto text-[10px] bg-blue-600 text-white rounded-full px-1.5 py-0.5 font-bold min-w-[18px] text-center">
                    {trafficBadge > 99 ? "99+" : trafficBadge}
                  </span>
                )}
              </Link>
              
              
              
              <Link
                prefetch={false}
                href="/chat"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/chat")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <MessageSquare className="h-4 w-4" />
                Chat
              </Link>
              <Link
                prefetch={false}
                href="/openclaw"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/openclaw")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Globe className="h-4 w-4" />
                OpenClaw
              </Link>
              <Link
                prefetch={false}
                href="/wiki"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/wiki")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <BookOpen className="h-4 w-4" />
                Wiki
              </Link>
              <Link
                prefetch={false}
                href="/calendar"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/calendar")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <CalendarDays className="h-4 w-4" />
                Calendar
              </Link>
              <Link
                prefetch={false}
                href="/projects"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/projects")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <FolderKanban className="h-4 w-4" />
                Projects
              </Link>
              <Link
                prefetch={false}
                href="/memory"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/memory")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <BookMarked className="h-4 w-4" />
                Memory
              </Link>
              <Link
                prefetch={false}
                href="/backups"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/backups")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Archive className="h-4 w-4" />
                Backups
              </Link>
              <Link
                prefetch={false}
                href="/review"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/review")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <ClipboardList className="h-4 w-4" />
                Owner Review
                {reviewBadge > 0 && !pathname.startsWith("/review") && (
                  <span className="ml-auto text-[10px] bg-yellow-500 text-white rounded-full px-1.5 py-0.5 font-bold min-w-[18px] text-center">
                    {reviewBadge > 99 ? "99+" : reviewBadge}
                  </span>
                )}
              </Link>
              <Link
                prefetch={false}
                href="/pipeline"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/pipeline")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Workflow className="h-4 w-4" />
                Pipeline
              </Link>
              <Link
                prefetch={false}
                href="/tests"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/tests")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <TestTube2 className="h-4 w-4" />
                Tests
              </Link>
            </div>
          </div>

          <div>
            <p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Boards
            </p>
            <div className="mt-1 space-y-1">
              <Link
                prefetch={false}
                href="/board-groups"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/board-groups")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Folder className="h-4 w-4" />
                Board groups
              </Link>
              <Link
                prefetch={false}
                href="/boards"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/boards")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <LayoutGrid className="h-4 w-4" />
                Boards
              </Link>
              <Link
                prefetch={false}
                href="/tags"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/tags")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Tags className="h-4 w-4" />
                Tags
              </Link>
              <Link
                prefetch={false}
                href="/approvals"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/approvals")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <CheckCircle2 className="h-4 w-4" />
                Approvals
              </Link>
              {isAdmin ? (
                <Link
                  prefetch={false}
                  href="/custom-fields"
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                    pathname.startsWith("/custom-fields")
                      ? "bg-blue-100 text-blue-800 font-medium"
                      : "hover:bg-slate-100",
                  )}
                >
                  <Settings className="h-4 w-4" />
                  Custom fields
                </Link>
              ) : null}
            </div>
          </div>

          <div>
            {isAdmin ? (
              <>
                <p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  Skills
                </p>
                <div className="mt-1 space-y-1">
                  <Link
                    prefetch={false}
                    href="/skills/marketplace"
                    className={cn(
                      "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                      pathname === "/skills" ||
                        pathname.startsWith("/skills/marketplace")
                        ? "bg-blue-100 text-blue-800 font-medium"
                        : "hover:bg-slate-100",
                    )}
                  >
                    <Store className="h-4 w-4" />
                    Marketplace
                  </Link>
                  <Link
                    prefetch={false}
                    href="/skills/packs"
                    className={cn(
                      "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                      pathname.startsWith("/skills/packs")
                        ? "bg-blue-100 text-blue-800 font-medium"
                        : "hover:bg-slate-100",
                    )}
                  >
                    <Boxes className="h-4 w-4" />
                    Packs
                  </Link>
                  <Link
                    prefetch={false}
                    href="/skills/installed"
                    className={cn(
                      "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                      pathname.startsWith("/skills/installed")
                        ? "bg-blue-100 text-blue-800 font-medium"
                        : "hover:bg-slate-100",
                    )}
                  >
                    <BookOpen className="h-4 w-4" />
                    Installed
                  </Link>
                </div>
              </>
            ) : null}
          </div>

          <div>
            <p className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Administration
            </p>
            <div className="mt-1 space-y-1">
              <Link
                prefetch={false}
                href="/organization"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/organization")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Building2 className="h-4 w-4" />
                Organization
              </Link>
              <Link
                prefetch={false}
                href="/settings/runtime"
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                  pathname.startsWith("/settings")
                    ? "bg-blue-100 text-blue-800 font-medium"
                    : "hover:bg-slate-100",
                )}
              >
                <Settings className="h-4 w-4" />
                Runtime Settings
              </Link>
              {isAdmin ? (
                <Link
                  prefetch={false}
                  href="/gateways"
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-slate-700 transition",
                    pathname.startsWith("/gateways")
                      ? "bg-blue-100 text-blue-800 font-medium"
                      : "hover:bg-slate-100",
                  )}
                >
                  <Network className="h-4 w-4" />
                  Gateways
                </Link>
              ) : null}
                          </div>
          </div>
        </nav>
      </div>
      <div className="border-t border-slate-200 p-4">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              systemStatus === "operational" && "bg-emerald-500",
              systemStatus === "degraded" && "bg-rose-500",
              systemStatus === "unknown" && "bg-slate-300",
            )}
          />
          {statusLabel}
        </div>
      </div>
    </aside>
  );
}
