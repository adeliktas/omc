"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import {
  Pause,
  Play,
  RefreshCw,
  Radio,
  X,
  Download,
  Clock,
  WifiOff,
  CheckSquare,
  Square,
} from "lucide-react";

import { SignedIn, SignedOut } from "@/auth/clerk";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { getApiBaseUrl } from "@/lib/api-base";
import { SignedOutPanel } from "@/components/auth/SignedOutPanel";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { cn } from "@/lib/utils";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { ProviderStatsBar } from "@/components/ProviderStatsBar";
import { HarnessSummary } from "@/components/HarnessSummary";
import { summarizeHarnesses, type HarnessSummary as HarnessSummaryItem } from "@/lib/harness-summary";

// ─── types ────────────────────────────────────────────────────────────────────

type Flow = {
  id: string;
  ts: string;
  host: string;
  path: string;
  method: string;
  model: string;
  status_code: number;
  latency_ms: number | null;
  agent_name: string | null;
  agent_id: string | null;
  board_id: string | null;
  messages_count: number;
  has_system: boolean;
  has_tools: boolean;
  tools_count: number;
  stream: boolean;
  temperature: number | null;
  max_tokens: number | null;
  last_user_message: string;
  finish_reason: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  content_preview: string;
  reasoning_preview: string;
  has_tool_calls: boolean;
  tool_calls_count: number;
  req_body: Record<string, unknown> | null;
  res_body: Record<string, unknown> | null;
};

type NativeAttempt = {
  attempt_id: string; cycle_id: string; task_id: string; board_id: string; task_title: string;
  member_id: string; member_name: string; role_kind: string; stage_key: string;
  provider: string; model: string; status: string; started_at: string | null;
  finished_at: string | null; usage_reported: boolean; total_tokens: number | null;
  tool_counts: Record<string, number> | null;
};

type AgentStat = {
  agent_name: string;
  agent_id: string | null;
  board_id: string | null;
  flow_count: number;
  total_tokens: number;
  error_count: number;
  avg_latency_ms: number | null;
};

type Stats = {
  flow_count: number;
  all_flow_count?: number;
  total_tokens: number;
  avg_latency_ms: number | null;
  p50_latency_ms: number | null;
  p99_latency_ms?: number | null;
  errors: number;
  running: boolean;
  last_ts: string | null;
  agents: AgentStat[];
};

type TrafficHealth = {
  enabled: boolean;
  flow_count: number;
  latest_ts: string | null;
  stale_seconds: number | null;
  // T111: 24h error breakdown from the provider-failure gate service.
  errors?: {
    window_seconds: number;
    total: number;
    errors: number;
    error_pct: number;
    by_status: Record<string, number>;
    top_errors: { message: string; count: number }[];
    providers: Record<string, { total: number; errors: number; error_pct: number }>;
  };
};

// T111: status-code chip colors — 4xx amber, 5xx red, 2xx green.
function statusChipColor(code: string): string {
  const n = parseInt(code, 10);
  if (n >= 500) return "bg-rose-100 text-rose-700 border-rose-200";
  if (n >= 400) return "bg-amber-100 text-amber-700 border-amber-200";
  return "bg-emerald-100 text-emerald-700 border-emerald-200";
}

// ─── agent colour palette ─────────────────────────────────────────────────────

const AGENT_COLOURS = [
  "bg-blue-100 text-blue-800",
  "bg-violet-100 text-violet-800",
  "bg-emerald-100 text-emerald-800",
  "bg-orange-100 text-orange-800",
  "bg-rose-100 text-rose-800",
  "bg-cyan-100 text-cyan-800",
  "bg-amber-100 text-amber-800",
  "bg-pink-100 text-pink-800",
  "bg-teal-100 text-teal-800",
  "bg-indigo-100 text-indigo-800",
];

function useAgentColours(agents: string[]) {
  return useMemo(() => {
    const map = new Map<string, string>();
    agents.forEach((a, i) => map.set(a, AGENT_COLOURS[i % AGENT_COLOURS.length]));
    return map;
  }, [agents]);
}

// ─── utils ────────────────────────────────────────────────────────────────────

// T103 v13 (2026-05-19): owner asked for tokens displayed with 2-decimal
// precision (e.g. 2.13k instead of 2,133; 1.05M instead of 1,050,000).
// Under 1000 → integer; 1k-1M → `<n.nn>k`; ≥1M → `<n.nn>M`.
function fmtTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1_000) return n.toLocaleString();
  if (n < 1_000_000) return (n / 1_000).toFixed(2) + "k";
  return (n / 1_000_000).toFixed(2) + "M";
}

function fmtLat(ms: number | null): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function latColor(ms: number | null): string {
  if (ms == null) return "text-slate-400";
  if (ms < 3000) return "text-emerald-600";
  if (ms < 15000) return "text-amber-600";
  return "text-rose-600";
}

