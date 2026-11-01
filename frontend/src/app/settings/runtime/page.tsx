"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Settings as SettingsIcon, Save, RefreshCw, RotateCcw } from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type SettingEntry = {
  key: string;
  type: "int" | "float" | "str" | "bool";
  default: number | string | boolean;
  section: string;
  description: string;
  value: number | string | boolean | null;
  source: "default" | "env" | "override";
  // "live"    = the consumer re-reads the knob each tick; saving is enough.
  // "restart" = resolved once at backend import; needs a backend restart.
  applies: "live" | "restart";
};

type Sections = Record<string, SettingEntry[]>;

function SettingsContent() {
  const { getToken } = useAuth();
  const [settings, setSettings] = useState<SettingEntry[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
      const r = await fetch(`${base}/api/v1/settings/runtime`, {
        headers: await authHeader(),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      const d = await r.json();
      setSettings(d.settings || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [authHeader]);

  useEffect(() => {
    reload();
  }, [reload]);

  const save = useCallback(
    async (key: string, value: string | null) => {
      setSaving(key);
      try {
        const base = getApiBaseUrl();
        const r = await fetch(`${base}/api/v1/settings/runtime/${encodeURIComponent(key)}`, {
          method: "PATCH",
          headers: { ...(await authHeader()), "Content-Type": "application/json" },
          body: JSON.stringify({
            value: value === null || value === "" ? null : value,
          }),
        });
        if (!r.ok) throw new Error(`${r.status}`);
        await reload();
        setDrafts((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSaving(null);
      }
    },
    [authHeader, reload],
  );

  const sections = useMemo(() => {
    const s: Sections = {};
    for (const e of settings) (s[e.section] ??= []).push(e);
    return s;
  }, [settings]);

  const sectionOrder = ["benchmark", "watchdog", "providers", "llm", "traffic"];
  const orderedSections = sectionOrder
    .filter((k) => sections[k])
    .concat(Object.keys(sections).filter((k) => !sectionOrder.includes(k)));

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <SettingsIcon className="h-6 w-6 text-slate-600" />
            Runtime Settings
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Owner-editable operational knobs. Most apply on the next tick of the
            owning loop; the ones marked <em>needs backend restart</em> are read
            once at startup and take effect when the backend is next redeployed.
            Source: <em>override</em> = sidecar value wins, <em>env</em> = .env
            file, <em>default</em> = registry fallback.
          </p>
        </div>
        <Button onClick={reload} disabled={loading} variant="outline" size="sm">
          <RefreshCw className={`h-4 w-4 mr-2 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </header>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      {orderedSections.map((section) => (
        <section
          key={section}
          className="rounded-lg border border-slate-200 bg-white overflow-hidden"
        >
          <h2 className="px-4 py-2 bg-slate-50 border-b border-slate-200 text-sm font-semibold uppercase tracking-wide text-slate-700">
            {section}
          </h2>
          <div className="divide-y divide-slate-100">
            {sections[section].map((entry) => {
              const draftValue = drafts[entry.key];
              const displayValue =
                draftValue !== undefined
                  ? draftValue
                  : entry.value !== null && entry.value !== undefined
                    ? String(entry.value)
                    : "";
              const dirty = draftValue !== undefined && draftValue !== String(entry.value ?? "");
              return (
                <div
                  key={entry.key}
                  className="px-4 py-3 grid grid-cols-1 md:grid-cols-[1fr_220px_auto] gap-3 items-start"
                >
                  <div>
                    <div className="font-mono text-xs text-slate-900">{entry.key}</div>
                    <div className="text-xs text-slate-600 mt-0.5">{entry.description}</div>
                    {entry.applies === "restart" && (
                      <div className="mt-1 inline-block rounded bg-amber-50 border border-amber-300 px-1.5 py-0.5 text-[10px] text-amber-800">
                        Needs backend restart — saving stores the value, but the
                        backend reads this one once at startup.
                      </div>
                    )}
                    <div className="text-[10px] text-slate-400 mt-0.5">
                      type={entry.type} · default={String(entry.default)} · source=
                      <span
                        className={
                          entry.source === "override"
                            ? "text-blue-600 font-semibold"
                            : entry.source === "env"
                              ? "text-amber-600"
                              : "text-slate-500"
                        }
                      >
                        {entry.source}
                      </span>
                    </div>
                  </div>
                  {entry.type === "bool" ? (
                    // A bool in a number input renders blank and saves garbage —
                    // the four pipeline switches were uneditable until 2026-09-22.
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={displayValue === "true"}
                        onChange={(e) =>
                          setDrafts((prev) => ({
                            ...prev,
                            [entry.key]: e.target.checked ? "true" : "false",
                          }))
                        }
                        className="h-4 w-4"
                      />
                      <span className="font-mono">
                        {displayValue === "true" ? "enabled" : "disabled"}
                      </span>
                    </label>
                  ) : (
                    <input
                      type={entry.type === "str" ? "text" : "number"}
                      step={entry.type === "float" ? "0.1" : "1"}
                      value={displayValue}
                      onChange={(e) =>
                        setDrafts((prev) => ({ ...prev, [entry.key]: e.target.value }))
                      }
                      className={`w-full px-2 py-1 text-sm border rounded font-mono ${
                        dirty ? "border-blue-400 bg-blue-50" : "border-slate-300"
                      }`}
                    />
                  )}
                  <div className="flex gap-1">
                    <Button
                      onClick={() => save(entry.key, displayValue || null)}
                      disabled={!dirty || saving === entry.key}
                      size="sm"
                    >
                      <Save className="h-3.5 w-3.5 mr-1" />
                      {saving === entry.key ? "..." : "Save"}
                    </Button>
                    {entry.source === "override" && (
                      <Button
                        onClick={() => save(entry.key, null)}
                        disabled={saving === entry.key}
                        size="sm"
                        variant="outline"
                        title="Revert to env / default"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

export default function SettingsRuntimePage() {
  return (
    <DashboardShell>
      <SignedOut>
        <div className="p-8 text-center">
          <SignInButton mode="modal">
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        <main className="flex-1 overflow-y-auto bg-slate-50 p-6">
          <SettingsContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
