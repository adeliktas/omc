"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, ShieldCheck, Users } from "lucide-react";

import { SignedIn, SignedOut } from "@/auth/clerk";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

/**
 * Role profiles: the templates agents inherit their identity from.
 *
 * Every agent whose role normalizes to the same slug renders the same
 * IDENTITY.md and SOUL.md. That registry lived only as .j2 files on disk, so
 * the thing defining agent behaviour was invisible in the UI and an agent
 * carrying no role at all fell back to the generic profile unnoticed.
 */

interface RoleAgent {
  name: string;
  role: string | null;
  status: string;
  has_override: boolean;
}

interface Role {
  slug: string;
  has_profile: boolean;
  profile_files: string[];
  agents: RoleAgent[];
  agent_count: number;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

export default function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [orphans, setOrphans] = useState<string[]>([]);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [templates, setTemplates] = useState<Record<string, Record<string, string | null>>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/roles`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRoles(data.items ?? []);
      setOrphans(data.agents_without_role ?? []);
      setError(null);
    } catch (caught) {
      setError(String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (slug: string) => {
    const next = !open[slug];
    setOpen((prev) => ({ ...prev, [slug]: next }));
    if (next && !templates[slug]) {
      try {
        const res = await fetch(`${getApiBaseUrl()}/api/v1/roles/${slug}`, {
          headers: authHeaders(),
        });
        if (res.ok) {
          const data = await res.json();
          setTemplates((prev) => ({ ...prev, [slug]: data.templates ?? {} }));
        }
      } catch {
        /* the row still renders its agents without the source */
      }
    }
  };

  return (
    <DashboardShell>
      <SignedIn>
        <main className="flex-1 overflow-y-auto bg-slate-50">
          <div className="border-b border-slate-200 bg-white px-6 py-4">
            <h1 className="flex items-center gap-2 text-lg font-semibold text-slate-800">
              <ShieldCheck className="h-5 w-5 text-violet-600" />
              Roles
            </h1>
            <p className="mt-1 text-xs text-slate-500">
              Profiles are defined per role, not per agent. Every agent with the same role
              renders the same IDENTITY.md and SOUL.md; seniority and model suffixes
              normalize to the same slug.
            </p>
          </div>

          <div className="mx-auto max-w-4xl space-y-3 p-6">
            {orphans.length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
                <p className="flex items-center gap-2 text-xs font-semibold text-amber-800">
                  <AlertTriangle className="h-4 w-4" />
                  {orphans.length} agent{orphans.length === 1 ? "" : "s"} with no role
                </p>
                <p className="mt-1 text-[11px] text-amber-700">
                  These fall back to the generic profile even when a matching role exists:{" "}
                  <span className="font-mono">{orphans.join(", ")}</span>
                </p>
              </div>
            )}

            {loading && <p className="text-xs text-slate-400">Loading roles…</p>}
            {error && <p className="text-xs text-rose-700">Roles unavailable: {error}</p>}

            {roles.map((role) => (
              <section
                key={role.slug}
                className="overflow-hidden rounded-lg border border-slate-200 bg-white"
              >
                <button
                  type="button"
                  onClick={() => void toggle(role.slug)}
                  className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-slate-50"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    {open[role.slug] ? (
                      <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
                    ) : (
                      <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
                    )}
                    <span className="truncate font-mono text-sm font-semibold text-slate-800">
                      {role.slug}
                    </span>
                    {!role.has_profile && (
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
                        no profile on disk
                      </span>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-1 text-[11px] text-slate-500">
                    <Users className="h-3.5 w-3.5" />
                    {role.agent_count}
                  </span>
                </button>

                {open[role.slug] && (
                  <div className="border-t border-slate-100 px-4 py-3">
                    <ul className="mb-3 grid gap-1 sm:grid-cols-2">
                      {role.agents.map((agent) => (
                        <li
                          key={agent.name}
                          className="flex items-center justify-between gap-2 rounded bg-slate-50 px-2 py-1"
                        >
                          <span className="truncate text-xs text-slate-700">{agent.name}</span>
                          <span className="flex shrink-0 items-center gap-1">
                            {agent.has_override && (
                              <span
                                className="rounded bg-violet-100 px-1 text-[9px] text-violet-700"
                                title="This agent overrides the shared profile"
                              >
                                override
                              </span>
                            )}
                            <span
                              className={cn(
                                "text-[10px]",
                                agent.status === "online" ? "text-emerald-600" : "text-slate-400",
                              )}
                            >
                              {agent.status}
                            </span>
                          </span>
                        </li>
                      ))}
                      {role.agents.length === 0 && (
                        <li className="text-[11px] text-slate-400">No agents use this role.</li>
                      )}
                    </ul>

                    {Object.entries(templates[role.slug] ?? {}).map(([name, body]) =>
                      body ? (
                        <details key={name} className="mt-2">
                          <summary className="cursor-pointer text-[11px] font-medium text-slate-600">
                            {name}
                          </summary>
                          <pre className="mt-1 max-h-64 overflow-auto rounded bg-slate-900 p-2 text-[10px] leading-relaxed text-slate-100">
                            {body}
                          </pre>
                        </details>
                      ) : null,
                    )}
                  </div>
                )}
              </section>
            ))}
          </div>
        </main>
      </SignedIn>
      <SignedOut>{null}</SignedOut>
    </DashboardShell>
  );
}
