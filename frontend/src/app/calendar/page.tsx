"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
  Plus,
  RefreshCw,
  Trash2,
  Clock,
  Repeat,
  Pencil,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type Occurrence = {
  task_id: string;
  board_id: string | null;
  board_name: string;
  title: string;
  description: string;
  fire_at: string;
  schedule_kind: "cron" | "interval" | "once";
  schedule_value: string;
  complexity: number | null;
  priority: string | null;
  status: string;
  assignee_name: string | null;
};

type Template = {
  key: string;
  title: string;
  description: string;
  cron: string;
  color: string;
};

type ScheduledTask = {
  id: string;
  board_id: string | null;
  board_name: string;
  title: string;
  scheduled_at: string | null;
  repeat_cron: string | null;
  repeat_interval_minutes: number | null;
  repeat_enabled: boolean;
  status: string;
  complexity: number | null;
};

type Board = { id: string; name: string };

const COLOR_MAP: Record<string, string> = {
  violet: "bg-violet-100 text-violet-800 border-violet-200",
  emerald: "bg-emerald-100 text-emerald-800 border-emerald-200",
  sky: "bg-sky-100 text-sky-800 border-sky-200",
  blue: "bg-blue-100 text-blue-800 border-blue-200",
  amber: "bg-amber-100 text-amber-800 border-amber-200",
  rose: "bg-rose-100 text-rose-800 border-rose-200",
  slate: "bg-slate-100 text-slate-800 border-slate-200",
};

const pad = (n: number) => String(n).padStart(2, "0");
const fmtHM = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const fmtYMD = (d: Date) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function startOfWeek(d: Date): Date {
  const x = new Date(d);
  const dow = x.getDay(); // 0 = Sun
  const back = (dow + 6) % 7; // make Monday the start
  x.setDate(x.getDate() - back);
  x.setHours(0, 0, 0, 0);
  return x;
}

function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

