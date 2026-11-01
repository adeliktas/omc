"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Archive,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
  GitCompare,
  Clock,
  HardDrive,
  CheckCircle2,
  AlertTriangle,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type Snapshot = {
  id: string;
  label: string;
  reason: string;
  started_at: string;
  finished_at: string;
  total_size: number;
  component_count: number;
  scope: "full" | "partial";
};

type Manifest = Snapshot & {
  components: {
    kind: string;
    path: string;
    size: number;
    sha256: string;
  }[];
};

type DiffResult = {
  ok: boolean;
  from?: string;
  to?: string;
  changes?: {
    component: string;
    change: "added" | "removed" | "modified";
    size?: number;
    size_a?: number;
    size_b?: number;
  }[];
  error?: string;
};

const COMPONENT_LABELS: Record<string, string> = {
  postgres: "Postgres DB",
  traffic_sqlite: "Traffic sqlite",
  openclaw_config: "openclaw.json + configs",
  workspaces: "Agent workspaces",
  wiki_vault: "Wiki vault",
  omniroute_volume: "OmniRoute volume",
  compose: "compose.yml + .env",
};

const fmtBytes = (n: number): string => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
};

const fmtTs = (iso: string): string => {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
};

function BackupsContent() {
  const { getToken } = useAuth();
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [components, setComponents] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [manifests, setManifests] = useState<Record<string, Manifest>>({});
  const [diffPair, setDiffPair] = useState<[string | null, string | null]>([null, null]);
  const [diffResult, setDiffResult] = useState<DiffResult | null>(null);

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
      const r = await fetch(`${getApiBaseUrl()}/api/v1/backups`, {
        headers: await authHeader(),
      });
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      setSnapshots(d.snapshots || []);
      setComponents(d.components || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [authHeader]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const create = useCallback(async (label: string) => {
    setCreating(true);
    setError(null);
    try {
      const r = await fetch(`${getApiBaseUrl()}/api/v1/backups`, {
        method: "POST",
        headers: { ...(await authHeader()), "Content-Type": "application/json" },
        body: JSON.stringify({ label, reason: "manual" }),
      });
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }, [authHeader, reload]);

  const restore = useCallback(
    async (snapshotId: string, components?: string[]) => {
      const msg =
        `RESTORE snapshot ${snapshotId}?\n\n` +
        (components
          ? `Components: ${components.join(", ")}\n\n`
          : "ALL components.\n\n") +
        "This will OVERWRITE the live system state with the snapshot. " +
        "Affected services may need restart. Continue?";
      if (!confirm(msg)) return;
      setRestoring(snapshotId);
      setError(null);
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/backups/${encodeURIComponent(snapshotId)}/restore`,
          {
            method: "POST",
            headers: { ...(await authHeader()), "Content-Type": "application/json" },
            body: JSON.stringify({ components, confirm: true }),
          },
        );
        if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
        const d = await r.json();
        alert(
          `Restore ${d.ok ? "completed" : "PARTIAL"}\n\n` +
            Object.entries(d.results || {})
              .map(([k, v]) => `${k}: ${v ? "OK" : "FAIL"}`)
              .join("\n") +
            "\n\nNote: backend will reload openclaw configs automatically. " +
            "OmniRoute / Postgres restore may require container restart.",
        );
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setRestoring(null);
      }
    },
    [authHeader, reload],
  );

  const remove = useCallback(
    async (snapshotId: string) => {
      if (!confirm(`Delete snapshot ${snapshotId}?\nThis cannot be undone.`)) return;
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/backups/${encodeURIComponent(snapshotId)}`,
          { method: "DELETE", headers: await authHeader() },
        );
        if (!r.ok) throw new Error(String(r.status));
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [authHeader, reload],
  );

  const loadManifest = useCallback(
    async (snapshotId: string) => {
      if (manifests[snapshotId]) return;
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/backups/${encodeURIComponent(snapshotId)}`,
          { headers: await authHeader() },
        );
        if (!r.ok) return;
        const m = (await r.json()) as Manifest;
        setManifests((prev) => ({ ...prev, [snapshotId]: m }));
      } catch {
        /* ignore */
      }
    },
    [authHeader, manifests],
  );

  const runDiff = useCallback(async () => {
    if (!diffPair[0] || !diffPair[1]) return;
    setDiffResult(null);
    try {
      const r = await fetch(
        `${getApiBaseUrl()}/api/v1/backups/diff/${encodeURIComponent(diffPair[0])}/${encodeURIComponent(diffPair[1])}`,
        { headers: await authHeader() },
      );
      if (!r.ok) throw new Error(String(r.status));
      setDiffResult(await r.json());
    } catch (e) {
      setDiffResult({
        ok: false,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }, [diffPair, authHeader]);

  const totals = useMemo(() => {
    const total = snapshots.reduce((s, x) => s + x.total_size, 0);
    return {
      count: snapshots.length,
      total_size: total,
      latest: snapshots[0]?.started_at,
      latestComponentCount: snapshots[0]?.component_count,
    };
  }, [snapshots]);

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Archive className="h-6 w-6 text-slate-600" />
            Backups
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Component snapshots (Postgres, sqlite, openclaw.json, workspaces,
            wiki, OmniRoute, compose). Most snapshots capture only a subset —
            restore only affects the components actually present in the
            chosen snapshot. Stored under <code className="text-xs">SNAPSHOT_ROOT</code>{" "}
            (default <code className="text-xs">/snapshots</code>).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void reload()}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => {
              const label = prompt("Snapshot label (optional)", "") || "";
              void create(label);
            }}
            disabled={creating}
          >
            <Plus className="h-4 w-4 mr-1" />
            {creating ? "Snapshotting…" : "Take Snapshot"}
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <Stat label="Snapshots" value={String(totals.count)} />
        <Stat label="Total size" value={fmtBytes(totals.total_size)} />
        <Stat
          label="Latest"
          value={totals.latest ? new Date(totals.latest).toLocaleString() : "—"}
        />
        <Stat
          label="Latest snapshot components"
          value={
            totals.latestComponentCount != null
              ? `${totals.latestComponentCount} of ${components.length} kinds`
              : "—"
          }
        />
      </div>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      {/* Diff selector */}
      <section className="rounded-lg border border-slate-200 bg-white p-3">
        <h2 className="text-sm font-semibold mb-2 flex items-center gap-2">
          <GitCompare className="h-4 w-4" />
          Compare two snapshots
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={diffPair[0] || ""}
            onChange={(e) => setDiffPair([e.target.value, diffPair[1]])}
            className="px-2 py-1.5 text-xs border border-slate-300 rounded font-mono"
          >
            <option value="">— from —</option>
            {snapshots.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id}
              </option>
            ))}
          </select>
          <span className="text-slate-400">→</span>
          <select
            value={diffPair[1] || ""}
            onChange={(e) => setDiffPair([diffPair[0], e.target.value])}
            className="px-2 py-1.5 text-xs border border-slate-300 rounded font-mono"
          >
            <option value="">— to —</option>
            {snapshots.map((s) => (
              <option key={s.id} value={s.id}>
                {s.id}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="outline"
            onClick={() => void runDiff()}
            disabled={!diffPair[0] || !diffPair[1]}
          >
            Diff
          </Button>
        </div>
        {diffResult && (
          <div className="mt-2 text-xs">
            {diffResult.error ? (
              <div className="text-rose-600">{diffResult.error}</div>
            ) : !diffResult.changes || diffResult.changes.length === 0 ? (
              <div className="text-emerald-600">No component changes.</div>
            ) : (
              <table className="w-full mt-1">
                <thead className="text-[10px] uppercase text-slate-500">
                  <tr>
                    <th className="text-left py-1">Component</th>
                    <th className="text-left py-1">Change</th>
                    <th className="text-right py-1">Size from</th>
                    <th className="text-right py-1">Size to</th>
                  </tr>
                </thead>
                <tbody>
                  {diffResult.changes.map((c, i) => (
                    <tr key={i} className="border-t border-slate-100">
                      <td className="py-1 font-mono">{c.component}</td>
                      <td
                        className={`py-1 font-semibold ${
                          c.change === "added"
                            ? "text-emerald-600"
                            : c.change === "removed"
                              ? "text-rose-600"
                              : "text-amber-600"
                        }`}
                      >
                        {c.change}
                      </td>
                      <td className="py-1 text-right font-mono text-slate-600">
                        {c.size_a != null
                          ? fmtBytes(c.size_a)
                          : c.change === "added"
                            ? "—"
                            : c.size != null
                              ? fmtBytes(c.size)
                              : "—"}
                      </td>
                      <td className="py-1 text-right font-mono text-slate-600">
                        {c.size_b != null
                          ? fmtBytes(c.size_b)
                          : c.change === "removed"
                            ? "—"
                            : c.size != null
                              ? fmtBytes(c.size)
                              : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      {/* Snapshot list */}
      <section className="rounded-lg border border-slate-200 bg-white overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-3 py-2 text-left">When</th>
              <th className="px-3 py-2 text-left">Label / Reason</th>
              <th className="px-3 py-2 text-left">Scope</th>
              <th className="px-3 py-2 text-right">Size</th>
              <th className="px-3 py-2 text-right">Components</th>
              <th className="px-3 py-2 text-right"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {snapshots.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-slate-500 text-xs">
                  No snapshots yet. Click <strong>Take Snapshot</strong> to create one.
                </td>
              </tr>
            ) : (
              snapshots.map((s) => (
                <SnapshotRow
                  key={s.id}
                  snap={s}
                  expanded={expandedId === s.id}
                  manifest={manifests[s.id]}
                  restoring={restoring === s.id}
                  onExpand={() => {
                    const next = expandedId === s.id ? null : s.id;
                    setExpandedId(next);
                    if (next) void loadManifest(s.id);
                  }}
                  onRestore={(components) => void restore(s.id, components)}
                  onDelete={() => void remove(s.id)}
                />
              ))
            )}
          </tbody>
        </table>
      </section>

      <footer className="text-[11px] text-slate-500 px-1">
        <p>
          <Clock className="inline h-3 w-3 mr-1" />
          Snapshots are stored OUTSIDE <code>~/.openclaw</code> so a workspace
          restore doesn&apos;t destroy the backup history. Manifests committed to
          git (snapshots dir) for diff/history view.
        </p>
        <p className="mt-1">
          <AlertTriangle className="inline h-3 w-3 mr-1" />
          Restore is destructive. Owner-confirm required. OmniRoute + Postgres
          restores may need container restart to take effect.
        </p>
      </footer>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p className="text-sm font-bold text-slate-900 font-mono">{value}</p>
    </div>
  );
}

function SnapshotRow({
  snap,
  expanded,
  manifest,
  restoring,
  onExpand,
  onRestore,
  onDelete,
}: {
  snap: Snapshot;
  expanded: boolean;
  manifest?: Manifest;
  restoring: boolean;
  onExpand: () => void;
  onRestore: (components?: string[]) => void;
  onDelete: () => void;
}) {
  return (
    <>
      <tr className="hover:bg-slate-50 cursor-pointer" onClick={onExpand}>
        <td className="px-3 py-2.5">
          <p className="text-xs font-mono">{snap.id}</p>
          <p className="text-[10px] text-slate-500">{fmtTs(snap.started_at)}</p>
        </td>
        <td className="px-3 py-2.5 text-xs">
          <p className="font-medium">{snap.label || "—"}</p>
          <p className="text-[10px] text-slate-500">{snap.reason}</p>
        </td>
        <td className="px-3 py-2.5">
          <span
            className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${
              snap.scope === "full"
                ? "bg-emerald-100 text-emerald-700 border-emerald-200"
                : "bg-amber-100 text-amber-700 border-amber-200"
            }`}
          >
            {snap.scope === "full" ? (
              <CheckCircle2 className="h-3 w-3" />
            ) : (
              <AlertTriangle className="h-3 w-3" />
            )}
            {snap.scope}
          </span>
        </td>
        <td className="px-3 py-2.5 text-right font-mono text-xs">
          <HardDrive className="inline h-3 w-3 mr-1 text-slate-400" />
          {fmtBytes(snap.total_size)}
        </td>
        <td className="px-3 py-2.5 text-right font-mono text-xs text-slate-600">
          {snap.component_count}
        </td>
        <td className="px-3 py-2.5 text-right">
          <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
            <Button
              size="sm"
              variant="outline"
              onClick={() => onRestore()}
              disabled={restoring}
              title="Restore all components from this snapshot"
            >
              <RotateCcw className="h-3.5 w-3.5 mr-1" />
              {restoring ? "Restoring…" : "Restore"}
            </Button>
            <button
              onClick={onDelete}
              disabled={restoring}
              className="p-1 rounded hover:bg-rose-50 text-rose-500"
              title="Delete snapshot"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-slate-50">
          <td colSpan={6} className="px-6 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-2">
              Components ({manifest?.components?.length ?? "…"})
            </p>
            {!manifest ? (
              <p className="text-xs text-slate-500">Loading manifest…</p>
            ) : (
              <div className="space-y-1">
                {manifest.components.map((c) => (
                  <div
                    key={c.kind}
                    className="flex items-center justify-between rounded border border-slate-200 bg-white px-2 py-1.5 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-medium">
                        {COMPONENT_LABELS[c.kind] || c.kind}
                      </span>
                      <span className="font-mono text-[10px] text-slate-400">
                        {c.path}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] text-slate-500">
                        {fmtBytes(c.size)}
                      </span>
                      <span
                        className="font-mono text-[10px] text-slate-400"
                        title={c.sha256}
                      >
                        sha {c.sha256.slice(0, 8)}…
                      </span>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => onRestore([c.kind])}
                        disabled={restoring}
                      >
                        <RotateCcw className="h-3 w-3 mr-1" />
                        Restore just this
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function BackupsPage() {
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
          <BackupsContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
