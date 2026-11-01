"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, SlidersHorizontal } from "lucide-react";

import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

/**
 * Models and team roles in one place (owner 2026-10-26): what each model is set to,
 * what each role is configured with, and what the last 7 days actually ran.
 * Settings save through the same audited endpoints as /ranking and the board roster.
 */

interface Entry {
  provider: string;
  model: string;
  level: number;
  max_complexity: number;
  disabled: boolean;
  eligible: boolean;
  block_reason: string | null;
  cost_class: string;
  execution: "agent_harness" | "model_endpoint";
  thinking_level: string | null;
  draft_only: boolean;
  observed_harness: { skills: string[]; mcp: string[]; at: string } | null;
}

interface Member {
  board_id: string;
  board: string;
  provider: string | null;
  model: string | null;
  thinking_level: string;
  enabled: boolean;
  created_by: string;
}

interface Role {
  kind: string;
  slug: string;
  label: string;
  capability: string;
  effort: Record<string, string>;
  members: Member[];
  attempts: number;
  succeeded: number;
  engines: Record<string, number>;
  last_used: string | null;
  harness: { skills: string[]; mcp: string[] };
}

interface Overview {
  roles: Role[];
  models: Record<string, { roles: Record<string, number>; thinking: Record<string, number> }>;
  levels: string[];
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

const counts = (values: Record<string, number>) =>
  Object.entries(values).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join(" · ");

/** Skills and MCP servers as the runner last reported them; nothing reported means no run since. */
function Loaded({ skills, mcp }: { skills?: string[]; mcp?: string[] }) {
  if (!skills && !mcp) return <span className="text-slate-400">no run reported yet</span>;
  return (
    <>
      <div>skills: {skills?.length ? skills.join(", ") : <span className="text-amber-700">none</span>}</div>
      <div className="text-slate-500">MCP: {mcp?.length ? mcp.join(", ") : "none"}</div>
    </>
  );
}

const select = "rounded border border-slate-300 bg-white px-1.5 py-1 text-xs disabled:opacity-50";

function state(role: Role): [string, string] {
  if (role.attempts > 0) return ["In use", "bg-emerald-100 text-emerald-800"];
  if (role.members.length > 0) return ["Configured, not used in 7 days", "bg-amber-100 text-amber-800"];
  return ["Defined, never configured or used", "bg-slate-100 text-slate-600"];
}

export default function ModelsPage() {
  const { isSignedIn } = useAuth();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ranking, over] = await Promise.all(
        ["/api/v1/ranking", "/api/v1/ranking/overview"].map(async (path) => {
          const response = await fetch(apiUrl(path), { headers: authHeaders() });
          if (!response.ok) throw new Error(`${path}: ${response.status}`);
          return response.json();
        }),
      );
      setEntries((ranking.levels as { entries: Entry[] }[]).flatMap((level) => level.entries));
      setOverview(over);
      setError(null);
    } catch (caught) {
      setError(String(caught));
    }
  }, []);

  useEffect(() => {
    if (!isSignedIn) return;
    const first = setTimeout(load, 0);
    const timer = setInterval(load, 60000);
    return () => { clearTimeout(first); clearInterval(timer); };
  }, [isSignedIn, load]);

  const save = useCallback(async (path: string, body: unknown) => {
    setSaving(true);
    try {
      const response = await fetch(apiUrl(path), { method: "PUT", headers: authHeaders(), body: JSON.stringify(body) });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      await load();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setSaving(false);
    }
  }, [load]);

  const levels = overview?.levels ?? ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
  const saveEntry = (entry: Entry, patch: Record<string, unknown>) =>
    save("/api/v1/ranking/entry", { provider: entry.provider, model: entry.model, ...patch });
  const saveMember = (role: Role, member: Member, patch: { thinking_level?: string; enabled?: boolean; auto?: boolean }) =>
    save(`/api/v1/boards/${member.board_id}/team/members`, {
      role_kind: role.kind,
      specialist_slug: role.kind === "specialist" ? role.slug : null,
      provider: member.provider,
      model: member.model,
      thinking_level: patch.thinking_level ?? member.thinking_level,
      enabled: patch.enabled ?? member.enabled,
      auto_assign: patch.auto ?? member.created_by !== "admin",
    });

  if (!isSignedIn) {
    return (
      <DashboardShell>
        <SignedOut><div className="flex min-h-[60vh] flex-col items-center justify-center gap-4"><SlidersHorizontal className="h-12 w-12" /><SignInButton /></div></SignedOut>
      </DashboardShell>
    );
  }

  return (
    <DashboardShell>
      <SignedIn>
        <DashboardSidebar />
        <main className="space-y-8 p-6">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <SlidersHorizontal className="h-6 w-6 text-slate-600" />
              <h1 className="text-2xl font-bold text-slate-900">Models &amp; Roles</h1>
              <button onClick={load} className="rounded p-1 hover:bg-slate-100" aria-label="refresh"><RefreshCw className="h-4 w-4" /></button>
              {saving && <span className="text-xs text-slate-500">saving…</span>}
            </div>
            <p className="mt-1 max-w-4xl text-sm text-slate-600">
              Thinking level for each attempt: the <b>model</b> setting wins, then an owner pin on the <b>role</b>, then the policy
              by role and task complexity (one level up per earlier failure, at most xhigh). A role&apos;s engine is only its default:
              every attempt picks its engine from the ranking, so &ldquo;last 7 days&rdquo; shows what really ran.
              Ordering and quotas stay on <a className="text-blue-700 hover:underline" href="/ranking">Model Ranking</a>.
            </p>
            {error && <div className="mt-2 rounded bg-red-50 p-2 text-sm text-red-700">{error}</div>}
          </div>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-slate-800">Models</h2>
            <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr><th className="p-2">Model</th><th className="p-2">Enabled</th><th className="p-2">Max task complexity</th><th className="p-2">Thinking</th><th className="p-2">Skills &amp; MCP</th><th className="p-2">Last 7 days</th></tr>
                </thead>
                <tbody>
                  {entries.map((entry) => {
                    const use = overview?.models[`${entry.provider}/${entry.model}`];
                    return (
                      <tr key={`${entry.provider}/${entry.model}`} className="border-t border-slate-100 align-top">
                        <td className="p-2">
                          <div className="font-mono text-slate-900">{entry.model}</div>
                          <div className="text-xs text-slate-500">
                            {entry.provider} · L{entry.level} · {entry.execution === "agent_harness" ? "coding agent" : "endpoint"} · {entry.cost_class}
                            {!entry.eligible && !entry.disabled && entry.block_reason && <span className="text-amber-700"> · {entry.block_reason}</span>}
                          </div>
                        </td>
                        <td className="p-2 text-xs text-slate-600">
                          <input type="checkbox" checked={!entry.disabled} disabled={saving} aria-label={`enable ${entry.model}`}
                            onChange={(event) => saveEntry(entry, { disabled: !event.target.checked })} />
                          {entry.cost_class === "local" && (
                            <label className="mt-1 flex items-center gap-1" title="Unproven engine: used only for local drafts while stronger engines are held, never for normal routing">
                              <input type="checkbox" checked={entry.draft_only} disabled={saving}
                                onChange={(event) => saveEntry(entry, { draft_only: event.target.checked })} />
                              drafts only
                            </label>
                          )}
                        </td>
                        <td className="p-2">
                          <select className={select} value={entry.max_complexity} disabled={saving}
                            onChange={(event) => saveEntry(entry, { max_complexity: Number(event.target.value) })}>
                            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>up to {n}</option>)}
                          </select>
                        </td>
                        <td className="p-2">
                          <select className={cn(select, entry.thinking_level && "border-blue-400 font-medium")} value={entry.thinking_level ?? ""} disabled={saving}
                            onChange={(event) => saveEntry(entry, { thinking_level: event.target.value || null })}>
                            <option value="">policy (role × complexity)</option>
                            {levels.map((level) => <option key={level} value={level}>{level}</option>)}
                          </select>
                        </td>
                        <td className="p-2 text-xs text-slate-600">
                          <Loaded skills={entry.observed_harness?.skills} mcp={entry.observed_harness?.mcp} />
                        </td>
                        <td className="p-2 text-xs text-slate-600">
                          {use ? <><div>{counts(use.roles)}</div><div className="text-slate-500">thinking: {counts(use.thinking)}</div></> : <span className="text-slate-400">not used</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-slate-800">Roles &amp; workers</h2>
            <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr><th className="p-2">Role</th><th className="p-2">State</th><th className="p-2">Thinking policy by complexity</th><th className="p-2">Configured on boards</th><th className="p-2">Skills &amp; MCP</th><th className="p-2">Last 7 days</th></tr>
                </thead>
                <tbody>
                  {overview?.roles.map((role) => {
                    const [label, tone] = state(role);
                    return (
                      <tr key={`${role.kind}/${role.slug}`} className="border-t border-slate-100 align-top">
                        <td className="p-2">
                          <div className="font-medium text-slate-900">{role.label}</div>
                          <div className="text-xs text-slate-500">{role.capability === "read_only" ? "reads only" : "edits code"}</div>
                        </td>
                        <td className="p-2"><span className={cn("rounded-full px-2 py-0.5 text-xs", tone)}>{label}</span></td>
                        <td className="p-2 text-xs text-slate-600">{Object.entries(role.effort).map(([n, level]) => `L${n} ${level}`).join(" · ")}</td>
                        <td className="p-2 text-xs">
                          {role.members.length === 0 && <span className="text-slate-400">none</span>}
                          {role.members.map((member) => (
                            <div key={member.board_id} className="mb-1 flex flex-wrap items-center gap-1.5">
                              <span className="text-slate-700">{member.board}:</span>
                              <span className="font-mono text-slate-500">{member.model ?? "auto"}</span>
                              <select className={select} disabled={saving || !member.provider} aria-label={`${role.label} thinking on ${member.board}`}
                                value={member.created_by === "admin" ? member.thinking_level : ""}
                                onChange={(event) => saveMember(role, member, event.target.value
                                  ? { thinking_level: event.target.value, auto: false } : { auto: true })}>
                                <option value="">policy</option>
                                {levels.map((level) => <option key={level} value={level}>pin {level}</option>)}
                              </select>
                              <label className="flex items-center gap-1 text-slate-600">
                                <input type="checkbox" checked={member.enabled} disabled={saving || !member.provider}
                                  onChange={(event) => saveMember(role, member, { enabled: event.target.checked })} />
                                enabled
                              </label>
                            </div>
                          ))}
                        </td>
                        <td className="p-2 text-xs text-slate-600">
                          {role.harness.skills.length || role.harness.mcp.length
                            ? <Loaded skills={role.harness.skills} mcp={role.harness.mcp} />
                            : <span className="text-slate-400">no run reported yet</span>}
                        </td>
                        <td className="p-2 text-xs text-slate-600">
                          {role.attempts > 0 ? (
                            <>
                              <div>{role.attempts} runs, {Math.round((100 * role.succeeded) / role.attempts)}% ok</div>
                              <div className="text-slate-500">{counts(role.engines)}</div>
                            </>
                          ) : <span className="text-slate-400">no runs</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </main>
      </SignedIn>
      <SignedOut>{null}</SignedOut>
    </DashboardShell>
  );
}
