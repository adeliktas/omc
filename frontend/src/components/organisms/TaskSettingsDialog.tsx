"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Paperclip, Trash2, UploadCloud, X } from "lucide-react";

import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { getApiBaseUrl } from "@/lib/api-base";
import { cn } from "@/lib/utils";
import { TeamCyclePanel } from "@/components/organisms/TeamCyclePanel";

/**
 * Every tunable value on a task, in one place.
 *
 * These fields were reachable only through the API: an owner had to curl a
 * PATCH to route a task to the coding-harness lane, point it at a folder, pin
 * a cheap model, or attach the image it was supposed to read. The board could
 * not express the work it was being asked to run.
 */

export interface TaskSettingsValues {
  execution_lane: string | null;
  execution_mode: string | null;
  workspace_path: string | null;
  harness_provider: string | null;
  harness_model: string | null;
  thinking_level: string | null;
  complexity: number | null;
  max_seconds_budget: number | null;
  max_tokens_budget: number | null;
  repeat_enabled: boolean;
  repeat_interval_minutes: number | null;
}

interface Attachment {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  storage_path: string;
}

const LANES = [
  { value: "", label: "Auto (title prefix)" },
  { value: "harness", label: "Coding harness — vendor CLI, edits files" },
  { value: "agent", label: "Gateway agent — chat-style worker" },
];

const MODES = [
  { value: "single", label: "Single agent — one harness run (legacy)" },
  {
    value: "team",
    label: "Coordinated team — coordinator, PM, developers, independent QA, then review",
  },
];

// Mirrors the harness CLI vocabulary. "off" is the cheap default for
// transcription-shaped steps; the runner falls back to "low".
const THINKING = ["", "off", "minimal", "low", "medium", "high", "xhigh", "max"];

function authHeaders(json = true): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers["Content-Type"] = "application/json";
  if (isLocalAuthMode()) {
    const token = getLocalAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        {label}
      </span>
      {children}
      {hint && <span className="mt-0.5 block text-[10px] text-slate-400">{hint}</span>}
    </label>
  );
}

