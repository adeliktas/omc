"use client";

import Link from "next/link";
import { useState } from "react";
import { Bot, CheckCircle2, CircleAlert } from "lucide-react";

import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import type { HarnessEntry, HarnessSummary as HarnessSummaryItem } from "@/lib/harness-summary";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

/**
 * Harness accounts are ranking entries, not OmniRoute endpoints, so they are
 * edited through the ranking entry API rather than provider CRUD. /ranking owns
 * the full editor (level, budget, pricing); this is the subset an operator needs
 * while looking at provider availability.
 */
async function saveEntry(entry: HarnessEntry, patch: Record<string, unknown>): Promise<void> {
  const response = await fetch(`${getApiBaseUrl()}/api/v1/ranking/entry`, {
    method: "PUT",
    headers: authHeaders(),
    body: JSON.stringify({ provider: entry.provider, model: entry.model, ...patch }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

function ModelRow({ entry, onChanged }: { entry: HarnessEntry; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const held = Boolean(entry.unavailable_until) && !entry.eligible;

  const apply = async (patch: Record<string, unknown>) => {
    setBusy(true);
    try {
      await saveEntry(entry, patch);
      setError(null);
      onChanged();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2 border-t border-slate-200 py-1 first:border-t-0">
      <span className="flex-1 truncate font-mono text-[10px] text-slate-600" title={entry.model}>
        {entry.model}
      </span>
      <label className="flex items-center gap-1 text-[10px] text-slate-500" title="Provider priority: lower wins">
        P
        <input
          type="number"
          disabled={busy}
          defaultValue={entry.provider_priority ?? 999}
          onBlur={(event) => {
            const next = Number(event.target.value);
            if (Number.isFinite(next) && next !== entry.provider_priority) void apply({ provider_priority: next });
          }}
          className="w-11 rounded border border-slate-200 px-1 py-0.5 text-[10px] text-slate-700"
        />
      </label>
      {held && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void apply({ unavailable_until: null, availability_reason: null })}
          className="rounded border border-amber-300 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
          title={`Quota hold until ${new Date(entry.unavailable_until as string).toLocaleString()}`}
        >
          Clear hold
        </button>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={() => void apply({ disabled: !entry.disabled })}
        className={cn(
          "rounded border px-1.5 py-0.5 text-[10px] font-medium disabled:opacity-50",
          entry.disabled
            ? "border-slate-300 text-slate-500 hover:bg-slate-100"
            : "border-emerald-300 text-emerald-700 hover:bg-emerald-50",
        )}
      >
        {entry.disabled ? "Disabled" : "Enabled"}
      </button>
      {error && <span className="text-[10px] text-rose-700">{error}</span>}
    </div>
  );
}

export function HarnessSummary({
  harnesses,
  loading = false,
  error = null,
  onChanged,
}: {
  harnesses: HarnessSummaryItem[];
  loading?: boolean;
  error?: string | null;
  onChanged?: () => void;
}) {
  const refresh = onChanged ?? (() => {});
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <Bot className="h-4 w-4 text-violet-600" />
            Coding harness accounts
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            Availability is from the canonical routing inventory. Harnesses execute coding tasks;
            they are not API endpoints. Priority, enablement and quota holds are editable here;
            level, budget and pricing live on the ranking board.
          </p>
        </div>
        <Link
          href="/ranking"
          className="shrink-0 text-xs font-medium text-blue-600 hover:text-blue-700"
        >
          Open ranking →
        </Link>
      </div>

      {loading ? (
        <p className="mt-3 text-xs text-slate-400">Loading harness availability…</p>
      ) : error ? (
        <p className="mt-3 text-xs text-rose-700">Harness availability is unavailable: {error}</p>
      ) : harnesses.length === 0 ? (
        <p className="mt-3 text-xs text-slate-500">No coding harnesses are configured in ranking.</p>
      ) : (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {harnesses.map((harness) => {
            const ready = harness.readyModels > 0;
            return (
              <div key={harness.provider} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs font-semibold text-slate-700">
                    {harness.provider}
                  </span>
                  <div className="flex shrink-0 items-center gap-2">
                    {harness.priority != null && (
                      <span className="rounded bg-slate-200 px-1 text-[10px] text-slate-600">P{harness.priority}</span>
                    )}
                    <span
                      className={cn(
                        "inline-flex items-center gap-1 text-[10px] font-medium",
                        ready ? "text-emerald-700" : "text-amber-700",
                      )}
                    >
                      {ready ? <CheckCircle2 className="h-3 w-3" /> : <CircleAlert className="h-3 w-3" />}
                      {harness.readyModels}/{harness.models.length} ready
                    </span>
                  </div>
                </div>
                <div className="mt-1">
                  {harness.models.map((entry) => (
                    <ModelRow key={entry.model} entry={entry} onChanged={refresh} />
                  ))}
                </div>
                <p className="mt-1 text-[10px] text-slate-500">
                  {harness.calls.toLocaleString()} calls · {harness.totalTokens.toLocaleString()} measured tokens · ${harness.costUsd.toFixed(4)}
                  {harness.lastAt ? ` · last ${new Date(harness.lastAt).toLocaleString()}` : ""}
                </p>
                {!ready && harness.reason ? (
                  <p className="mt-1 line-clamp-2 text-[10px] text-amber-700">
                    {harness.reason}{harness.retryAt ? ` · retry ${new Date(harness.retryAt).toLocaleString()}` : ""}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
