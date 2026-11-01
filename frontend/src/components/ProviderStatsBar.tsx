"use client";

import { useEffect, useState, useCallback } from "react";
import { Cpu, Cloud, CloudOff, Clock } from "lucide-react";
import { cn } from "@/lib/utils";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

interface ProviderStats {
  ollama: {
    session_used_pct: number;
    weekly_used_pct: number;
    session_reset_in: string;
    weekly_reset_in: string;
    concurrent: number;
    flow_count?: number;
    agents?: string[];
    max_concurrent: number;
    unlimited?: boolean;
    status: string;
    window_seconds?: number;
  };
  vllm: {
    concurrent: number;
    flow_count?: number;
    agents?: string[];
    max_concurrent: number;
    unlimited: boolean;
    status: string;
    window_seconds?: number;
  };
  updated_at: string;
}

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const t = getLocalAuthToken();
    if (t) h["Authorization"] = `Bearer ${t}`;
  }
  return h;
}

function apiUrl(path: string): string {
  return `${window.location.protocol}//${window.location.hostname}:8000${path}`;
}

interface ProviderStatsBarProps {
  /** Board id whose cloud toggle this bar mirrors. Required per STATE.md §3 invariant 3. */
  boardId?: string;
}

export function ProviderStatsBar({ boardId }: ProviderStatsBarProps = {}) {
  const [stats, setStats] = useState<ProviderStats | null>(null);
  const [cloudEnabled, setCloudEnabled] = useState(true);
  const [toggling, setToggling] = useState(false);

  // Resolve board id from props or current /boards/<id> route. No hardcoded UUID.
  const resolvedBoardId = (() => {
    if (boardId) return boardId;
    if (typeof window === "undefined") return null;
    const m = window.location.pathname.match(/\/boards\/([0-9a-f-]{36})/i);
    return m ? m[1] : null;
  })();

  const fetchInitialState = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/v1/traffic/provider-stats"), { headers: authHeaders() });
      if (res.ok) {
        const d = await res.json();
        setStats(d);
      }
    } catch { /* ignore */ }
    if (!resolvedBoardId) return;
    try {
      const res = await fetch(apiUrl(`/api/v1/boards/${resolvedBoardId}`), { headers: authHeaders() });
      if (res.ok) {
        const d = await res.json();
        if (d.cloud_providers_enabled !== undefined) setCloudEnabled(d.cloud_providers_enabled);
      }
    } catch { /* ignore */ }
  }, [resolvedBoardId]);

  useEffect(() => {
    void fetchInitialState();
    const t = setInterval(fetchInitialState, 300_000);
    return () => clearInterval(t);
  }, [fetchInitialState]);

  const toggleCloud = async () => {
    if (!resolvedBoardId) return;
    setToggling(true);
    const next = !cloudEnabled;
    try {
      const boardRes = await fetch(apiUrl(`/api/v1/boards/${resolvedBoardId}`), {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ cloud_providers_enabled: next }),
      });
      if (!boardRes.ok) throw new Error("board update failed");

      const toggleRes = await fetch(apiUrl("/api/v1/traffic/cloud-toggle"), {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ enabled: next }),
      });
      if (!toggleRes.ok) throw new Error("cloud toggle failed");

      setCloudEnabled(next);
      if (next) {
        location.reload();
      }
    } catch { /* ignore */ }
    setToggling(false);
  };

  const o = stats?.ollama;
  const v = stats?.vllm;

  return (
    <div className="flex items-center gap-3 text-xs px-2">
      {/* vLLM */}
      <div
        className="flex items-center gap-1.5"
        title={v?.agents?.length ? `Active agents (last ${(v.window_seconds ?? 900) / 60}m): ${v.agents.join(", ")}` : "No vLLM traffic in window"}
      >
        <Cpu className="h-3 w-3 text-emerald-500" />
        <span className="text-slate-600 font-medium">vLLM</span>
        <span className={cn("text-slate-400", v && v.concurrent > 0 && "text-emerald-600 font-semibold")}>
          {v ? `${v.concurrent}/${v.max_concurrent}` : "\u2014"}
        </span>
        {v?.unlimited && <span className="text-[9px] text-emerald-500 font-bold">\u221E</span>}
        {v && v.flow_count !== undefined && v.flow_count > 0 && (
          <span className="text-[10px] text-slate-400 font-mono">{v.flow_count}f</span>
        )}
      </div>

      {/* Ollama */}
      <div
        className="flex items-center gap-1.5"
        title={o?.agents?.length ? `Active agents (last ${(o.window_seconds ?? 900) / 60}m): ${o.agents.join(", ")}` : "No Ollama traffic in window"}
      >
        <Cloud className={cn("h-3 w-3", o?.status === "online" ? "text-sky-500" : "text-slate-300")} />
        <span className="text-slate-600 font-medium">Ollama</span>
        <span className={cn("text-slate-400", o && o.concurrent > 0 && "text-sky-600 font-semibold")}>
          {o ? `${o.concurrent}/${o.max_concurrent}` : "\u2014"}
        </span>
        {o && o.flow_count !== undefined && o.flow_count > 0 && (
          <span className="text-[10px] text-slate-400 font-mono">{o.flow_count}f</span>
        )}
        {o && o.session_used_pct > 0 && (
          <span className={cn("font-mono font-semibold", o.session_used_pct > 80 ? "text-amber-600" : "text-slate-500")}>
            S:{o.session_used_pct}%
          </span>
        )}
        {o && o.weekly_used_pct > 0 && (
          <span className={cn("font-mono", o.weekly_used_pct > 80 ? "text-amber-500" : "text-slate-400")}>
            W:{o.weekly_used_pct}%
          </span>
        )}
        {o?.session_reset_in && o.session_reset_in !== "unknown" && (
          <span className="text-slate-400 flex items-center gap-0.5">
            <Clock className="h-2.5 w-2.5" />
            {o.session_reset_in}
          </span>
        )}
      </div>

      {/* Cloud toggle \u2014 only when bound to a specific board */}
      {resolvedBoardId && (
        <button
          onClick={toggleCloud}
          disabled={toggling}
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 border text-[10px] font-semibold transition-colors",
            cloudEnabled
              ? "bg-sky-50 border-sky-200 text-sky-700 hover:bg-sky-100"
              : "bg-emerald-50 border-emerald-200 text-emerald-700 hover:bg-emerald-100",
            toggling && "opacity-50"
          )}
          title={cloudEnabled
            ? "Cloud providers enabled \u2014 click to force vLLM only. NOTE: cloud-toggle is GLOBAL scope (2026-05-13) \u2014 affects every agent in openclaw.json, not just this board."
            : "vLLM only mode \u2014 click to re-enable cloud. NOTE: cloud-toggle is GLOBAL scope \u2014 affects every agent in openclaw.json."}
        >
          {cloudEnabled ? <Cloud className="h-3 w-3" /> : <CloudOff className="h-3 w-3" />}
          {cloudEnabled ? "Cloud ON" : "vLLM only"}
        </button>
      )}
    </div>
  );
}