function timeStr(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString("de-DE", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch { return ts.slice(11, 19); }
}

function dateStr(ts: string): string {
  try {
    return new Date(ts).toLocaleDateString("de-DE", { month: "2-digit", day: "2-digit" });
  } catch { return ts.slice(0, 10); }
}

function humanSince(ts: string | null): string {
  if (!ts) return "never";
  const diff = Math.max(0, Date.now() - new Date(ts).getTime());
  const mins = Math.round(diff / 60_000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

function fmtDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function prettyJSON(obj: unknown): string {
  if (obj == null) return "null";
  try { return JSON.stringify(obj, null, 2); } catch { return String(obj); }
}

// ─── api ──────────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) h["Authorization"] = `Bearer ${token}`;
  }
  return h;
}

async function apiFetch<T>(path: string): Promise<T> {
  const base = getApiBaseUrl();
  const res = await fetch(`${base}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json() as Promise<T>;
}

// ─── stat card ────────────────────────────────────────────────────────────────

function StatCard({ label, value, color = "text-slate-800" }: {
  label: string; value: string | number; color?: string;
}) {
  return (
    <div className="bg-white rounded-xl px-4 py-3 shadow-sm border border-slate-100 min-w-[90px]">
      <div className={cn("text-xl font-bold leading-none", color)}>{String(value)}</div>
      <div className="text-[10px] uppercase tracking-widest text-slate-400 mt-1">{label}</div>
    </div>
  );
}

// ─── per-agent stats table ────────────────────────────────────────────────────

function AgentStatsTable({ agents, colourMap }: {
  agents: AgentStat[];
  colourMap: Map<string, string>;
}) {
  if (agents.length === 0) return null;
  return (
    <div className="bg-white rounded-xl shadow-sm border border-slate-100 overflow-hidden mx-4 mb-3">
      <div className="px-4 py-2 border-b border-slate-100 text-xs font-semibold text-slate-500 uppercase tracking-wide">
        Per-Agent Breakdown
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-slate-50 text-left">
            <th className="px-3 py-2 text-slate-500 font-semibold">Agent</th>
            <th className="px-3 py-2 text-slate-500 font-semibold">Board</th>
            <th className="px-3 py-2 text-slate-500 font-semibold text-right">Flows</th>
            <th className="px-3 py-2 text-slate-500 font-semibold text-right">Tokens</th>
            <th className="px-3 py-2 text-slate-500 font-semibold text-right">Avg Lat</th>
            <th className="px-3 py-2 text-slate-500 font-semibold text-right">Errors</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.agent_name} className="border-t border-slate-50 hover:bg-slate-50">
              <td className="px-3 py-2">
                <span className={cn("px-1.5 py-0.5 rounded text-[11px] font-medium", colourMap.get(a.agent_name))}>
                  {a.agent_name}
                </span>
              </td>
              <td className="px-3 py-2">
                {a.board_id ? (
                  <Link href={`/boards/${a.board_id}`} className="text-blue-600 hover:underline font-mono text-[10px]">
                    {a.board_id.slice(0, 8)}…
                  </Link>
                ) : <span className="text-slate-300">—</span>}
              </td>
              <td className="px-3 py-2 text-right font-mono">{a.flow_count}</td>
              <td className="px-3 py-2 text-right font-mono text-cyan-700">
                {fmtTokens(a.total_tokens)}
              </td>
              <td className={cn("px-3 py-2 text-right font-mono", latColor(a.avg_latency_ms))}>
                {fmtLat(a.avg_latency_ms)}
              </td>
              <td className={cn("px-3 py-2 text-right font-mono", a.error_count > 0 ? "text-rose-600" : "text-slate-400")}>
                {a.error_count}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── flow modal ───────────────────────────────────────────────────────────────

type ModalTab = "conversation" | "tools" | "reasoning" | "req" | "resp" | "raw";

function FlowModal({ flow, onClose, agentColour }: {
  flow: Flow; onClose: () => void; agentColour: string;
}) {
  const [tab, setTab] = useState<ModalTab>("conversation");
  const reqBody = flow.req_body;
  const resBody = flow.res_body;
  const messages = (reqBody?.messages as Array<{ role: string; content: unknown; tool_calls?: unknown[]; tool_call_id?: string }>) ?? [];
  const choices = (resBody?.choices as Array<Record<string, unknown>>) ?? [];
  const responseMsg = (choices[0]?.message ?? {}) as Record<string, unknown>;
  const responseContent = (responseMsg?.content as string) ?? "";
  const reasoning = (responseMsg?.reasoning ?? responseMsg?.reasoning_content ?? "") as string;
  const toolCalls = (responseMsg?.tool_calls as Array<Record<string, unknown>>) ?? [];

  function msgBubble(role: string, body: ReactNode) {
    // T69 (2026-05-15): dark-mode-aware bubble classes. Light bg variants
    // alone produced white-on-bright-pastel unreadability in dark theme.
    // Each role keeps its accent colour; bg + text + border switch
    // between light/dark variants via Tailwind's `dark:` prefix.
    const cls =
      role === "system"
        ? "bg-amber-50 dark:bg-amber-950/40 border-l-2 border-amber-400 dark:border-amber-500 text-slate-800 dark:text-amber-100"
        : role === "user"
          ? "bg-blue-50 dark:bg-blue-950/40 border-l-2 border-blue-400 dark:border-blue-500 text-slate-800 dark:text-blue-100"
          : role === "assistant"
            ? "bg-emerald-50 dark:bg-emerald-950/40 border-l-2 border-emerald-400 dark:border-emerald-500 text-slate-800 dark:text-emerald-100"
            : role === "request"
              ? "bg-sky-50 dark:bg-sky-950/40 border-l-2 border-sky-400 dark:border-sky-500 text-slate-800 dark:text-sky-100"
              : role === "response"
                ? "bg-teal-50 dark:bg-teal-950/40 border-l-2 border-teal-400 dark:border-teal-500 text-slate-800 dark:text-teal-100"
                : role === "raw flow"
                  ? "bg-slate-100 dark:bg-slate-900/60 border-l-2 border-slate-400 dark:border-slate-500 text-slate-800 dark:text-slate-100"
                  : "bg-purple-50 dark:bg-purple-950/40 border-l-2 border-purple-400 dark:border-purple-500 text-slate-800 dark:text-purple-100";
    return (
      <div className={cn("rounded-lg px-3 py-2.5 mb-2 text-sm", cls)}>
        <div className="text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-300 mb-1">{role}</div>
        {body}
      </div>
    );
  }

  function renderContent(content: unknown): ReactNode {
    if (typeof content === "string")
      return <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed">{content}</pre>;
    if (Array.isArray(content))
      return (
        <>
          {content.map((item, i) => {
            const it = item as Record<string, unknown>;
            if (it.type === "text")
              return <pre key={i} className="whitespace-pre-wrap break-words text-xs">{String(it.text ?? "")}</pre>;
            if (it.type === "image_url")
              return <em key={i}>[image]</em>;
            return <pre key={i} className="text-xs">{prettyJSON(item)}</pre>;
          })}
        </>
      );
    return null;
  }

  const tabs: { id: ModalTab; label: string; show: boolean }[] = [
    { id: "conversation", label: "Conversation", show: true },
    { id: "tools", label: `Tools (${toolCalls.length})`, show: toolCalls.length > 0 },
    { id: "reasoning", label: "Reasoning", show: reasoning.length > 0 },
    { id: "req", label: "Request JSON", show: true },
    { id: "resp", label: "Response JSON", show: true },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-[90vw] min-w-[600px] max-w-4xl min-h-[560px] max-h-[90vh] flex flex-col"
           onClick={(e) => e.stopPropagation()}>
        {/* header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-slate-100 dark:border-slate-700">
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-semibold text-slate-800 dark:text-slate-100 text-sm">
                {timeStr(flow.ts)} · {flow.host}
              </span>
              {flow.agent_name && (
                <span className={cn("text-xs px-2 py-0.5 rounded font-medium", agentColour)}>
                  {flow.agent_name}
                </span>
              )}
              {flow.board_id && (
                <Link href={`/boards/${flow.board_id}`}
                      className="text-xs text-blue-600 hover:underline"
                      onClick={onClose}>
                  board ↗
                </Link>
              )}
              <span className="text-xs text-slate-500 font-mono">{flow.model}</span>
            </div>
            <div className="text-xs text-slate-400 mt-0.5 flex flex-wrap gap-x-3">
              {flow.latency_ms != null && <span>{fmtLat(flow.latency_ms)} latency</span>}
              {flow.total_tokens != null && (
                <span>{fmtTokens(flow.total_tokens)} tokens ({fmtTokens(flow.prompt_tokens)}↑ {fmtTokens(flow.completion_tokens)}↓)</span>
              )}
              {flow.finish_reason && <span>finish: {flow.finish_reason}</span>}
              {flow.temperature != null && <span>temp: {flow.temperature}</span>}
              {flow.messages_count > 0 && <span>{flow.messages_count} msgs</span>}
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 p-1 shrink-0">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* tabs — shadcn/ui Tabs component */}
        <Tabs value={tab} onValueChange={(v) => setTab(v as ModalTab)} className="flex-1 flex flex-col min-h-0">
          <TabsList className="px-4 py-0 border-b border-slate-100 rounded-none bg-transparent h-10 min-h-[40px] gap-1 flex-wrap justify-start overflow-x-auto">
            <TabsTrigger value="conversation" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Conversation</TabsTrigger>
            {toolCalls.length > 0 && (
              <TabsTrigger value="tools" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Tools ({toolCalls.length})</TabsTrigger>
            )}
            {reasoning.length > 0 && (
              <TabsTrigger value="reasoning" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Reasoning</TabsTrigger>
            )}
            <TabsTrigger value="req" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Request JSON</TabsTrigger>
            <TabsTrigger value="resp" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Response JSON</TabsTrigger>
            <TabsTrigger value="raw" className="text-xs h-8 data-[state=active]:bg-blue-600 data-[state=active]:text-white rounded-t-lg">Raw Data</TabsTrigger>
          </TabsList>

          {/* body — T69: stable height across tabs so flipping to an
              empty Response JSON doesn't shrink the modal. min-h matches
              the typical conversation height. */}
          <div className="flex-1 overflow-auto p-4 min-h-[420px]">
          <TabsContent value="conversation" className="mt-0 min-h-[100px]">
              {messages.map((m, i) => {
                const body = renderContent(m.content);
                return (
                  <div key={i}>
                    {m.tool_call_id
                      ? msgBubble(`tool result · ${m.tool_call_id}`, body)
                      : msgBubble(m.role, body)}
                  </div>
                );
              })}
              {reasoning && (
                <div className="bg-slate-50 dark:bg-slate-900/60 border border-dashed border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2.5 mb-2">
                  <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400 dark:text-slate-300 mb-1">
                    🧠 Reasoning ({reasoning.length.toLocaleString()} chars)
                  </div>
                  <pre className="text-xs text-slate-600 dark:text-slate-200 whitespace-pre-wrap max-h-64 overflow-auto">{reasoning}</pre>
                </div>
              )}
              {toolCalls.length > 0 && toolCalls.map((tc, i) => {
                const fn = (tc.function ?? {}) as Record<string, unknown>;
                let args = "";
                try { args = JSON.stringify(JSON.parse(String(fn.arguments ?? "{}")), null, 2); }
                catch { args = String(fn.arguments ?? ""); }
                return (
                  <div key={i}>
                    {msgBubble(`tool call · ${String(tc.id ?? "")}`,
                      <div>
                        <strong className="text-xs">{String(fn.name ?? "")}</strong>
                        <pre className="text-xs mt-1 bg-white rounded p-2 border border-slate-100">{args}</pre>
                      </div>
                    )}
                  </div>
                );
              })}
              {responseContent && msgBubble("assistant",
                <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed">{responseContent}</pre>
              )}
              {messages.length === 0 && !responseContent && (
                <p className="text-sm text-slate-400 text-center py-6">No conversation data</p>
              )}
          </TabsContent>

          <TabsContent value="tools" className="mt-0">
            <div>
              {toolCalls.map((tc, i) => {
                const fn = (tc.function ?? {}) as Record<string, unknown>;
                let args = "";
                try { args = JSON.stringify(JSON.parse(String(fn.arguments ?? "{}")), null, 2); }
                catch { args = String(fn.arguments ?? ""); }
                return (
                  <div key={i} className="bg-purple-50 dark:bg-purple-950/40 border border-purple-200 dark:border-purple-700 rounded-lg p-3 mb-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="font-semibold text-purple-800 dark:text-purple-200 text-sm">{String(fn.name ?? "(unnamed)")}</span>
                      <code className="text-xs text-slate-400 dark:text-slate-500">{String(tc.id ?? "")}</code>
                    </div>
                    <pre className="text-xs bg-white rounded p-2 border border-purple-100 overflow-auto max-h-48">{args}</pre>
                  </div>
                );
              })}
              {Array.isArray(reqBody?.tools) && (
                <>
                  <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mt-4 mb-2">
                    Available tools ({(reqBody.tools as Array<Record<string, unknown>>).length})
                  </h3>
                  {(reqBody.tools as Array<Record<string, unknown>>).map((t, i) => {
                    const fn = (t.function ?? t) as Record<string, unknown>;
                    return (
                      <div key={i} className="bg-slate-50 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 rounded-lg p-3 mb-2">
                        <div className="font-semibold text-sm text-slate-700 dark:text-slate-200">{String(fn.name ?? "")}</div>
                        {fn.description != null && <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{String(fn.description)}</p>}
                        <pre className="text-xs mt-2 overflow-auto max-h-32 text-slate-800 dark:text-slate-200">{prettyJSON(fn.parameters ?? {})}</pre>
                      </div>
                    );
                  })}
                </>
              )}
            </div>
          </TabsContent>

          <TabsContent value="reasoning" className="mt-0">
            <pre className="text-xs text-slate-600 whitespace-pre-wrap leading-relaxed">
              {reasoning || "No reasoning data"}
            </pre>
          </TabsContent>

          <TabsContent value="req" className="mt-0">
            {msgBubble(
              "request",
              <pre className="text-xs whitespace-pre-wrap break-words leading-relaxed font-mono max-h-[60vh] overflow-auto">
                {reqBody && Object.keys(reqBody).length > 0
                  ? prettyJSON(reqBody)
                  : <span className="italic text-slate-400 dark:text-slate-500">No request body captured.</span>}
              </pre>,
            )}
          </TabsContent>

          <TabsContent value="resp" className="mt-0">
            {msgBubble(
              "response",
              <pre className="text-xs whitespace-pre-wrap break-words leading-relaxed font-mono max-h-[60vh] overflow-auto">
                {resBody && Object.keys(resBody).length > 0
                  ? prettyJSON(resBody)
                  : <span className="italic text-slate-400 dark:text-slate-500">No response body captured (streaming response or upstream error).</span>}
              </pre>,
            )}
          </TabsContent>

          <TabsContent value="raw" className="mt-0">
            {msgBubble(
              "raw flow",
              <pre className="text-xs whitespace-pre-wrap break-words leading-relaxed font-mono max-h-[60vh] overflow-auto">{prettyJSON(flow)}</pre>,
            )}
          </TabsContent>
          </div>
        </Tabs>
      </div>
    </div>
  );
}

// ─── per-model stats table ────────────────────────────────────────────────────

function ModelStatsTable({ rows }: {
  rows: Array<{
    model: string;
    provider: string;
    calls: number;
    tokens: number;
    errors: number;
    avg_latency_ms: number | null;
    error_rate_pct: number;
    tokens_per_sec: number | null;
  }>;
}) {
  return (
    <div className="surface-card rounded-xl overflow-hidden mx-4 mb-3">
      <div className="px-4 py-2 border-b border-slate-100 text-xs font-semibold text-muted uppercase tracking-wide">
        Per-Model Breakdown (filtered)
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="surface-muted text-left">
            <th className="px-3 py-2 text-muted font-semibold">Model</th>
            <th className="px-3 py-2 text-muted font-semibold">Provider</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Calls</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Tokens</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Tokens/sec</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Avg Lat</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Errors</th>
            <th className="px-3 py-2 text-muted font-semibold text-right">Err %</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model} className="border-t border-slate-50">
              <td className="px-3 py-2 font-mono text-strong">{modelShort(r.model)}</td>
              <td className="px-3 py-2">
                <span className={cn("px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase",
                  providerChipClass(r.provider))}>
                  {r.provider}
                </span>
              </td>
              <td className="px-3 py-2 text-right font-mono">{r.calls}</td>
              <td className="px-3 py-2 text-right font-mono text-cyan-700">
                {fmtTokens(r.tokens)}
              </td>
              <td className="px-3 py-2 text-right font-mono">
                {r.tokens_per_sec != null ? r.tokens_per_sec : "—"}
              </td>
              <td className={cn("px-3 py-2 text-right font-mono", latColor(r.avg_latency_ms))}>
                {fmtLat(r.avg_latency_ms)}
              </td>
              <td className={cn("px-3 py-2 text-right font-mono", r.errors > 0 ? "text-rose-600" : "text-slate-400")}>
                {r.errors}
              </td>
              <td className={cn("px-3 py-2 text-right font-mono",
                r.error_rate_pct > 5 ? "text-rose-600" : r.error_rate_pct > 0 ? "text-amber-600" : "text-slate-400")}>
                {r.error_rate_pct}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── flow row ─────────────────────────────────────────────────────────────────

function FlowRow({ flow, index, onClick, agentColour, checked, onToggle }: {
  flow: Flow; index: number; onClick: () => void; agentColour: string;
  checked: boolean; onToggle: (e: React.MouseEvent, index: number) => void;
}) {
  return (
    <tr className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer" onClick={onClick}>
      <td className="px-2 py-2" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          onClick={(e) => onToggle(e, index)}
          className="text-slate-400 hover:text-blue-600 transition-colors"
        >
          {checked
            ? <CheckSquare className="h-4 w-4 text-blue-600" />
            : <Square className="h-4 w-4" />}
        </button>
      </td>
      <td className="px-3 py-2 font-mono text-xs text-slate-500 whitespace-nowrap">
        {timeStr(flow.ts)}<br />
        <span className="text-[10px] text-slate-400">{dateStr(flow.ts)}</span>
      </td>
      <td className="px-3 py-2">
        {flow.agent_name ? (
          <span className={cn("text-[11px] px-1.5 py-0.5 rounded font-medium whitespace-nowrap", agentColour)}>
            {flow.agent_name}
          </span>
        ) : (
          <span className="text-[11px] text-slate-300">—</span>
        )}
      </td>
      <td className="px-3 py-2 whitespace-nowrap">
        {(() => {
          const p = modelProvider(flow.model, flow.host);
          return p !== "—" ? (
            <span className={cn(
              "text-[10px] px-1.5 py-0.5 rounded font-semibold uppercase",
              providerChipClass(p),
            )}>
              {p}
            </span>
          ) : (
            <span className="text-[10px] text-slate-300">—</span>
          );
        })()}
      </td>
      <td className="px-3 py-2 text-xs text-slate-500 font-mono truncate max-w-[110px]" title={flow.model}>
        {modelShort(flow.model)}
      </td>
      <td className="px-3 py-2 max-w-[280px]">
        <div className="text-[10px] font-bold uppercase tracking-widest text-blue-400 mb-0.5">User</div>
        <div
          className="text-xs text-blue-700 line-clamp-3 whitespace-pre-wrap break-words"
          title={flow.last_user_message}
        >
          {flow.last_user_message || <span className="text-slate-300">—</span>}
        </div>
      </td>
      <td className="px-3 py-2 max-w-[280px]">
        <div className="text-[10px] font-bold uppercase tracking-widest text-emerald-500 mb-0.5">AI</div>
        {(() => {
          // T69: reasoning models (Qwen3.6-thinking, deepseek-reasoner …)
          // emit the full output into `message.reasoning` and leave
          // `message.content` empty. Reply column reads `content_preview`
          // only → always rendered as `—`. Fall back to the captured
          // reasoning preview so the table actually shows what the model
          // produced. Visually distinguish reasoning replies with a slight
          // dim + italics so the difference is obvious.
          // T103 v13 (2026-05-19): owner-readable preview — wrap to 3
          // lines, break long words, keep newlines. Full body still in
          // expand panel.
          const reply = flow.content_preview || flow.reasoning_preview;
          if (!reply) return <span className="text-slate-300">—</span>;
          const isReasoning = !flow.content_preview && !!flow.reasoning_preview;
          return (
            <div
              className={cn(
                "text-xs font-medium line-clamp-3 whitespace-pre-wrap break-words",
                isReasoning
                  ? "italic text-teal-700 dark:text-teal-300"
                  : "text-emerald-700 dark:text-emerald-300",
              )}
              title={reply}
            >
              {reply}
            </div>
          );
        })()}
      </td>
      <td className="px-3 py-2 text-xs whitespace-nowrap">
        <span className={cn("font-semibold tabular-nums", latColor(flow.latency_ms))}>
          {fmtLat(flow.latency_ms)}
        </span>
      </td>
      <td className="px-3 py-2 text-xs text-slate-500 whitespace-nowrap font-mono">
        {flow.total_tokens != null ? (
          <div>
            <span className="text-cyan-700">{fmtTokens(flow.total_tokens)}</span>
            <div className="h-1 w-14 bg-slate-200 rounded mt-0.5">
              <div className="h-full bg-slate-400 rounded"
                style={{ width: `${Math.round(((flow.prompt_tokens ?? 0) / (flow.total_tokens || 1)) * 100)}%` }} />
            </div>
          </div>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="flex gap-1 flex-wrap">
          {flow.stream && <span className="text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700 font-semibold">SSE</span>}
          {(flow.has_tool_calls || flow.has_tools) && <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-100 text-purple-700 font-semibold">Tools{flow.tools_count > 0 ? ` ${flow.tools_count}` : ""}</span>}
          {flow.reasoning_preview && <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-100 text-teal-700 font-semibold">Think</span>}
          {flow.status_code >= 400 && <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 font-semibold">{flow.status_code}</span>}
        </div>
      </td>
    </tr>
  );
}

// ─── main component ───────────────────────────────────────────────────────────

const TIME_PRESETS = [
  { label: "15m", minutes: 15 },
  { label: "1h", minutes: 60 },
  { label: "6h", minutes: 360 },
  { label: "24h", minutes: 1440 },
  { label: "7d", minutes: 10080 },
  { label: "All", minutes: 0 },
];

type TypeFilter = "" | "tool" | "think" | "error" | "sse";

type SortMode = "newest" | "oldest" | "latency_desc" | "tokens_desc";

function modelShort(m: string): string {
  if (!m) return "—";
  return m.split("/").pop() ?? m;
}

// T78 (2026-05-16): used to be a vllm/ollama whitelist that collapsed
// every other provider to "—", which made the traffic-tab Provider filter
// dropdown show only those two. Now we trust the model-string prefix
// (every captured flow uses `<provider>/<model>` per proxy_vllm /
// proxy_generic / provider_benchmark) and fall back to host-sniffing
// only for legacy or unknown rows. Returned token is rendered in the UI
// as a chip + drives the filter dropdown.
function modelProvider(m: string, host?: string): string {
  if (m && m.includes("/")) {
    const p = m.split("/", 1)[0];
    if (p) return p;
  }
  if (host) {
    const h = host.toLowerCase();
    if (h.includes("ollama")) return "ollama";
    if (h.includes("openrouter")) return "openrouter";
    if (h.includes("groq")) return "groq";
    if (h.includes("z.ai")) return "zai";
    if (h.includes("dashscope")) return "dashscope";
    if (h.includes("kilo")) return "kilocode";
    if (h.includes("opencode")) return "opencode-zen";
    if (h.includes("googleapis") || h.includes("generativelanguage")) return "gemini";
    if (h.includes("cloudflare")) return "cloudflare";
    if (h.includes("deepseek")) return "deepseek";
    if (h.includes("anthropic")) return "anthropic";
  }
  return "—";
}

// T78: stable colour per provider name. Anything outside the curated
// palette gets a hash-derived slate variant so the chip is at least
// visually distinguishable.
function providerChipClass(p: string): string {
  const palette: Record<string, string> = {
    vllm: "bg-emerald-100 text-emerald-700",
    ollama: "bg-violet-100 text-violet-700",
    openrouter: "bg-sky-100 text-sky-700",
    groq: "bg-orange-100 text-orange-700",
    gemini: "bg-blue-100 text-blue-700",
    cloudflare: "bg-amber-100 text-amber-700",
    "opencode-zen": "bg-teal-100 text-teal-700",
    zai: "bg-fuchsia-100 text-fuchsia-700",
    dashscope: "bg-rose-100 text-rose-700",
    kilocode: "bg-indigo-100 text-indigo-700",
    deepseek: "bg-cyan-100 text-cyan-700",
    LMStudio: "bg-lime-100 text-lime-700",
    benchmark: "bg-slate-200 text-slate-700",
  };
  return palette[p] ?? "bg-slate-100 text-slate-600";
}

function TrafficContent() {
  const [flows, setFlows] = useState<Flow[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<TrafficHealth | null>(null);
  const [harnesses, setHarnesses] = useState<HarnessSummaryItem[]>([]);
  const [harnessError, setHarnessError] = useState<string | null>(null);
  const [nativeAttempts, setNativeAttempts] = useState<NativeAttempt[]>([]);
  const [nativeError, setNativeError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("");
  const [timeMins, setTimeMins] = useState(1440);
  const [customVal, setCustomVal] = useState("24");
  const [customUnit, setCustomUnit] = useState(60);
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [agentFilter, setAgentFilter] = useState("");
  const [modelFilter, setModelFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [sortMode, setSortMode] = useState<SortMode>("newest");
  const [showAgentStats, setShowAgentStats] = useState(false);
  const [showModelStats, setShowModelStats] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const lastClickIdxRef = useRef<number | null>(null);
  const lastTsRef = useRef<string | null>(null);
  const timeMinsRef = useRef(timeMins);
  useEffect(() => { timeMinsRef.current = timeMins; }, [timeMins]);

  const allAgentNames = useMemo(
    () => [...new Set(flows.map((f) => f.agent_name).filter((n): n is string => Boolean(n)))].sort(),
    [flows],
  );
  const allModels = useMemo(
    () => [...new Set(flows.map((f) => f.model).filter(Boolean))].sort(),
    [flows],
  );
  const allProviders = useMemo(
    () => [...new Set(flows.map((f) => modelProvider(f.model, f.host)).filter((p) => p !== "—"))].sort(),
    [flows],
  );
  const colourMap = useAgentColours(allAgentNames);

  // Flows are events, not a collector heartbeat. No recent OmniRoute request
  // means the proxy is idle; direct Claude/Pi harness activity can still be live.
  const trafficStatus = useMemo(() => {
    const latestHarnessMs = Math.max(
      0,
      ...harnesses.map((harness) => harness.lastAt ? new Date(harness.lastAt).getTime() : 0),
    );
    const harnessLive = latestHarnessMs > Date.now() - 30 * 60_000;
    if (health) {
      if (health.enabled && health.flow_count === 0) {
        return { label: harnessLive ? "harness live · proxy idle" : "proxy idle · no flows", variant: harnessLive ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500" };
      }
      if (health.enabled && health.stale_seconds != null && health.stale_seconds >= 300) {
        return {
          label: harnessLive ? "harness live · proxy idle" : `proxy idle · last flow ${humanSince(health.latest_ts)}`,
          variant: harnessLive ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500",
        };
      }
      if (health.enabled) return { label: "proxy live", variant: "bg-emerald-50 text-emerald-700" };
      return { label: "collector disabled", variant: "bg-slate-100 text-slate-500" };
    }
    if (stats?.running) return { label: "proxy live", variant: "bg-emerald-50 text-emerald-700" };
    return { label: harnessLive ? "harness live · proxy idle" : "waiting", variant: harnessLive ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500" };
  }, [health, stats, harnesses]);

  const sinceParam = useCallback(() => {
    const mins = timeMinsRef.current;
    return mins === 0 ? undefined : new Date(Date.now() - mins * 60_000).toISOString();
  }, []);

  const refresh = useCallback(async (incremental = false) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (incremental && lastTsRef.current) {
        params.set("after", lastTsRef.current);
      } else {
        const s = sinceParam();
        if (s) params.set("since", s);
      }
      const qs = params.toString();
      const url = `/api/v1/traffic/flows${qs ? `?${qs}` : ""}`;
      const statsQs = sinceParam() ? `?since=${encodeURIComponent(sinceParam()!)}` : "";

      const [newFlows, st, h, ranking, attempts] = await Promise.all([
        apiFetch<Flow[]>(url),
        apiFetch<Stats>(`/api/v1/traffic/stats${statsQs}`).catch(() => null),
        apiFetch<TrafficHealth>(`/api/v1/traffic/health`).catch(() => null),
        apiFetch<unknown>("/api/v1/ranking")
          .then((value) => { setHarnessError(null); return value; })
          .catch((caught) => { setHarnessError(caught instanceof Error ? caught.message : "Request failed"); return null; }),
        apiFetch<NativeAttempt[]>(`/api/v1/traffic/native-attempts${statsQs}`)
          .then((value) => { setNativeError(null); return value; })
          .catch((caught) => { setNativeError(caught instanceof Error ? caught.message : "Request failed"); return []; }),
      ]);
      setNativeAttempts(attempts);

      if (ranking) setHarnesses(summarizeHarnesses(ranking));

      if (incremental && lastTsRef.current) {
        if (newFlows.length > 0) {
          setFlows((prev) => {
            const merged = [...newFlows, ...prev];
            const s = sinceParam();
            if (!s) return merged;
            const cutoff = new Date(s).getTime();
            return merged.filter((f) => new Date(f.ts).getTime() >= cutoff);
          });
          lastTsRef.current = newFlows[0].ts;
        }
      } else {
        setFlows(newFlows);
        lastTsRef.current = newFlows[0]?.ts ?? null;
      }
      setStats(st);
      setHealth(h);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [sinceParam]);

  useEffect(() => {
    lastTsRef.current = null;
    void refresh(false);
  }, [refresh, timeMins]);

  useEffect(() => {
    if (paused) return;
    const id = setInterval(() => void refresh(true), 60_000);  // 60s (was 5s)
    return () => clearInterval(id);
  }, [paused, refresh]);

  const filtered = useMemo(() => {
    const cutoff = timeMins === 0 ? null : new Date(Date.now() - timeMins * 60_000);
    const q = search.toLowerCase();
    const list = flows.filter((f) => {
      if (cutoff && new Date(f.ts) < cutoff) return false;
      if (agentFilter && f.agent_name !== agentFilter) return false;
      if (modelFilter && f.model !== modelFilter) return false;
      if (providerFilter && modelProvider(f.model, f.host) !== providerFilter) return false;
      if (typeFilter === "tool" && !f.has_tool_calls && !f.has_tools) return false;
      if (typeFilter === "think" && !f.reasoning_preview) return false;
      if (typeFilter === "error" && f.status_code < 400) return false;
      if (typeFilter === "sse" && !f.stream) return false;
      if (q && ![f.last_user_message, f.content_preview, f.model, f.agent_name ?? ""].join(" ").toLowerCase().includes(q))
        return false;
      return true;
    });
    const sorted = [...list];
    if (sortMode === "newest") {
      sorted.sort((a, b) => +new Date(b.ts) - +new Date(a.ts));
    } else if (sortMode === "oldest") {
      sorted.sort((a, b) => +new Date(a.ts) - +new Date(b.ts));
    } else if (sortMode === "latency_desc") {
      sorted.sort((a, b) => (b.latency_ms ?? 0) - (a.latency_ms ?? 0));
    } else if (sortMode === "tokens_desc") {
      sorted.sort((a, b) => (b.total_tokens ?? 0) - (a.total_tokens ?? 0));
    }
    return sorted;
  }, [flows, timeMins, search, typeFilter, agentFilter, modelFilter, providerFilter, sortMode]);

  // Per-model aggregate stats (computed from currently visible filtered set)
  const modelStats = useMemo(() => {
    const acc = new Map<string, { calls: number; tokens: number; errors: number; latSum: number; latN: number }>();
    for (const f of filtered) {
      const m = f.model || "?";
      const ent = acc.get(m) ?? { calls: 0, tokens: 0, errors: 0, latSum: 0, latN: 0 };
      ent.calls += 1;
      ent.tokens += f.total_tokens ?? 0;
      if (f.status_code >= 400) ent.errors += 1;
      if (f.latency_ms != null) { ent.latSum += f.latency_ms; ent.latN += 1; }
      acc.set(m, ent);
    }
    return [...acc.entries()]
      .map(([model, v]) => ({
        model,
        provider: modelProvider(model),
        calls: v.calls,
        tokens: v.tokens,
        errors: v.errors,
        avg_latency_ms: v.latN > 0 ? Math.round(v.latSum / v.latN) : null,
        error_rate_pct: v.calls > 0 ? Math.round((100 * v.errors) / v.calls * 10) / 10 : 0,
        tokens_per_sec: v.latN > 0 && v.tokens > 0 ? Math.round((v.tokens * 1000) / v.latSum) : null,
      }))
      .sort((a, b) => b.calls - a.calls);
  }, [filtered]);

  function exportFlows() {
    const data = selectedIds.size > 0
      ? filtered.filter((f) => selectedIds.has(`${f.ts}-${f.id}`))
      : filtered;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `llm-traffic-${new Date().toISOString().slice(0,19)}.json`;
    a.click(); URL.revokeObjectURL(url);
  }

  const agentColour = (f: Flow) => colourMap.get(f.agent_name ?? "") ?? "bg-slate-100 text-slate-600";

  // Fetch full flow detail (with req_body/res_body) from the detail endpoint
  const openFlowDetail = useCallback(async (flow: Flow) => {
    setSelectedFlow(flow); // show modal immediately with preview data
    try {
      const full = await apiFetch<Flow>(`/api/v1/traffic/flows/${flow.id}`);
      setSelectedFlow(full ?? flow);
    } catch {
      // Modal already rendered with list data — leave as-is
    }
  }, []);

  return (
    <div className="flex flex-col h-full bg-app">
      {selectedFlow && (
        <FlowModal flow={selectedFlow} onClose={() => setSelectedFlow(null)}
          agentColour={agentColour(selectedFlow)} />
      )}

      {/* toolbar */}
      <div className="flex items-center gap-2 px-4 py-3 surface-panel border-b flex-wrap">
        <Radio className="h-4 w-4 text-muted shrink-0" />
        <span className="text-sm font-semibold text-strong">LLM API Proxy Traffic</span>
        {loading && <RefreshCw className="h-3.5 w-3.5 text-muted animate-spin" />}
        <span className={cn("text-xs px-2 py-0.5 rounded-full font-medium", trafficStatus.variant)}>
          {trafficStatus.label}
        </span>
        <div className="flex-1" />
        <ProviderStatsBar />
        <Button variant="ghost" size="sm" onClick={exportFlows} className="h-7 px-2" title={selectedIds.size > 0 ? `Export ${selectedIds.size} selected` : "Export all"}>
          <Download className="h-3.5 w-3.5" />
          {selectedIds.size > 0 && (
            <span className="ml-1 text-[10px] font-semibold text-blue-600">{selectedIds.size}</span>
          )}
        </Button>
        {selectedIds.size > 0 && (
          <Button variant="ghost" size="sm" onClick={() => { setSelectedIds(new Set()); lastClickIdxRef.current = null; }} className="h-7 px-2 text-[10px] text-slate-500">
            Deselect
          </Button>
        )}
        <Button variant="ghost" size="sm" onClick={() => void refresh()} className="h-7 px-2">
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setPaused((v) => !v)} className="h-7 px-2">
          {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          <span className="ml-1 text-xs">{paused ? "Resume" : "Pause"}</span>
        </Button>
      </div>

      <div className="border-b px-4 py-3">
        <p className="mb-2 text-xs text-muted">
          Proxy flows below cover OmniRoute/API endpoints. Coding harness subscriptions call Claude and Pi providers directly, so their live availability and measured usage are shown separately.
        </p>
        <HarnessSummary
          harnesses={harnesses}
          loading={loading && harnesses.length === 0}
          error={harnessError}
        />
        <h2 className="mt-3 text-sm font-semibold">Native task attempts</h2>
        <p className="text-xs text-muted">Last 40 updated attempts in this time window. Queued is not executing. Observed invocation totals and completed built-in tools; not HTTP flows, billed cost, or model diversity.</p>
        {nativeError ? <p role="alert" className="text-xs text-rose-700">{nativeError}</p> : (
          <div className="max-h-52 overflow-auto">
            <table className="w-full text-xs">
              <thead><tr><th scope="col">Task / attempt</th><th scope="col">Worker / provider</th><th scope="col">Status</th><th scope="col">Observed tokens</th><th scope="col">Completed tools</th></tr></thead>
              <tbody>{nativeAttempts.map((attempt) => (
                <tr key={attempt.attempt_id} className="border-t">
                  <td className="py-1"><Link className="text-blue-700 underline" href={`/boards/${attempt.board_id}?taskId=${attempt.task_id}`}>{attempt.task_title}</Link><div title={`Cycle ${attempt.cycle_id}; attempt ${attempt.attempt_id}`}>{attempt.stage_key} · {attempt.attempt_id.slice(0, 8)}</div></td>
                  <td>{attempt.member_name} ({attempt.role_kind})<div>{attempt.provider}/{attempt.model}</div></td>
                  <td>{attempt.status}<div>{attempt.started_at ? new Date(attempt.started_at + (attempt.started_at.endsWith("Z") ? "" : "Z")).toLocaleTimeString() : "Not started"}</div></td>
                  <td>{attempt.usage_reported && attempt.total_tokens !== null ? fmtTokens(attempt.total_tokens) : "Not reported"}</td>
                  <td>{attempt.tool_counts ? Object.entries(attempt.tool_counts).map(([name, count]) => `${name} ${count}`).join(", ") : "Not observed"}</td>
                </tr>
              ))}</tbody>
            </table>
            {!nativeAttempts.length && <p className="text-xs text-muted">No native attempts observed in this time window.</p>}
          </div>
        )}
      </div>

      {/* T111: 24h error health strip */}
      {health?.errors && health.errors.total > 0 && (
        <div className="px-4 py-2 border-b surface-panel flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          <span className="font-semibold text-strong">24h</span>
          <span className={cn(
            "px-2 py-0.5 rounded-full font-semibold",
            health.errors.error_pct > 20 ? "bg-rose-100 text-rose-700" :
            health.errors.error_pct > 5 ? "bg-amber-100 text-amber-700" :
            "bg-emerald-100 text-emerald-700"
          )}>
            {100 - health.errors.error_pct}% OK · {health.errors.error_pct}% err ({health.errors.errors}/{health.errors.total})
          </span>
          {Object.entries(health.errors.by_status)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 5)
            .map(([code, count]) => (
              <span key={code} className={cn("px-1.5 py-0.5 rounded border font-mono", statusChipColor(code))}>
                {code}×{count}
              </span>
            ))}
          {(() => {
            const worst = Object.entries(health.errors.providers || {})
              .filter(([, s]) => s.error_pct > 0)
              .sort((a, b) => b[1].error_pct - a[1].error_pct)[0];
            if (!worst) return null;
            return (
              <span className={cn(
                "px-2 py-0.5 rounded-full",
                worst[1].error_pct > 50 ? "bg-rose-100 text-rose-700" : "bg-amber-100 text-amber-700"
              )}>
                worst: {worst[0]} {worst[1].error_pct}% ({worst[1].errors}/{worst[1].total})
              </span>
            );
          })()}
          {health.errors.top_errors[0] && (
            <span
              className="text-muted truncate max-w-md"
              title={health.errors.top_errors.map((t) => `${t.count}× ${t.message}`).join("\n")}
            >
              top: {health.errors.top_errors[0].message}
            </span>
          )}
        </div>
      )}

      {(error || health?.enabled === false) && (
        <div className="px-4 py-2 text-xs text-rose-600 bg-rose-50 border-b border-rose-100">
          {error === "503" ? "mitmproxy offline — run llm-traffic" : error ? `Error: ${error}` : null}
          {!health?.enabled && !error && (
            <span className="flex items-center gap-1 mt-1">
              <WifiOff className="h-3 w-3" /> Traffic collection not active. No flows have been recorded.
            </span>
          )}
          {health?.enabled && health.stale_seconds != null && health.stale_seconds > 300 && (
            <span className="flex items-center gap-1 mt-1">
              <Clock className="h-3 w-3" /> Last flow: {humanSince(health.latest_ts)} ({fmtDuration(health.stale_seconds)} ago)
            </span>
          )}
        </div>
      )}

      {/* stats bar */}
      <div className="flex gap-3 px-4 py-3 flex-wrap items-center">
        <StatCard label="Flows" value={(stats?.flow_count ?? 0).toLocaleString()} color="text-blue-700" />
        {stats?.all_flow_count != null && stats.all_flow_count !== stats.flow_count && (
          <span className="text-xs text-slate-400 self-center">of {stats.all_flow_count.toLocaleString()} total</span>
        )}
        <StatCard label="Avg Lat" value={fmtLat(stats?.avg_latency_ms ?? null)} color={latColor(stats?.avg_latency_ms ?? null)} />
        <StatCard label="P50 Lat" value={fmtLat(stats?.p50_latency_ms ?? null)} color={latColor(stats?.p50_latency_ms ?? null)} />
        {stats?.p99_latency_ms != null && (
          <StatCard label="P99 Lat" value={fmtLat(stats.p99_latency_ms)} color={latColor(stats.p99_latency_ms)} />
        )}
        <StatCard label="Tokens" value={fmtTokens(stats?.total_tokens ?? 0)} color="text-cyan-700" />
        <StatCard label="Errors" value={stats?.errors ?? 0} color={(stats?.errors ?? 0) > 0 ? "text-rose-600" : "text-slate-400"} />
        {health?.latest_ts && (
          <span className="text-xs text-slate-400 flex items-center gap-1 self-center">
            <Clock className="h-3 w-3" /> last ts: {humanSince(health.latest_ts)}
          </span>
        )}
        {allAgentNames.length > 0 && (
          <button onClick={() => setShowAgentStats((v) => !v)}
            className="text-xs text-blue-600 hover:underline ml-1">
            {showAgentStats ? "Hide agents" : "Per-agent ▾"}
          </button>
        )}
        {allModels.length > 0 && (
          <button onClick={() => setShowModelStats((v) => !v)}
            className="text-xs text-blue-600 hover:underline ml-1">
            {showModelStats ? "Hide models" : "Per-model ▾"}
          </button>
        )}
      </div>

      {showAgentStats && stats?.agents && stats.agents.length > 0 && (
        <AgentStatsTable agents={stats.agents} colourMap={colourMap} />
      )}

      {showModelStats && modelStats.length > 0 && (
        <ModelStatsTable rows={modelStats} />
      )}

      {/* filters */}
      <div className="flex gap-2 px-4 pb-3 flex-wrap items-center">
        <input type="text" placeholder="Search prompts, replies, agents…"
          value={search} onChange={(e) => setSearch(e.target.value)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-3 py-1.5 w-52 focus:outline-none focus:ring-2 focus:ring-blue-200" />
        <select value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-2 py-1.5 focus:outline-none">
          <option value="">All agents</option>
          {allAgentNames.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <select value={modelFilter} onChange={(e) => setModelFilter(e.target.value)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-2 py-1.5 focus:outline-none"
          title={modelFilter || "Filter by model"}>
          <option value="">All models</option>
          {allModels.map((m) => <option key={m} value={m}>{modelShort(m)}</option>)}
        </select>
        <select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-2 py-1.5 focus:outline-none">
          <option value="">All providers</option>
          {allProviders.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value as TypeFilter)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-2 py-1.5 focus:outline-none">
          <option value="">All types</option>
          <option value="tool">Has tool calls</option>
          <option value="think">Has reasoning</option>
          <option value="error">Errors only</option>
          <option value="sse">Streaming (SSE)</option>
        </select>
        <select value={sortMode} onChange={(e) => setSortMode(e.target.value as SortMode)}
          className="text-xs border border-slate-200 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 rounded-lg px-2 py-1.5 focus:outline-none"
          title="Sort">
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="latency_desc">Slowest first</option>
          <option value="tokens_desc">Most tokens first</option>
        </select>
        <div className="flex gap-1 items-center flex-wrap">
          <span className="text-xs text-slate-400">Range:</span>
          {TIME_PRESETS.map(({ label, minutes }) => (
            <button key={label} onClick={() => setTimeMins(minutes)}
              className={cn("text-xs px-2 py-1 rounded border transition",
                timeMins === minutes ? "bg-blue-600 text-white border-blue-600" : "bg-white text-slate-600 border-slate-200 hover:border-blue-300")}>
              {label}
            </button>
          ))}
          <input type="number" value={customVal} onChange={(e) => setCustomVal(e.target.value)}
            className="text-xs border border-slate-200 rounded px-2 py-1 w-14" min={1} />
          <select value={customUnit} onChange={(e) => setCustomUnit(Number(e.target.value))}
            className="text-xs border border-slate-200 rounded px-2 py-1">
            <option value={1}>min</option>
            <option value={60}>hrs</option>
            <option value={1440}>days</option>
          </select>
          <button onClick={() => setTimeMins((parseFloat(customVal) || 24) * customUnit)}
            className="text-xs px-2 py-1 rounded bg-blue-600 text-white">Go</button>
        </div>
        <span className="text-xs text-slate-400 ml-auto">{filtered.length.toLocaleString()} flows shown</span>
      </div>

      {/* table */}
      <div className="flex-1 overflow-auto mx-4 mb-4 bg-white rounded-xl shadow-sm border border-slate-100">
        {filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-slate-400 text-sm gap-2">
            <Radio className="h-8 w-8 opacity-30" />
            {flows.length === 0
              ? error
                ? <span className="flex items-center gap-2"><WifiOff className="h-4 w-4" /> Failed to load traffic: {error}</span>
                : <span className="flex items-center gap-2"><WifiOff className="h-4 w-4" /> No traffic yet — ensure llm-traffic proxy is running</span>
              : `No flows match current filters (${flows.length} total)`}
            {health?.latest_ts && (
              <span className="text-xs text-slate-500 mt-2">
                Last flow: {humanSince(health.latest_ts)} · Try selecting &quot;7d&quot; or &quot;All&quot;
              </span>
            )}
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="bg-slate-800 text-white text-left sticky top-0 z-10">
                <th className="px-2 py-2 text-xs font-semibold">
                  <button
                    type="button"
                    onClick={() => {
                      if (selectedIds.size === filtered.length) {
                        setSelectedIds(new Set());
                      } else {
                        setSelectedIds(new Set(filtered.map((f) => `${f.ts}-${f.id}`)));
                      }
                    }}
                    className="text-white hover:text-blue-300 transition-colors"
                    title={selectedIds.size === filtered.length ? "Deselect all" : "Select all"}
                  >
                    {selectedIds.size > 0 && selectedIds.size === filtered.length
                      ? <CheckSquare className="h-4 w-4" />
                      : selectedIds.size > 0
                        ? <CheckSquare className="h-4 w-4 opacity-60" />
                        : <Square className="h-4 w-4" />}
                  </button>
                </th>
                <th className="px-3 py-2 text-xs font-semibold whitespace-nowrap">Time</th>
                <th className="px-3 py-2 text-xs font-semibold">Agent</th>
                <th className="px-3 py-2 text-xs font-semibold">Provider</th>
                <th className="px-3 py-2 text-xs font-semibold">Model</th>
                <th className="px-3 py-2 text-xs font-semibold">Prompt</th>
                <th className="px-3 py-2 text-xs font-semibold">Reply</th>
                <th className="px-3 py-2 text-xs font-semibold">Latency</th>
                <th className="px-3 py-2 text-xs font-semibold">Tokens</th>
                <th className="px-3 py-2 text-xs font-semibold">Flags</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((f, i) => {
                const key = `${f.ts}-${f.id}`;
                const handleToggle = (e: React.MouseEvent, idx: number) => {
                  e.stopPropagation();
                  setSelectedIds((prev) => {
                    const next = new Set(prev);
                    if (e.shiftKey && lastClickIdxRef.current !== null) {
                      const start = Math.min(lastClickIdxRef.current, idx);
                      const end = Math.max(lastClickIdxRef.current, idx);
                      const inRange = filtered.slice(start, end + 1).map((ff) => `${ff.ts}-${ff.id}`);
                      // If anchor is checked, add range; otherwise remove range
                      if (prev.has(`${filtered[lastClickIdxRef.current].ts}-${filtered[lastClickIdxRef.current].id}`)) {
                        for (const id of inRange) next.add(id);
                      } else {
                        for (const id of inRange) next.delete(id);
                      }
                    } else {
                      if (next.has(key)) next.delete(key);
                      else next.add(key);
                    }
                    lastClickIdxRef.current = idx;
                    return next;
                  });
                };
                return (
                <FlowRow key={key} flow={f} index={i}
                  onClick={() => openFlowDetail(f)}
                  agentColour={agentColour(f)}
                  checked={selectedIds.has(key)}
                  onToggle={handleToggle} />
              )})}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ─── page ─────────────────────────────────────────────────────────────────────

export default function TrafficPage() {
  return (
    <DashboardShell>
      <DashboardSidebar />
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <SignedIn>
          <TrafficContent />
        </SignedIn>
        <SignedOut>
          <SignedOutPanel message="Sign in to view LLM traffic" forceRedirectUrl="/traffic" />
        </SignedOut>
      </div>
    </DashboardShell>
  );
}