function CalendarContent() {
  const { getToken } = useAuth();
  const [view, setView] = useState<"week" | "day" | "month">("week");
  const [anchor, setAnchor] = useState<Date>(() => {
    const t = new Date();
    t.setHours(0, 0, 0, 0);
    return t;
  });
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [scheduled, setScheduled] = useState<ScheduledTask[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [boards, setBoards] = useState<Board[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  // T107: right-click context menu + prefilled new-event time
  const [ctxMenu, setCtxMenu] = useState<{
    x: number;
    y: number;
    when: Date | null;
  } | null>(null);
  const [prefillStart, setPrefillStart] = useState<Date | null>(null);

  const openContextMenu = useCallback((e: React.MouseEvent, when: Date | null) => {
    e.preventDefault();
    setCtxMenu({ x: e.clientX, y: e.clientY, when });
  }, []);

  const closeContextMenu = useCallback(() => setCtxMenu(null), []);

  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [ctxMenu]);

  const authHeader = useCallback(async (): Promise<HeadersInit> => {
    if (isLocalAuthMode()) {
      const t = getLocalAuthToken();
      return t ? { Authorization: `Bearer ${t}` } : {};
    }
    const t = await getToken();
    return t ? { Authorization: `Bearer ${t}` } : {};
  }, [getToken]);

  const windowRange = useMemo((): { from: Date; to: Date } => {
    if (view === "day") return { from: anchor, to: addDays(anchor, 1) };
    if (view === "week") {
      const wk = startOfWeek(anchor);
      return { from: wk, to: addDays(wk, 7) };
    }
    // month
    const m0 = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const m1 = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1);
    return { from: m0, to: m1 };
  }, [view, anchor]);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const base = getApiBaseUrl();
      const headers = await authHeader();
      const fromIso = windowRange.from.toISOString();
      const toIso = windowRange.to.toISOString();
      const [occRes, schedRes, tplRes, boardsRes] = await Promise.all([
        fetch(
          `${base}/api/v1/calendar/occurrences?from=${encodeURIComponent(fromIso)}&to=${encodeURIComponent(toIso)}`,
          { headers },
        ),
        fetch(`${base}/api/v1/calendar/tasks`, { headers }),
        fetch(`${base}/api/v1/calendar/templates`, { headers }),
        fetch(`${base}/api/v1/boards`, { headers }),
      ]);
      if (!occRes.ok) throw new Error(`occurrences: ${occRes.status}`);
      const occData = await occRes.json();
      setOccurrences(occData.items || []);
      if (schedRes.ok) setScheduled(await schedRes.json());
      if (tplRes.ok) setTemplates(await tplRes.json());
      if (boardsRes.ok) {
        const bd = await boardsRes.json();
        const list = Array.isArray(bd) ? bd : bd.items || [];
        setBoards(list.map((b: any) => ({ id: b.id, name: b.name })));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [authHeader, windowRange]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Group occurrences by YYYY-MM-DD then by hour for grid rendering
  const occByDate = useMemo(() => {
    const m: Record<string, Occurrence[]> = {};
    for (const o of occurrences) {
      const d = new Date(o.fire_at);
      const key = fmtYMD(d);
      (m[key] ??= []).push(o);
    }
    for (const k of Object.keys(m)) {
      m[k].sort((a, b) => a.fire_at.localeCompare(b.fire_at));
    }
    return m;
  }, [occurrences]);

  const hours = useMemo(() => Array.from({ length: 24 }, (_, i) => i), []);

  const colorForOccurrence = (o: Occurrence): string => {
    const t = templates.find((x) =>
      o.title.toLowerCase().includes(x.title.toLowerCase()),
    );
    if (t) return COLOR_MAP[t.color] || COLOR_MAP.slate;
    if (o.schedule_kind === "cron") return COLOR_MAP.violet;
    if (o.schedule_kind === "interval") return COLOR_MAP.sky;
    return COLOR_MAP.slate;
  };

  const navigate = (dir: -1 | 1) => {
    setAnchor((d) => {
      const x = new Date(d);
      if (view === "day") x.setDate(x.getDate() + dir);
      else if (view === "week") x.setDate(x.getDate() + 7 * dir);
      else x.setMonth(x.getMonth() + dir);
      return x;
    });
  };

  const goToday = () => {
    const t = new Date();
    t.setHours(0, 0, 0, 0);
    setAnchor(t);
  };

  const headerLabel = useMemo(() => {
    if (view === "day") {
      return anchor.toLocaleDateString(undefined, {
        weekday: "long",
        year: "numeric",
        month: "long",
        day: "numeric",
      });
    }
    if (view === "week") {
      const wk = startOfWeek(anchor);
      const end = addDays(wk, 6);
      return `${wk.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${end.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
    }
    return anchor.toLocaleDateString(undefined, {
      month: "long",
      year: "numeric",
    });
  }, [view, anchor]);

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <CalendarIcon className="h-6 w-6 text-slate-600" />
            Calendar
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Scheduled tasks across all boards. Cron + interval + one-shot.
            Each cell shows what runs when, like Thunderbird's calendar.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-slate-200 bg-white">
            {(["day", "week", "month"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-3 py-1.5 text-xs font-medium capitalize ${
                  view === v
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-100"
                } ${v === "day" ? "rounded-l-lg" : ""} ${v === "month" ? "rounded-r-lg" : ""}`}
              >
                {v}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={goToday}>
            Today
          </Button>
          <button
            onClick={() => navigate(-1)}
            className="rounded-lg border border-slate-200 bg-white p-2 hover:bg-slate-50"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            onClick={() => navigate(1)}
            className="rounded-lg border border-slate-200 bg-white p-2 hover:bg-slate-50"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
          <Button variant="outline" size="sm" onClick={() => void reload()}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setShowCreate(true)}>
            <Plus className="h-4 w-4 mr-1" />
            New Event
          </Button>
        </div>
      </header>

      <p className="text-sm text-slate-700 font-medium">{headerLabel}</p>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4">
        {view === "month" ? (
          <MonthGrid
            anchor={anchor}
            occByDate={occByDate}
            colorFor={colorForOccurrence}
            onContextMenu={openContextMenu}
          />
        ) : view === "week" ? (
          <WeekGrid
            anchor={anchor}
            occByDate={occByDate}
            hours={hours}
            colorFor={colorForOccurrence}
            onContextMenu={openContextMenu}
          />
        ) : (
          <DayGrid
            anchor={anchor}
            occByDate={occByDate}
            hours={hours}
            colorFor={colorForOccurrence}
            onContextMenu={openContextMenu}
          />
        )}

        <aside className="space-y-4">
          <section className="rounded-lg border border-slate-200 bg-white p-3">
            <h2 className="text-sm font-semibold mb-2 flex items-center gap-2">
              <Repeat className="h-4 w-4" />
              Templates
            </h2>
            <p className="text-xs text-slate-500 mb-2">
              Drop a routine. One click creates a task with the cron set.
            </p>
            <div className="space-y-1">
              {templates.map((t) => (
                <TemplateRow
                  key={t.key}
                  template={t}
                  boards={boards}
                  authHeader={authHeader}
                  onCreated={() => void reload()}
                />
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-slate-200 bg-white p-3">
            <h2 className="text-sm font-semibold mb-2 flex items-center gap-2">
              <Clock className="h-4 w-4" />
              Scheduled tasks ({scheduled.length})
            </h2>
            <div className="space-y-1 max-h-[420px] overflow-y-auto">
              {scheduled.length === 0 ? (
                <p className="text-xs text-slate-500 p-1">
                  No scheduled tasks yet. Use a template or "New Event".
                </p>
              ) : (
                scheduled.map((s) => (
                  <ScheduledTaskRow
                    key={s.id}
                    task={s}
                    authHeader={authHeader}
                    onChanged={() => void reload()}
                  />
                ))
              )}
            </div>
          </section>
        </aside>
      </div>

      {showCreate && (
        <CreateEventDialog
          boards={boards}
          authHeader={authHeader}
          prefillStart={prefillStart}
          onClose={() => {
            setShowCreate(false);
            setPrefillStart(null);
          }}
          onCreated={() => {
            setShowCreate(false);
            setPrefillStart(null);
            void reload();
          }}
        />
      )}
      {ctxMenu && (
        <div
          className="fixed z-50 rounded-md border border-slate-200 bg-white shadow-lg py-1 min-w-[200px]"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="w-full text-left px-3 py-1.5 text-xs hover:bg-slate-100"
            onClick={() => {
              setPrefillStart(ctxMenu.when);
              setShowCreate(true);
              closeContextMenu();
            }}
          >
            New event{ctxMenu.when ? ` at ${ctxMenu.when.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : "..."}
          </button>
          <button
            className="w-full text-left px-3 py-1.5 text-xs hover:bg-slate-100"
            onClick={() => {
              setPrefillStart(ctxMenu.when);
              setShowCreate(true);
              closeContextMenu();
            }}
          >
            New recurring task...
          </button>
          <div className="border-t border-slate-100 my-1" />
          <a
            href="/projects"
            className="block w-full text-left px-3 py-1.5 text-xs hover:bg-slate-100"
          >
            Create project (from intent)...
          </a>
          <div className="border-t border-slate-100 my-1" />
          <button
            className="w-full text-left px-3 py-1.5 text-xs text-slate-500 hover:bg-slate-100"
            onClick={closeContextMenu}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function DayGrid({
  anchor,
  occByDate,
  hours,
  colorFor,
  onContextMenu,
}: {
  anchor: Date;
  occByDate: Record<string, Occurrence[]>;
  hours: number[];
  colorFor: (o: Occurrence) => string;
  onContextMenu: (e: React.MouseEvent, when: Date | null) => void;
}) {
  const key = fmtYMD(anchor);
  const list = occByDate[key] || [];
  const byHour = useMemo(() => {
    const m: Record<number, Occurrence[]> = {};
    for (const o of list) {
      const h = new Date(o.fire_at).getHours();
      (m[h] ??= []).push(o);
    }
    return m;
  }, [list]);

  return (
    <div className="rounded-lg border border-slate-200 bg-white overflow-hidden">
      <div className="grid grid-cols-[60px_1fr] divide-x divide-slate-100 max-h-[680px] overflow-y-auto">
        {hours.map((h) => (
          <Row
            key={h}
            h={h}
            items={byHour[h] || []}
            colorFor={colorFor}
            onContextMenu={(e) => {
              const when = new Date(anchor);
              when.setHours(h, 0, 0, 0);
              onContextMenu(e, when);
            }}
          />
        ))}
      </div>
    </div>
  );
}

function Row({
  h,
  items,
  colorFor,
  onContextMenu,
}: {
  h: number;
  items: Occurrence[];
  colorFor: (o: Occurrence) => string;
  onContextMenu?: (e: React.MouseEvent) => void;
}) {
  return (
    <>
      <div className="px-2 py-2 text-[10px] font-mono text-slate-400 border-b border-slate-100 sticky left-0 bg-white">
        {pad(h)}:00
      </div>
      <div
        className="px-2 py-2 border-b border-slate-100 min-h-[44px] space-y-1 hover:bg-slate-50/40"
        onContextMenu={onContextMenu}
      >
        {items.map((o) => (
          <OccurrenceChip key={`${o.task_id}-${o.fire_at}`} o={o} colorFor={colorFor} />
        ))}
      </div>
    </>
  );
}

function WeekGrid({
  anchor,
  occByDate,
  hours,
  colorFor,
  onContextMenu,
}: {
  anchor: Date;
  occByDate: Record<string, Occurrence[]>;
  hours: number[];
  colorFor: (o: Occurrence) => string;
  onContextMenu: (e: React.MouseEvent, when: Date | null) => void;
}) {
  const days = useMemo(() => {
    const wk = startOfWeek(anchor);
    return Array.from({ length: 7 }, (_, i) => addDays(wk, i));
  }, [anchor]);
  const today = fmtYMD(new Date());

  return (
    <div className="rounded-lg border border-slate-200 bg-white overflow-hidden">
      <div className="grid grid-cols-[60px_repeat(7,1fr)] sticky top-0 bg-slate-50 border-b border-slate-200">
        <div></div>
        {days.map((d) => (
          <div
            key={d.toISOString()}
            className={`px-2 py-2 text-center text-xs ${
              fmtYMD(d) === today ? "bg-blue-50 font-semibold text-blue-900" : "text-slate-700"
            }`}
          >
            <div className="text-[10px] uppercase text-slate-500">
              {d.toLocaleDateString(undefined, { weekday: "short" })}
            </div>
            <div className="font-mono text-sm">{d.getDate()}</div>
          </div>
        ))}
      </div>
      <div className="max-h-[680px] overflow-y-auto">
        <div className="grid grid-cols-[60px_repeat(7,1fr)] divide-x divide-slate-100">
          {hours.map((h) => (
            <>
              <div
                key={`h-${h}`}
                className="px-2 py-1.5 text-[10px] font-mono text-slate-400 border-b border-slate-100 sticky left-0 bg-white"
              >
                {pad(h)}:00
              </div>
              {days.map((d) => {
                const key = fmtYMD(d);
                const items = (occByDate[key] || []).filter(
                  (o) => new Date(o.fire_at).getHours() === h,
                );
                return (
                  <div
                    key={`${d.toISOString()}-${h}`}
                    className="px-1 py-1 border-b border-slate-100 min-h-[36px] space-y-0.5 hover:bg-slate-50/40"
                    onContextMenu={(e) => {
                      const when = new Date(d);
                      when.setHours(h, 0, 0, 0);
                      onContextMenu(e, when);
                    }}
                  >
                    {items.map((o) => (
                      <OccurrenceChip
                        key={`${o.task_id}-${o.fire_at}`}
                        o={o}
                        colorFor={colorFor}
                        compact
                      />
                    ))}
                  </div>
                );
              })}
            </>
          ))}
        </div>
      </div>
    </div>
  );
}

function MonthGrid({
  anchor,
  occByDate,
  colorFor,
  onContextMenu,
}: {
  anchor: Date;
  occByDate: Record<string, Occurrence[]>;
  colorFor: (o: Occurrence) => string;
  onContextMenu: (e: React.MouseEvent, when: Date | null) => void;
}) {
  const cells = useMemo(() => {
    const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const start = startOfWeek(first);
    return Array.from({ length: 42 }, (_, i) => addDays(start, i));
  }, [anchor]);
  const today = fmtYMD(new Date());
  return (
    <div className="rounded-lg border border-slate-200 bg-white overflow-hidden">
      <div className="grid grid-cols-7 bg-slate-50 border-b border-slate-200">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <div
            key={d}
            className="px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500"
          >
            {d}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-7">
        {cells.map((d) => {
          const key = fmtYMD(d);
          const items = occByDate[key] || [];
          const inMonth = d.getMonth() === anchor.getMonth();
          return (
            <div
              key={d.toISOString()}
              className={`border-r border-b border-slate-100 min-h-[92px] p-1.5 hover:bg-slate-50/40 ${
                inMonth ? "bg-white" : "bg-slate-50/50"
              }`}
              onContextMenu={(e) => {
                const when = new Date(d);
                when.setHours(9, 0, 0, 0);
                onContextMenu(e, when);
              }}
            >
              <div
                className={`text-[11px] font-mono mb-1 ${
                  key === today
                    ? "inline-flex items-center justify-center bg-blue-600 text-white rounded-full w-5 h-5"
                    : inMonth
                      ? "text-slate-700"
                      : "text-slate-400"
                }`}
              >
                {d.getDate()}
              </div>
              <div className="space-y-0.5">
                {items.slice(0, 4).map((o) => (
                  <OccurrenceChip
                    key={`${o.task_id}-${o.fire_at}`}
                    o={o}
                    colorFor={colorFor}
                    compact
                  />
                ))}
                {items.length > 4 && (
                  <p className="text-[9px] text-slate-500">+{items.length - 4} more</p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function OccurrenceChip({
  o,
  colorFor,
  compact,
}: {
  o: Occurrence;
  colorFor: (o: Occurrence) => string;
  compact?: boolean;
}) {
  const t = new Date(o.fire_at);
  return (
    <a
      href={`/boards/${o.board_id}?taskId=${o.task_id}`}
      className={`block rounded border px-1.5 py-0.5 text-[10px] truncate ${colorFor(o)} hover:brightness-95`}
      title={`${o.title}\n${o.board_name} · ${o.schedule_kind}=${o.schedule_value}\n${o.description}`}
    >
      <span className="font-mono">{fmtHM(t)}</span>
      {!compact && <span className="ml-1 text-slate-400">·</span>}
      <span className="ml-1 font-medium">{o.title}</span>
    </a>
  );
}

function TemplateRow({
  template,
  boards,
  authHeader,
  onCreated,
}: {
  template: Template;
  boards: Board[];
  authHeader: () => Promise<HeadersInit>;
  onCreated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [boardId, setBoardId] = useState<string>(boards[0]?.id || "");

  useEffect(() => {
    if (!boardId && boards.length > 0) setBoardId(boards[0].id);
  }, [boards, boardId]);

  const drop = async () => {
    if (!boardId) {
      alert("Pick a board first");
      return;
    }
    setBusy(true);
    try {
      const base = getApiBaseUrl();
      const headers = { ...(await authHeader()), "Content-Type": "application/json" };
      const r = await fetch(`${base}/api/v1/boards/${boardId}/tasks`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          title: template.title,
          description: template.description,
          status: "inbox",
          priority: "medium",
          repeat_enabled: true,
          repeat_cron: template.cron,
        }),
      });
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      onCreated();
      setOpen(false);
    } catch (e: any) {
      alert("create failed: " + String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded border border-slate-100 hover:border-slate-300 transition">
      <button
        onClick={() => setOpen((x) => !x)}
        className={`w-full text-left px-2 py-1.5 text-xs flex items-center justify-between`}
      >
        <span className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              COLOR_MAP[template.color]?.split(" ")[0] || "bg-slate-400"
            }`}
          />
          <span className="font-medium">{template.title}</span>
        </span>
        <span className="font-mono text-[10px] text-slate-500">{template.cron}</span>
      </button>
      {open && (
        <div className="border-t border-slate-100 p-2 space-y-1.5">
          <p className="text-[11px] text-slate-600">{template.description}</p>
          <select
            value={boardId}
            onChange={(e) => setBoardId(e.target.value)}
            className="w-full text-xs border border-slate-300 rounded px-1.5 py-1"
          >
            <option value="">Pick board…</option>
            {boards.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            onClick={() => void drop()}
            disabled={busy || !boardId}
            className="w-full"
          >
            {busy ? "Creating…" : "Drop on calendar"}
          </Button>
        </div>
      )}
    </div>
  );
}

function ScheduledTaskRow({
  task,
  authHeader,
  onChanged,
}: {
  task: ScheduledTask;
  authHeader: () => Promise<HeadersInit>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [cronDraft, setCronDraft] = useState(task.repeat_cron || "");
  const [intervalDraft, setIntervalDraft] = useState(task.repeat_interval_minutes || 60);
  const [onceDraft, setOnceDraft] = useState(() => {
    const d = task.scheduled_at ? new Date(task.scheduled_at) : new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });

  const patch = async (body: Record<string, unknown>) => {
    if (!task.board_id) return;
    setBusy(true);
    try {
      const base = getApiBaseUrl();
      const headers = { ...(await authHeader()), "Content-Type": "application/json" };
      const r = await fetch(`${base}/api/v1/boards/${task.board_id}/tasks/${task.id}`, {
        method: "PATCH",
        headers,
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(String(r.status));
      onChanged();
      return true;
    } catch (e: any) {
      alert("update failed: " + String(e?.message || e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = () => patch({ repeat_enabled: !task.repeat_enabled });

  const saveSchedule = async () => {
    const ok = task.repeat_cron
      ? await patch({ repeat_cron: cronDraft })
      : task.repeat_interval_minutes
        ? await patch({ repeat_interval_minutes: intervalDraft })
        : await patch({ scheduled_at: new Date(onceDraft).toISOString() });
    if (ok) setEditing(false);
  };

  const remove = async () => {
    if (!task.board_id) return;
    if (!confirm(`Delete schedule for "${task.title}"? Underlying task will be removed.`)) return;
    setBusy(true);
    try {
      const base = getApiBaseUrl();
      const headers = await authHeader();
      const r = await fetch(`${base}/api/v1/boards/${task.board_id}/tasks/${task.id}`, {
        method: "DELETE",
        headers,
      });
      if (!r.ok) throw new Error(String(r.status));
      onChanged();
    } catch (e: any) {
      alert("delete failed: " + String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const sched =
    task.repeat_cron
      ? `cron: ${task.repeat_cron}`
      : task.repeat_interval_minutes
        ? `every ${task.repeat_interval_minutes}m`
        : task.scheduled_at
          ? `once at ${new Date(task.scheduled_at).toLocaleString()}`
          : "—";

  return (
    <div className="rounded border border-slate-100 px-2 py-1.5 hover:border-slate-300 transition">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium truncate" title={task.title}>
            {task.title}
          </p>
          {editing ? (
            <div className="flex items-center gap-1 mt-0.5">
              {task.repeat_cron ? (
                <input
                  value={cronDraft}
                  onChange={(e) => setCronDraft(e.target.value)}
                  className="text-[10px] font-mono border rounded px-1 py-0.5 w-32"
                />
              ) : task.repeat_interval_minutes ? (
                <>
                  <input
                    type="number"
                    min={1}
                    value={intervalDraft}
                    onChange={(e) => setIntervalDraft(Number(e.target.value))}
                    className="text-[10px] font-mono border rounded px-1 py-0.5 w-16"
                  />
                  <span className="text-[10px] text-slate-400">min</span>
                </>
              ) : (
                <input
                  type="datetime-local"
                  value={onceDraft}
                  onChange={(e) => setOnceDraft(e.target.value)}
                  className="text-[10px] font-mono border rounded px-1 py-0.5"
                />
              )}
              <button
                onClick={() => void saveSchedule()}
                disabled={busy}
                className="text-[9px] px-1 py-0.5 rounded bg-emerald-100 text-emerald-700"
              >
                Save
              </button>
              <button
                onClick={() => setEditing(false)}
                disabled={busy}
                className="text-[9px] px-1 py-0.5 rounded bg-slate-100 text-slate-500"
              >
                Cancel
              </button>
            </div>
          ) : (
            <p
              className="text-[10px] text-slate-500 font-mono truncate cursor-pointer hover:underline"
              title={`${sched} — click to edit`}
              onClick={() => setEditing(true)}
            >
              {sched}
            </p>
          )}
          <p className="text-[10px] text-slate-400 truncate" title={task.board_name}>
            {task.board_name}
          </p>
        </div>
        <div className="flex flex-col gap-0.5">
          <button
            onClick={() => void toggleEnabled()}
            disabled={busy}
            className={`px-1.5 py-0.5 rounded text-[9px] font-mono ${
              task.repeat_enabled
                ? "bg-emerald-100 text-emerald-700"
                : "bg-slate-100 text-slate-500"
            }`}
            title={task.repeat_enabled ? "Click to disable" : "Click to enable"}
          >
            {task.repeat_enabled ? "ON" : "OFF"}
          </button>
          <button
            onClick={() => setEditing((v) => !v)}
            disabled={busy}
            className="p-1 rounded hover:bg-slate-100 text-slate-500"
            title="Edit schedule"
          >
            <Pencil className="h-3 w-3" />
          </button>
          <button
            onClick={() => void remove()}
            disabled={busy}
            className="p-1 rounded hover:bg-rose-50 text-rose-500"
            title="Delete schedule"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>
    </div>
  );
}

function CreateEventDialog({
  boards,
  authHeader,
  onClose,
  onCreated,
  prefillStart,
}: {
  boards: Board[];
  authHeader: () => Promise<HeadersInit>;
  onClose: () => void;
  onCreated: () => void;
  prefillStart?: Date | null;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [boardId, setBoardId] = useState<string>(boards[0]?.id || "");
  const [mode, setMode] = useState<"friendly" | "cron" | "interval" | "once">(
    prefillStart ? "once" : "friendly",
  );
  const [cron, setCron] = useState("0 8 * * *");
  const [interval, setIntervalMin] = useState<number>(60);
  const [onceAt, setOnceAt] = useState<string>(() => {
    const d = prefillStart ? new Date(prefillStart) : new Date();
    d.setSeconds(0, 0);
    if (!prefillStart) {
      d.setMinutes(0);
      d.setHours(d.getHours() + 1);
    }
    // datetime-local needs YYYY-MM-DDTHH:MM in local tz, not ISO
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });
  // T107 Lightning-style friendly recurrence
  const [freq, setFreq] = useState<"daily" | "weekly" | "monthly">("daily");
  const [freqInterval, setFreqInterval] = useState<number>(1);
  const [atHour, setAtHour] = useState<number>(prefillStart ? prefillStart.getHours() : 9);
  const [atMin, setAtMin] = useState<number>(prefillStart ? prefillStart.getMinutes() : 0);
  const [weekDays, setWeekDays] = useState<string[]>(["MON", "TUE", "WED", "THU", "FRI"]);
  const [monthDay, setMonthDay] = useState<number>(prefillStart ? prefillStart.getDate() : 1);
  const [reminderMin, setReminderMin] = useState<number>(0);
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Build a cron expression from the friendly form. Mirrors RFC5545 RRULE
  // intent but emits crontab (which is what the backend already executes).
  const friendlyCron = useMemo(() => {
    const hr = Math.max(0, Math.min(23, atHour));
    const mn = Math.max(0, Math.min(59, atMin));
    if (freq === "daily") {
      // every N days → cron day field "*/N" (only honored on days-of-month;
      // crontab can't do "every 3 days from now" precisely, but close enough
      // for typical N=1,2,3,7).
      const dom = freqInterval > 1 ? `*/${freqInterval}` : "*";
      return `${mn} ${hr} ${dom} * *`;
    }
    if (freq === "weekly") {
      // every week on selected weekdays. Cron doesn't support "every other
      // week" without trickery — owner can fall back to cron mode.
      const dow = weekDays.length ? weekDays.join(",") : "*";
      return `${mn} ${hr} * * ${dow}`;
    }
    // monthly: nth day of every month (every N months not standard in cron)
    const dom = Math.max(1, Math.min(31, monthDay));
    return `${mn} ${hr} ${dom} * *`;
  }, [freq, freqInterval, atHour, atMin, weekDays, monthDay]);

  const cronPresets: { label: string; v: string }[] = [
    { label: "00:00 daily", v: "0 0 * * *" },
    { label: "07:00 daily", v: "0 7 * * *" },
    { label: "08:00 daily", v: "0 8 * * *" },
    { label: "09:00 daily", v: "0 9 * * *" },
    { label: "21:00 daily", v: "0 21 * * *" },
    { label: "Every hour", v: "0 * * * *" },
    { label: "Every 15 min", v: "*/15 * * * *" },
    { label: "Weekdays 09:00", v: "0 9 * * MON-FRI" },
    { label: "Monday 08:00", v: "0 8 * * MON" },
  ];

  const submit = async () => {
    if (!title.trim()) {
      setErr("title required");
      return;
    }
    if (!boardId) {
      setErr("board required");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const base = getApiBaseUrl();
      const headers = { ...(await authHeader()), "Content-Type": "application/json" };
      const payload: any = {
        title,
        description,
        status: "inbox",
        priority: "medium",
        repeat_enabled: enabled && mode !== "once",
      };
      if (mode === "friendly") payload.repeat_cron = friendlyCron;
      else if (mode === "cron") payload.repeat_cron = cron;
      else if (mode === "interval") payload.repeat_interval_minutes = interval;
      else if (mode === "once")
        payload.scheduled_at = new Date(onceAt).toISOString();

      const r = await fetch(`${base}/api/v1/boards/${boardId}/tasks`, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
      onCreated();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-sm font-semibold">New scheduled event</h2>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-800 text-lg leading-none"
          >
            ×
          </button>
        </div>
        <div className="p-4 space-y-3">
          {err && (
            <div className="rounded border border-red-300 bg-red-50 text-red-700 p-2 text-xs">
              {err}
            </div>
          )}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              Title
            </label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
              placeholder="e.g. Trend Radar Daily"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
              rows={3}
              placeholder="What should the agent do?"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              Board
            </label>
            <select
              value={boardId}
              onChange={(e) => setBoardId(e.target.value)}
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
            >
              <option value="">Pick board…</option>
              {boards.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              Schedule type
            </label>
            <div className="flex gap-1 mb-2">
              {(["friendly", "cron", "interval", "once"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`px-3 py-1 text-xs rounded ${
                    mode === m
                      ? "bg-slate-900 text-white"
                      : "border border-slate-200 text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
            {mode === "friendly" && (
              <div className="space-y-2 rounded border border-slate-200 bg-slate-50/40 p-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs">Repeats</span>
                  <select
                    value={freq}
                    onChange={(e) => setFreq(e.target.value as any)}
                    className="px-2 py-1 text-xs border border-slate-300 rounded bg-white"
                  >
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="monthly">Monthly</option>
                  </select>
                  {freq !== "weekly" && (
                    <>
                      <span className="text-xs">every</span>
                      <input
                        type="number"
                        min={1}
                        max={31}
                        value={freqInterval}
                        onChange={(e) => setFreqInterval(Number(e.target.value))}
                        className="w-16 px-2 py-1 text-xs border border-slate-300 rounded font-mono"
                      />
                      <span className="text-xs">
                        {freq === "daily" ? "day(s)" : "month(s) on day"}
                      </span>
                      {freq === "monthly" && (
                        <input
                          type="number"
                          min={1}
                          max={31}
                          value={monthDay}
                          onChange={(e) => setMonthDay(Number(e.target.value))}
                          className="w-16 px-2 py-1 text-xs border border-slate-300 rounded font-mono"
                        />
                      )}
                    </>
                  )}
                </div>
                {freq === "weekly" && (
                  <div className="flex items-center gap-1 flex-wrap">
                    <span className="text-xs mr-2">On:</span>
                    {(["MON","TUE","WED","THU","FRI","SAT","SUN"] as const).map((d) => (
                      <button
                        key={d}
                        type="button"
                        onClick={() =>
                          setWeekDays((ds) =>
                            ds.includes(d) ? ds.filter((x) => x !== d) : [...ds, d],
                          )
                        }
                        className={`px-2 py-0.5 text-[10px] font-mono rounded ${
                          weekDays.includes(d)
                            ? "bg-slate-900 text-white"
                            : "border border-slate-200 text-slate-600 hover:bg-slate-50 bg-white"
                        }`}
                      >
                        {d}
                      </button>
                    ))}
                  </div>
                )}
                <div className="flex items-center gap-2">
                  <span className="text-xs">At</span>
                  <input
                    type="number"
                    min={0}
                    max={23}
                    value={atHour}
                    onChange={(e) => setAtHour(Number(e.target.value))}
                    className="w-14 px-2 py-1 text-xs border border-slate-300 rounded font-mono"
                  />
                  <span>:</span>
                  <input
                    type="number"
                    min={0}
                    max={59}
                    value={atMin}
                    onChange={(e) => setAtMin(Number(e.target.value))}
                    className="w-14 px-2 py-1 text-xs border border-slate-300 rounded font-mono"
                  />
                  <span className="text-[10px] text-slate-500">(UTC)</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs">Reminder</span>
                  <input
                    type="number"
                    min={0}
                    value={reminderMin}
                    onChange={(e) => setReminderMin(Number(e.target.value))}
                    className="w-20 px-2 py-1 text-xs border border-slate-300 rounded font-mono"
                  />
                  <span className="text-xs">min before</span>
                  <span className="text-[10px] text-slate-400">(0 = none)</span>
                </div>
                <p className="text-[10px] text-slate-500 font-mono">
                  cron equivalent → <code>{friendlyCron}</code>
                </p>
              </div>
            )}
            {mode === "cron" && (
              <>
                <input
                  value={cron}
                  onChange={(e) => setCron(e.target.value)}
                  className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                  placeholder="0 8 * * *"
                />
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {cronPresets.map((p) => (
                    <button
                      key={p.v}
                      onClick={() => setCron(p.v)}
                      className="px-1.5 py-0.5 text-[10px] rounded border border-slate-200 hover:bg-slate-100 font-mono"
                      title={p.v}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <p className="text-[10px] text-slate-500 mt-1">
                  Format: <code className="font-mono">min hour dom month dow</code> (cron). Times in UTC.
                </p>
              </>
            )}
            {mode === "interval" && (
              <div className="flex items-center gap-2">
                <span className="text-xs">every</span>
                <input
                  type="number"
                  min={1}
                  value={interval}
                  onChange={(e) => setIntervalMin(Number(e.target.value))}
                  className="w-24 px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                />
                <span className="text-xs">minutes</span>
              </div>
            )}
            {mode === "once" && (
              <input
                type="datetime-local"
                value={onceAt}
                onChange={(e) => setOnceAt(e.target.value)}
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
              />
            )}
          </div>
          {mode !== "once" && (
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              <span>Enable immediately</span>
            </label>
          )}
          {mode === "friendly" && reminderMin > 0 && (
            <p className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
              Note: reminder field captured but desktop notifications not yet wired — owner will see flow in /traffic.
            </p>
          )}
        </div>
        <div className="px-4 py-3 border-t border-slate-200 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => void submit()} disabled={busy}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function CalendarPage() {
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
          <CalendarContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
