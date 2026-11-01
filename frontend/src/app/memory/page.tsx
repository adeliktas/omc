"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookMarked,
  RefreshCw,
  Plus,
  ChevronLeft,
  ChevronRight,
  MessageSquare,
  Lightbulb,
  Compass,
  Gavel,
  CheckSquare,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type DayMeta = {
  date: string;
  size_bytes: number;
  mtime: string;
};

type Section = { heading: string; body: string };

type DayPayload = {
  date: string;
  exists: boolean;
  raw: string;
  sections: Section[];
  grouped: Record<string, Section[]>;
  size_bytes?: number;
  mtime?: string;
};

const CANONICAL_SECTIONS = [
  "What we discussed",
  "Key findings",
  "Recommendations given",
  "Decisions",
  "Results",
] as const;

const SECTION_ICONS: Record<string, any> = {
  "What we discussed": MessageSquare,
  "Key findings": Lightbulb,
  "Recommendations given": Compass,
  "Decisions": Gavel,
  "Results": CheckSquare,
};

const SECTION_COLOR: Record<string, string> = {
  "What we discussed": "border-l-blue-400 bg-blue-50/40",
  "Key findings": "border-l-amber-400 bg-amber-50/40",
  "Recommendations given": "border-l-emerald-400 bg-emerald-50/40",
  "Decisions": "border-l-rose-400 bg-rose-50/40",
  "Results": "border-l-violet-400 bg-violet-50/40",
};

const pad = (n: number) => String(n).padStart(2, "0");
const todayStr = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const shiftDate = (s: string, days: number) => {
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() + days);
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
};

