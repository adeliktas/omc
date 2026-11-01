"use client";

import { useState, useCallback, useMemo } from "react";
import { Calendar, Clock, Repeat, Code2, X, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

// ── Types ──────────────────────────────────────────────────────────────────
export type ScheduleMode = "none" | "once" | "interval" | "cron";

export interface ScheduleData {
  mode: ScheduleMode;
  scheduledAt: Date | null;
  repeatIntervalMinutes: number | null;
  repeatCron: string;
  repeatEnabled: boolean;
}

interface SchedulePickerProps {
  value: ScheduleData;
  onChange: (data: ScheduleData) => void;
  disabled?: boolean;
  className?: string;
}

// ── Constants ──────────────────────────────────────────────────────────────
const INTERVAL_PRESETS = [
  { label: "minute", minutes: 1 },
  { label: "5 minutes", minutes: 5 },
  { label: "15 minutes", minutes: 15 },
  { label: "30 minutes", minutes: 30 },
  { label: "hour", minutes: 60 },
  { label: "2 hours", minutes: 120 },
  { label: "6 hours", minutes: 360 },
  { label: "12 hours", minutes: 720 },
  { label: "day", minutes: 1440 },
  { label: "2 days", minutes: 2880 },
  { label: "week", minutes: 10080 },
  { label: "2 weeks", minutes: 20160 },
  { label: "month (30d)", minutes: 43200 },
];

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
] as const;