export function TaskSettingsDialog({
  open,
  onClose,
  boardId,
  taskId,
  taskTitle,
  initial,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  boardId: string;
  taskId: string;
  taskTitle: string;
  initial: Partial<TaskSettingsValues>;
  onSaved?: () => void;
}) {
  const [values, setValues] = useState<Partial<TaskSettingsValues>>(initial);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const base = `${getApiBaseUrl()}/api/v1/boards/${boardId}/tasks/${taskId}`;

  useEffect(() => {
    if (open) setValues(initial);
  }, [open, initial]);

  const loadAttachments = useCallback(async () => {
    try {
      const res = await fetch(`${base}/attachments`, { headers: authHeaders(false) });
      if (res.ok) setAttachments((await res.json()).items ?? []);
    } catch {
      /* listing is best-effort; the dialog still edits settings */
    }
  }, [base]);

  useEffect(() => {
    if (open) void loadAttachments();
  }, [open, loadAttachments]);

  const set = <K extends keyof TaskSettingsValues>(key: K, value: TaskSettingsValues[K]) =>
    setValues((prev) => ({ ...prev, [key]: value }));

  const upload = useCallback(
    async (files: FileList | File[]) => {
      setBusy(true);
      setError(null);
      try {
        for (const file of Array.from(files)) {
          const body = new FormData();
          body.append("file", file);
          const res = await fetch(`${base}/attachments`, {
            method: "POST",
            headers: authHeaders(false),
            body,
          });
          if (!res.ok) throw new Error(`${file.name}: HTTP ${res.status}`);
        }
        await loadAttachments();
      } catch (caught) {
        setError(String(caught));
      } finally {
        setBusy(false);
      }
    },
    [base, loadAttachments],
  );

  const removeAttachment = async (id: string) => {
    setBusy(true);
    try {
      await fetch(`${base}/attachments/${id}`, { method: "DELETE", headers: authHeaders(false) });
      await loadAttachments();
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      // Send empty strings as null so clearing a field really clears it.
      const payload: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(values)) {
        if (key === "max_seconds_budget") continue; // Supported on create, not PATCH.
        payload[key] = value === "" ? null : value;
      }
      const res = await fetch(base, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      onSaved?.();
      onClose();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setBusy(false);
    }
  };

  const isHarness = (values.execution_lane ?? "") === "harness";

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="truncate">Task settings — {taskTitle}</DialogTitle>
          <DialogDescription>
            Everything the runner reads from this task. Blank means “use the default”.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <section className="rounded-lg border border-slate-200 p-3">
            <h3 className="mb-2 text-xs font-semibold text-slate-700">Execution</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Lane" hint="Which worker executes this task.">
                <select
                  value={values.execution_lane ?? ""}
                  onChange={(e) => set("execution_lane", e.target.value || null)}
                  className="mt-1 w-full rounded border border-slate-200 px-2 py-1 text-xs"
                >
                  {LANES.map((l) => (
                    <option key={l.value} value={l.value}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Mode"
                hint={
                  values.execution_mode === "team"
                    ? "Harness lane only, runs in the runner's own checkout."
                    : "Team mode is opt-in per task."
                }
              >
                <select
                  value={values.execution_mode ?? "single"}
                  disabled={!isHarness}
                  onChange={(e) => set("execution_mode", e.target.value)}
                  className="mt-1 w-full rounded border border-slate-200 px-2 py-1 text-xs"
                >
                  {MODES.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Workspace path"
                hint={isHarness ? "Absolute folder the run works in." : "Harness lane only."}
              >
                <Input
                  className="mt-1 text-xs"
                  placeholder="/srv/my-project"
                  disabled={!isHarness}
                  value={values.workspace_path ?? ""}
                  onChange={(e) => set("workspace_path", e.target.value || null)}
                />
              </Field>
            </div>
          </section>

          {values.execution_mode === "team" && <TeamCyclePanel boardId={boardId} taskId={taskId} />}

          <section className="rounded-lg border border-slate-200 p-3">
            <h3 className="mb-2 text-xs font-semibold text-slate-700">
              Engine <span className="font-normal text-slate-400">— blank uses the ranking board</span>
            </h3>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Provider">
                <Input
                  className="mt-1 text-xs"
                  placeholder="deepseek-sub"
                  value={values.harness_provider ?? ""}
                  onChange={(e) => set("harness_provider", e.target.value || null)}
                />
              </Field>
              <Field label="Model">
                <Input
                  className="mt-1 text-xs"
                  placeholder="deepseek-flash"
                  value={values.harness_model ?? ""}
                  onChange={(e) => set("harness_model", e.target.value || null)}
                />
              </Field>
              <Field label="Thinking" hint="Pinned engines are never upgraded.">
                <select
                  value={values.thinking_level ?? ""}
                  onChange={(e) => set("thinking_level", e.target.value || null)}
                  className="mt-1 w-full rounded border border-slate-200 px-2 py-1 text-xs"
                >
                  {THINKING.map((t) => (
                    <option key={t} value={t}>
                      {t || "default (low)"}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </section>

          <section className="rounded-lg border border-slate-200 p-3">
            <h3 className="mb-2 text-xs font-semibold text-slate-700">Budgets and cadence</h3>
            <div className="grid gap-3 sm:grid-cols-4">
              <Field label="Complexity">
                <Input
                  type="number" min={1} max={5} className="mt-1 text-xs"
                  value={values.complexity ?? ""}
                  onChange={(e) => set("complexity", e.target.value ? Number(e.target.value) : null)}
                />
              </Field>
              <Field label="Seconds" hint="Read-only; cannot be changed after creation.">
                <Input
                  type="number" className="mt-1 text-xs"
                  value={values.max_seconds_budget ?? ""}
                  readOnly
                />
              </Field>
              <Field label="Tokens">
                <Input
                  type="number" min={0} className="mt-1 text-xs"
                  value={values.max_tokens_budget ?? ""}
                  onChange={(e) =>
                    set("max_tokens_budget", e.target.value ? Number(e.target.value) : null)
                  }
                />
              </Field>
              <Field label="Repeat (min)" hint="Blank = one-shot.">
                <Input
                  type="number" min={1} className="mt-1 text-xs"
                  value={values.repeat_interval_minutes ?? ""}
                  onChange={(e) => {
                    const next = e.target.value ? Number(e.target.value) : null;
                    set("repeat_interval_minutes", next);
                    set("repeat_enabled", next !== null);
                  }}
                />
              </Field>
            </div>
          </section>

          <section
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (e.dataTransfer.files?.length) void upload(e.dataTransfer.files);
            }}
            className={cn(
              "rounded-lg border-2 border-dashed p-3 transition",
              dragOver ? "border-blue-400 bg-blue-50" : "border-slate-200",
            )}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-slate-700">
                <Paperclip className="mr-1 inline h-3 w-3" />
                Attachments
              </h3>
              <Button
                type="button" size="sm" variant="ghost" disabled={busy}
                onClick={() => fileInput.current?.click()}
                className="text-xs"
              >
                <UploadCloud className="mr-1 h-3.5 w-3.5" />
                Choose files
              </Button>
              <input
                ref={fileInput} type="file" multiple className="hidden"
                onChange={(e) => e.target.files && void upload(e.target.files)}
              />
            </div>
            <p className="mt-1 text-[10px] text-slate-400">
              Drop files here. The agent reads them from disk by absolute path.
            </p>
            <ul className="mt-2 space-y-1">
              {attachments.map((a) => (
                <li
                  key={a.id}
                  className="flex items-center justify-between gap-2 rounded bg-slate-50 px-2 py-1"
                >
                  <span className="truncate font-mono text-[10px] text-slate-600" title={a.storage_path}>
                    {a.filename}
                  </span>
                  <span className="shrink-0 text-[10px] text-slate-400">
                    {humanBytes(a.size_bytes)}
                  </span>
                  <button
                    type="button" disabled={busy}
                    onClick={() => void removeAttachment(a.id)}
                    className="shrink-0 text-slate-400 hover:text-rose-600"
                    aria-label={`Remove ${a.filename}`}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </li>
              ))}
              {attachments.length === 0 && (
                <li className="text-[10px] text-slate-400">No files attached.</li>
              )}
            </ul>
          </section>

          {error && (
            <p className="flex items-start gap-1 text-xs text-rose-700">
              <X className="mt-0.5 h-3 w-3 shrink-0" />
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy} className="text-xs">
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={busy} className="text-xs">
            {busy ? "Saving…" : "Save settings"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