function MemoryContent() {
  const { getToken } = useAuth();
  const [days, setDays] = useState<DayMeta[]>([]);
  const [selected, setSelected] = useState<string>(todayStr());
  const [day, setDay] = useState<DayPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAppend, setShowAppend] = useState(false);

  const authHeader = useCallback(async (): Promise<HeadersInit> => {
    if (isLocalAuthMode()) {
      const t = getLocalAuthToken();
      return t ? { Authorization: `Bearer ${t}` } : {};
    }
    const t = await getToken();
    return t ? { Authorization: `Bearer ${t}` } : {};
  }, [getToken]);

  const loadDays = useCallback(async () => {
    try {
      const r = await fetch(`${getApiBaseUrl()}/api/v1/memory/days`, {
        headers: await authHeader(),
      });
      if (!r.ok) throw new Error(String(r.status));
      setDays(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [authHeader]);

  const loadDay = useCallback(
    async (date: string) => {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/memory/day/${encodeURIComponent(date)}`,
          { headers: await authHeader() },
        );
        if (!r.ok) throw new Error(String(r.status));
        setDay(await r.json());
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [authHeader],
  );

  useEffect(() => {
    void loadDays();
  }, [loadDays]);

  useEffect(() => {
    void loadDay(selected);
  }, [selected, loadDay]);

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <BookMarked className="h-6 w-6 text-slate-600" />
            Memory
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Daily journal — structured by What we discussed · Key findings ·
            Recommendations · Decisions · Results. Backs `wiki-vault/journal/`.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void loadDays();
              void loadDay(selected);
            }}
          >
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setShowAppend(true)}>
            <Plus className="h-4 w-4 mr-1" />
            Add Entry
          </Button>
        </div>
      </header>

      <div className="flex items-center gap-2">
        <button
          onClick={() => setSelected((d) => shiftDate(d, -1))}
          className="rounded-lg border border-slate-200 bg-white p-2 hover:bg-slate-50"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <input
          type="date"
          value={selected}
          onChange={(e) => setSelected(e.target.value || todayStr())}
          className="px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
        />
        <button
          onClick={() => setSelected((d) => shiftDate(d, 1))}
          className="rounded-lg border border-slate-200 bg-white p-2 hover:bg-slate-50"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
        <Button variant="outline" size="sm" onClick={() => setSelected(todayStr())}>
          Today
        </Button>
        <p className="text-xs text-slate-500 ml-2">
          {day?.exists
            ? `${day.size_bytes} bytes · updated ${day.mtime ? new Date(day.mtime).toLocaleString() : "—"}`
            : "no journal for this day yet"}
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-4">
        <aside className="space-y-2">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 px-1">
            History ({days.length})
          </p>
          <div className="rounded-lg border border-slate-200 bg-white max-h-[640px] overflow-y-auto">
            {days.length === 0 ? (
              <p className="text-xs text-slate-500 p-3">No journal entries yet.</p>
            ) : (
              days.map((d) => (
                <button
                  key={d.date}
                  onClick={() => setSelected(d.date)}
                  className={`w-full text-left px-3 py-2 text-xs border-b border-slate-100 hover:bg-slate-50 transition ${
                    d.date === selected ? "bg-blue-50 border-l-2 border-l-blue-500" : ""
                  }`}
                >
                  <p className="font-mono font-medium">{d.date}</p>
                  <p className="text-[10px] text-slate-500">{d.size_bytes} bytes</p>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="space-y-3">
          {!day?.exists ? (
            <EmptyDay date={selected} authHeader={authHeader} onCreated={() => void loadDay(selected)} />
          ) : (
            <DayView
              day={day}
              authHeader={authHeader}
              onSummaryWritten={() => void loadDay(day.date)}
            />
          )}
        </section>
      </div>

      {showAppend && (
        <AppendDialog
          date={selected}
          authHeader={authHeader}
          onClose={() => setShowAppend(false)}
          onAppended={() => {
            setShowAppend(false);
            void loadDays();
            void loadDay(selected);
          }}
        />
      )}
    </div>
  );
}

function EmptyDay({
  date,
  authHeader,
  onCreated,
}: {
  date: string;
  authHeader: () => Promise<HeadersInit>;
  onCreated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true);
    try {
      await fetch(`${getApiBaseUrl()}/api/v1/memory/new`, {
        method: "POST",
        headers: { ...(await authHeader()), "Content-Type": "application/json" },
        body: JSON.stringify({ date }),
      });
      onCreated();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-lg border-2 border-dashed border-slate-300 bg-white p-8 text-center">
      <BookMarked className="h-10 w-10 text-slate-300 mx-auto mb-3" />
      <p className="text-sm font-medium text-slate-700 mb-1">
        No journal for {date}
      </p>
      <p className="text-xs text-slate-500 mb-3">
        Start fresh. The day file lives at <code>wiki-vault/journal/{date}.md</code>.
      </p>
      <Button onClick={() => void create()} disabled={busy} size="sm">
        Create empty journal
      </Button>
    </div>
  );
}

function DayView({
  day,
  authHeader,
  onSummaryWritten,
}: {
  day: DayPayload;
  authHeader: () => Promise<HeadersInit>;
  onSummaryWritten: () => void;
}) {
  const grouped = day.grouped || {};
  const hasAny = CANONICAL_SECTIONS.some((s) => (grouped[s] || []).length > 0);
  const [genBusy, setGenBusy] = useState(false);
  const generateSummary = async () => {
    setGenBusy(true);
    try {
      await fetch(`${getApiBaseUrl()}/api/v1/memory/auto-summary/${day.date}`, {
        method: "POST",
        headers: await authHeader(),
      });
      onSummaryWritten();
    } finally {
      setGenBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end gap-2">
        <Button
          onClick={() => void generateSummary()}
          disabled={genBusy}
          size="sm"
          variant="outline"
        >
          {genBusy ? "Generating..." : "Generate / refresh daily summary"}
        </Button>
      </div>
      {CANONICAL_SECTIONS.map((s) => {
        const items = grouped[s] || [];
        const Icon = SECTION_ICONS[s] || MessageSquare;
        return (
          <SectionCard key={s} title={s} icon={Icon} count={items.length}>
            {items.length === 0 ? (
              <p className="text-xs text-slate-400 italic p-2">No entries.</p>
            ) : (
              <div className="space-y-2">
                {items.map((it, i) => (
                  <div
                    key={`${s}-${i}`}
                    className={`pl-3 py-1 border-l-2 ${SECTION_COLOR[s] || "border-l-slate-300"}`}
                  >
                    <p className="text-[11px] font-mono text-slate-500 mb-1">
                      {it.heading}
                    </p>
                    <pre className="whitespace-pre-wrap text-xs text-slate-800 font-sans">
                      {it.body}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </SectionCard>
        );
      })}
      {!hasAny && day.sections.length > 0 && (
        <SectionCard title="Other (legacy / uncategorized)" icon={BookMarked} count={day.sections.length}>
          <div className="space-y-2">
            {day.sections.map((it, i) => (
              <div
                key={`other-${i}`}
                className="pl-3 py-1 border-l-2 border-l-slate-300 bg-slate-50/40"
              >
                <p className="text-[11px] font-mono text-slate-500 mb-1">{it.heading}</p>
                <pre className="whitespace-pre-wrap text-xs text-slate-800 font-sans">
                  {it.body}
                </pre>
              </div>
            ))}
          </div>
        </SectionCard>
      )}
      <details className="rounded-lg border border-slate-200 bg-white">
        <summary className="px-3 py-2 cursor-pointer text-xs font-medium text-slate-600 hover:bg-slate-50">
          View raw markdown
        </summary>
        <pre className="px-3 py-2 text-[11px] font-mono whitespace-pre-wrap text-slate-700 border-t border-slate-100 max-h-[400px] overflow-auto">
          {day.raw}
        </pre>
      </details>
    </div>
  );
}

function SectionCard({
  title,
  icon: Icon,
  count,
  children,
}: {
  title: string;
  icon: any;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white overflow-hidden">
      <header className="px-3 py-2 border-b border-slate-100 flex items-center justify-between bg-slate-50/60">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-slate-600" />
          <h3 className="text-sm font-semibold">{title}</h3>
        </div>
        <span className="text-[10px] font-mono text-slate-500">{count}</span>
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}

function AppendDialog({
  date,
  authHeader,
  onClose,
  onAppended,
}: {
  date: string;
  authHeader: () => Promise<HeadersInit>;
  onClose: () => void;
  onAppended: () => void;
}) {
  const [entries, setEntries] = useState<Record<string, string>>(() =>
    Object.fromEntries(CANONICAL_SECTIONS.map((s) => [s, ""])),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    const nonEmpty = Object.fromEntries(
      Object.entries(entries).filter(([, v]) => v.trim().length > 0),
    );
    if (Object.keys(nonEmpty).length === 0) {
      setErr("fill at least one section");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`${getApiBaseUrl()}/api/v1/memory/append`, {
        method: "POST",
        headers: { ...(await authHeader()), "Content-Type": "application/json" },
        body: JSON.stringify({ date, sections: nonEmpty }),
      });
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      onAppended();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-sm font-semibold">
            Add entry — <span className="font-mono">{date}</span>
          </h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-800 text-lg leading-none">
            ×
          </button>
        </div>
        <div className="p-4 space-y-3">
          {err && (
            <div className="rounded border border-red-300 bg-red-50 text-red-700 p-2 text-xs">
              {err}
            </div>
          )}
          <p className="text-xs text-slate-500">
            Each section becomes its own timestamped H2 block. Leave blank to skip.
          </p>
          {CANONICAL_SECTIONS.map((s) => {
            const Icon = SECTION_ICONS[s];
            return (
              <div key={s}>
                <label className="text-xs font-medium text-slate-700 mb-1 flex items-center gap-1.5">
                  <Icon className="h-3.5 w-3.5 text-slate-500" />
                  {s}
                </label>
                <textarea
                  value={entries[s]}
                  onChange={(e) => setEntries((p) => ({ ...p, [s]: e.target.value }))}
                  rows={3}
                  className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                  placeholder={`Add notes for ${s.toLowerCase()}…`}
                />
              </div>
            );
          })}
        </div>
        <div className="px-4 py-3 border-t border-slate-200 flex justify-end gap-2 bg-white sticky bottom-0">
          <Button variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => void submit()} disabled={busy}>
            {busy ? "Saving…" : "Append entry"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function MemoryPage() {
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
          <MemoryContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