// ── Helpers ────────────────────────────────────────────────────────────────
function toLocalISO(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalISO(val: string): Date {
  return new Date(val);
}

function startOfMonth(year: number, month: number): Date {
  return new Date(year, month, 1);
}

function daysInMonth(year: number, month: number): number {
  return new Date(year, month + 1, 0).getDate();
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

// ── Mini-calendar ──────────────────────────────────────────────────────────
function MiniCalendar({
  selected,
  onSelect,
}: {
  selected: Date | null;
  onSelect: (d: Date) => void;
}) {
  const today = new Date();
  const [viewYear, setViewYear] = useState(today.getFullYear());
  const [viewMonth, setViewMonth] = useState(today.getMonth());

  const firstDay = startOfMonth(viewYear, viewMonth);
  const total = daysInMonth(viewYear, viewMonth);
  // Monday-indexed start offset (0 = Mon, 6 = Sun)
  const startDow = firstDay.getDay() === 0 ? 6 : firstDay.getDay() - 1;

  const days: (Date | null)[] = [];
  for (let i = 0; i < startDow; i++) days.push(null);
  for (let d = 1; d <= total; d++) days.push(new Date(viewYear, viewMonth, d));

  // Fill remaining week
  const remaining = 7 - (days.length % 7);
  if (remaining < 7) {
    for (let i = 0; i < remaining; i++) days.push(null);
  }

  const prevMonth = () => {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(viewYear - 1); }
    else setViewMonth(viewMonth - 1);
  };
  const nextMonth = () => {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(viewYear + 1); }
    else setViewMonth(viewMonth + 1);
  };

  return (
    <div className="w-[252px] rounded-lg border bg-white p-3 shadow-lg">
      {/* Header */}
      <div className="mb-2 flex items-center justify-between">
        <button onClick={prevMonth} className="rounded p-1 hover:bg-slate-100">
          <ChevronLeft className="h-4 w-4 text-slate-600" />
        </button>
        <span className="text-xs font-semibold text-slate-700">
          {MONTHS[viewMonth]} {viewYear}
        </span>
        <button onClick={nextMonth} className="rounded p-1 hover:bg-slate-100">
          <ChevronRight className="h-4 w-4 text-slate-600" />
        </button>
      </div>

      {/* Day names */}
      <div className="mb-1 grid grid-cols-7">
        {DAYS.map((d) => (
          <div key={d} className="py-0.5 text-center text-[10px] font-semibold text-slate-400">
            {d[0]}
          </div>
        ))}
      </div>

      {/* Day grid */}
      <div className="grid grid-cols-7 gap-px">
        {days.map((d, i) => {
          if (!d) return <div key={`empty-${i}`} className="aspect-square" />;
          const isSel = selected && sameDay(d, selected);
          const isToday = sameDay(d, today);
          return (
            <button
              key={d.toISOString()}
              onClick={() => onSelect(d)}
              className={cn(
                "flex aspect-square items-center justify-center rounded text-xs transition",
                isSel
                  ? "bg-blue-600 font-semibold text-white"
                  : isToday
                    ? "border border-blue-300 font-semibold text-blue-700 hover:bg-blue-50"
                    : "text-slate-700 hover:bg-slate-100",
              )}
            >
              {d.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Time Input ─────────────────────────────────────────────────────────────
function TimeInput({
  date,
  onChange,
}: {
  date: Date;
  onChange: (d: Date) => void;
}) {
  const hours = Array.from({ length: 24 }, (_, i) => i);
  const minutes = Array.from({ length: 12 }, (_, i) => i * 5);

  return (
    <div className="flex items-center gap-1">
      <select
        value={date.getHours()}
        onChange={(e) => {
          const d = new Date(date);
          d.setHours(Number(e.target.value));
          onChange(d);
        }}
        className="h-8 rounded border px-2 text-xs font-mono text-slate-700"
      >
        {hours.map((h) => (
          <option key={h} value={h}>{String(h).padStart(2, "0")}</option>
        ))}
      </select>
      <span className="text-xs text-slate-400">:</span>
      <select
        value={date.getMinutes()}
        onChange={(e) => {
          const d = new Date(date);
          d.setMinutes(Number(e.target.value));
          onChange(d);
        }}
        className="h-8 rounded border px-2 text-xs font-mono text-slate-700"
      >
        {minutes.map((m) => (
          <option key={m} value={m}>{String(m).padStart(2, "0")}</option>
        ))}
      </select>
    </div>
  );
}

// ── Quick datetime options ─────────────────────────────────────────────────
function QuickDatetimeSelector({
  date,
  onChange,
}: {
  date: Date;
  onChange: (d: Date) => void;
}) {
  const quick = useMemo(() => {
    const now = new Date();
    return [
      { label: "Now + 5 min", value: new Date(now.getTime() + 5 * 60000) },
      { label: "Now + 15 min", value: new Date(now.getTime() + 15 * 60000) },
      { label: "Now + 30 min", value: new Date(now.getTime() + 30 * 60000) },
      { label: "Now + 1 hour", value: new Date(now.getTime() + 60 * 60000) },
      { label: "Now + 2 hours", value: new Date(now.getTime() + 120 * 60000) },
      { label: "Now + 6 hours", value: new Date(now.getTime() + 360 * 60000) },
      { label: "Tomorrow 9 AM", value: (() => {
        const d = new Date(now); d.setDate(d.getDate() + 1);
        d.setHours(9, 0, 0, 0); return d;
      })() },
      { label: "Tomorrow 2 PM", value: (() => {
        const d = new Date(now); d.setDate(d.getDate() + 1);
        d.setHours(14, 0, 0, 0); return d;
      })() },
      { label: "Next Mon 9 AM", value: (() => {
        const d = new Date(now);
        d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
        d.setHours(9, 0, 0, 0); return d;
      })() },
    ];
  }, []);

  return (
    <div className="flex flex-wrap gap-1.5">
      {quick.map((opt) => (
        <button
          key={opt.label}
          type="button"
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-[11px] transition",
            sameDay(date, opt.value) && date.getHours() === opt.value.getHours() && date.getMinutes() === opt.value.getMinutes()
              ? "border-blue-400 bg-blue-50 text-blue-700"
              : "border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────
export function SchedulePicker({ value, onChange, disabled, className }: SchedulePickerProps) {
  const { mode, scheduledAt, repeatIntervalMinutes, repeatCron, repeatEnabled } = value;

  const setMode = useCallback((m: ScheduleMode) => {
    onChange({
      mode: m,
      scheduledAt: m === "once" ? (scheduledAt ?? new Date(Date.now() + 30 * 60000)) : null,
      repeatIntervalMinutes: m === "interval" ? (repeatIntervalMinutes ?? 30) : null,
      repeatCron: m === "cron" ? (repeatCron || "*/30 * * * *") : "",
      repeatEnabled: m !== "none",
    });
  }, [onChange, scheduledAt, repeatIntervalMinutes, repeatCron]);

  const setDate = useCallback((d: Date) => {
    onChange({ ...value, scheduledAt: d });
  }, [onChange, value]);

  const setInterval = useCallback((mins: number) => {
    onChange({ ...value, mode: "interval", repeatIntervalMinutes: mins, repeatEnabled: true });
  }, [onChange, value]);

  const setCron = useCallback((c: string) => {
    onChange({ ...value, mode: "cron", repeatCron: c, repeatEnabled: true });
  }, [onChange, value]);

  const clear = useCallback(() => {
    onChange({
      mode: "none",
      scheduledAt: null,
      repeatIntervalMinutes: null,
      repeatCron: "",
      repeatEnabled: false,
    });
  }, [onChange]);

  const modes: { id: ScheduleMode; icon: React.ReactNode; label: string }[] = [
    { id: "none", icon: <X className="h-3.5 w-3.5" />, label: "No schedule" },
    { id: "once", icon: <Clock className="h-3.5 w-3.5" />, label: "Run once at" },
    { id: "interval", icon: <Repeat className="h-3.5 w-3.5" />, label: "Repeat every" },
    { id: "cron", icon: <Code2 className="h-3.5 w-3.5" />, label: "Crontab" },
  ];

  return (
    <div className={cn("space-y-3", className)}>
      {/* Mode selector */}
      <div className="flex flex-wrap gap-1">
        {modes.map((m) => (
          <button
            key={m.id}
            type="button"
            disabled={disabled}
            onClick={() => setMode(m.id)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition",
              mode === m.id
                ? "border-blue-300 bg-blue-50 text-blue-700"
                : "border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50",
              disabled && "opacity-50 cursor-not-allowed",
            )}
          >
            {m.icon}
            {m.label}
          </button>
        ))}
        {mode !== "none" && (
          <button
            type="button"
            disabled={disabled}
            onClick={clear}
            className="inline-flex items-center rounded-lg border border-red-200 px-2 py-1.5 text-xs text-red-500 hover:bg-red-50 transition"
            title="Clear schedule"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* Run once: datetime selector */}
      {mode === "once" && scheduledAt && (
        <div className="space-y-3 rounded-lg border bg-white p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Task will move from inbox → active when this time arrives.
          </p>

          {/* Quick selects */}
          <QuickDatetimeSelector date={scheduledAt} onChange={setDate} />

          {/* Custom picker row */}
          <div className="flex flex-wrap items-start gap-3">
            <MiniCalendar selected={scheduledAt} onSelect={(d) => {
              const next = new Date(d);
              next.setHours(scheduledAt.getHours(), scheduledAt.getMinutes(), 0, 0);
              setDate(next);
            }} />
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Calendar className="h-4 w-4 text-slate-400" />
                <span className="text-xs text-slate-600">
                  {scheduledAt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" })}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Clock className="h-4 w-4 text-slate-400" />
                <TimeInput date={scheduledAt} onChange={setDate} />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Interval repeat */}
      {mode === "interval" && (
        <div className="space-y-2 rounded-lg border bg-white p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Task clones itself {repeatIntervalMinutes ? `every ${INTERVAL_PRESETS.find(p => p.minutes === repeatIntervalMinutes)?.label ?? `${repeatIntervalMinutes} minutes`}` : ""} after being marked done.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {INTERVAL_PRESETS.map((preset) => (
              <button
                key={preset.minutes}
                type="button"
                disabled={disabled}
                onClick={() => setInterval(preset.minutes)}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-[11px] transition",
                  repeatIntervalMinutes === preset.minutes
                    ? "border-emerald-400 bg-emerald-50 text-emerald-700"
                    : "border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50",
                  disabled && "opacity-50 cursor-not-allowed",
                )}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Cron tab */}
      {mode === "cron" && (
        <div className="space-y-2 rounded-lg border bg-white p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Crontab format: minute hour day-of-month month day-of-week
          </p>
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-1.5">
              {[
                { label: "Every 5 min", cron: "*/5 * * * *" },
                { label: "Every 15 min", cron: "*/15 * * * *" },
                { label: "Every 30 min", cron: "*/30 * * * *" },
                { label: "Every hour", cron: "0 * * * *" },
                { label: "Daily 9 AM", cron: "0 9 * * *" },
                { label: "Weekdays 9 AM", cron: "0 9 * * 1-5" },
                { label: "Mon 9 AM", cron: "0 9 * * 1" },
                { label: "Daily midnight", cron: "0 0 * * *" },
              ].map((p) => (
                <button
                  key={p.cron}
                  type="button"
                  disabled={disabled}
                  onClick={() => setCron(p.cron)}
                  className={cn(
                    "rounded-full border px-2.5 py-0.5 text-[11px] transition",
                    repeatCron === p.cron
                      ? "border-violet-400 bg-violet-50 text-violet-700"
                      : "border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50",
                    disabled && "opacity-50 cursor-not-allowed",
                  )}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={repeatCron}
              onChange={(e) => setCron(e.target.value)}
              disabled={disabled}
              placeholder="*/30 * * * *"
              className="w-full rounded-lg border px-3 py-1.5 text-xs font-mono text-slate-700 placeholder:text-slate-400 focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
            />
          </div>
        </div>
      )}
    </div>
  );
}
