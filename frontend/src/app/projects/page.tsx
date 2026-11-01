"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  FolderKanban,
  Plus,
  RefreshCw,
  CheckCircle2,
  PlayCircle,
  Pause,
  Eye,
  AlertTriangle,
  Trash2,
  Pencil,
} from "lucide-react";
import { SignedIn, SignedOut, SignInButton, useAuth } from "@/auth/clerk";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import { SettingsGear } from "@/components/ui/settings-gear";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

type Project = {
  id: string;
  kind: "group" | "board";
  name: string;
  description: string;
  boards: { id: string; name: string; autonomous_enabled?: boolean }[];
  board_count: number;
  total: number;
  done: number;
  in_progress: number;
  review: number;
  inbox: number;
  cancelled: number;
  blocked: number;
  progress_pct: number;
  status: "planning" | "active" | "complete";
  priority: "high" | "medium" | "normal" | "low" | null;
  last_agent: {
    agent_id: string | null;
    agent_name: string | null;
    ts: string | null;
    event_type: string;
  } | null;
  created_at: string | null;
  autonomous_enabled?: boolean;  // T105 Phase 5
};

const STATUS_STYLE: Record<string, { icon: any; cls: string; label: string }> = {
  planning: { icon: Pause, cls: "bg-slate-100 text-slate-700 border-slate-200", label: "Planning" },
  active: { icon: PlayCircle, cls: "bg-emerald-100 text-emerald-700 border-emerald-200", label: "Active" },
  complete: { icon: CheckCircle2, cls: "bg-blue-100 text-blue-700 border-blue-200", label: "Complete" },
};

const PRIORITY_STYLE: Record<string, string> = {
  high: "bg-rose-100 text-rose-700 border-rose-200",
  medium: "bg-amber-100 text-amber-700 border-amber-200",
  normal: "bg-slate-100 text-slate-700 border-slate-200",
  low: "bg-slate-50 text-slate-500 border-slate-200",
};

const fmtAgo = (iso: string | null): string => {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`;
  return `${Math.round(ms / 86_400_000)}d ago`;
};

function ProjectsContent() {
  const { getToken } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "planning" | "complete">("all");
  const [showCreate, setShowCreate] = useState(false);

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
      const r = await fetch(`${getApiBaseUrl()}/api/v1/projects`, {
        headers: await authHeader(),
      });
      if (!r.ok) throw new Error(String(r.status));
      setProjects(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [authHeader]);

  useEffect(() => {
    void reload();
    const id = setInterval(() => void reload(), 30_000);
    return () => clearInterval(id);
  }, [reload]);

  const filtered = useMemo(
    () => (filter === "all" ? projects : projects.filter((p) => p.status === filter)),
    [projects, filter],
  );

  const totals = useMemo(
    () => ({
      projects: projects.length,
      active: projects.filter((p) => p.status === "active").length,
      planning: projects.filter((p) => p.status === "planning").length,
      complete: projects.filter((p) => p.status === "complete").length,
      tasks_total: projects.reduce((s, p) => s + p.total, 0),
      tasks_done: projects.reduce((s, p) => s + p.done, 0),
    }),
    [projects],
  );

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <FolderKanban className="h-6 w-6 text-slate-600" />
            Projects
          </h1>
          <p className="text-sm text-slate-600 mt-1">
            Roll-up over board groups + ungrouped boards. Progress %, status,
            priority, last agent that touched it. Click a row for boards.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void reload()}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setShowCreate(true)}>
            <Plus className="h-4 w-4 mr-1" />
            New Project
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
        <StatCard label="Projects" value={totals.projects} />
        <StatCard label="Active" value={totals.active} accent="emerald" />
        <StatCard label="Planning" value={totals.planning} accent="slate" />
        <StatCard label="Complete" value={totals.complete} accent="blue" />
        <StatCard label="Tasks total" value={totals.tasks_total} />
        <StatCard
          label="Tasks done"
          value={`${totals.tasks_done}/${totals.tasks_total}`}
          accent="emerald"
        />
      </div>

      <div className="flex gap-1">
        {(["all", "active", "planning", "complete"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1 text-xs rounded ${
              filter === f
                ? "bg-slate-900 text-white"
                : "border border-slate-200 text-slate-700 hover:bg-slate-50"
            }`}
          >
            {f}
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 text-red-700 p-3 text-sm">
          {error}
        </div>
      )}

      <div className="rounded-lg border border-slate-200 bg-white overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-3 py-2 text-left">Project</th>
              <th className="px-3 py-2 text-left">Progress</th>
              <th className="px-3 py-2 text-left">Status</th>
              <th className="px-3 py-2 text-left">Priority</th>
              <th className="px-3 py-2 text-left">Last agent</th>
              <th className="px-3 py-2 text-left">Boards</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-slate-500 text-xs">
                  No projects.{" "}
                  <button
                    className="underline text-blue-600"
                    onClick={() => setShowCreate(true)}
                  >
                    Create one
                  </button>
                  .
                </td>
              </tr>
            ) : (
              filtered.map((p) => (
                <ProjectRow key={p.id} project={p} authHeader={authHeader} onChanged={() => void reload()} />
              ))
            )}
          </tbody>
        </table>
      </div>

      {showCreate && (
        <CreateProjectDialog
          authHeader={authHeader}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            void reload();
          }}
        />
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | string;
  accent?: "emerald" | "slate" | "blue";
}) {
  const valColor =
    accent === "emerald"
      ? "text-emerald-700"
      : accent === "blue"
        ? "text-blue-700"
        : "text-slate-900";
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p className={`text-lg font-bold ${valColor}`}>{value}</p>
    </div>
  );
}

function ProjectRow({
  project,
  authHeader,
  onChanged,
}: {
  project: Project;
  authHeader: () => Promise<HeadersInit>;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [refineOpen, setRefineOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState(project.name);
  const [draftDesc, setDraftDesc] = useState(project.description);
  const [saving, setSaving] = useState(false);
  const status = STATUS_STYLE[project.status] || STATUS_STYLE.planning;
  const StatusIcon = status.icon;
  const remove = async () => {
    if (project.kind !== "group") {
      alert("Only group-based projects can be deleted. Boards stay where they are.");
      return;
    }
    if (!confirm(`Delete project "${project.name}"? Boards keep existing.`)) return;
    const r = await fetch(`${getApiBaseUrl()}/api/v1/projects/${project.id}`, {
      method: "DELETE",
      headers: await authHeader(),
    });
    if (!r.ok) alert(`delete failed: ${r.status}`);
    else onChanged();
  };

  const startEdit = () => {
    setDraftName(project.name);
    setDraftDesc(project.description);
    setEditing(true);
  };

  const save = async () => {
    if (!draftName.trim()) return;
    setSaving(true);
    try {
      const r = await fetch(`${getApiBaseUrl()}/api/v1/projects/${project.id}`, {
        method: "PATCH",
        headers: await authHeader(),
        body: JSON.stringify({
          name: draftName.trim(),
          description: draftDesc.trim() || null,
        }),
      });
      if (!r.ok) alert(`save failed: ${r.status}`);
      else {
        setEditing(false);
        onChanged();
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <tr className="hover:bg-slate-50 cursor-pointer" onClick={() => setOpen((x) => !x)}>
        <td className="px-3 py-2.5">
          {editing ? (
            <div onClick={(e) => e.stopPropagation()} className="space-y-1">
              <input
                type="text"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                className="w-full rounded border border-slate-300 px-2 py-1 text-sm font-medium text-slate-900"
              />
              <input
                type="text"
                value={draftDesc}
                onChange={(e) => setDraftDesc(e.target.value)}
                placeholder="Description (optional)"
                className="w-full rounded border border-slate-300 px-2 py-1 text-xs text-slate-600"
              />
              <div className="flex items-center gap-1">
                <button
                  onClick={() => void save()}
                  disabled={saving || !draftName.trim()}
                  className="rounded bg-emerald-600 px-2 py-0.5 text-[10px] font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Save"}
                </button>
                <button
                  onClick={() => setEditing(false)}
                  className="rounded border border-slate-300 px-2 py-0.5 text-[10px] font-semibold text-slate-600 hover:bg-slate-100"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div>
              <p className="font-medium text-slate-900">{project.name}</p>
              {project.description ? (
                <p className="text-xs text-slate-500 line-clamp-1">{project.description}</p>
              ) : null}
              <p className="text-[10px] text-slate-400 mt-0.5">
                {project.kind === "group" ? "project (group)" : "single board"} · {project.board_count} board{project.board_count !== 1 ? "s" : ""}
              </p>
            </div>
          )}
        </td>
        <td className="px-3 py-2.5">
          <div className="space-y-1 min-w-[160px]">
            <div className="flex items-center justify-between text-[10px] text-slate-500">
              <span>
                {project.done}/{project.total} tasks
              </span>
              <span className="font-mono">{project.progress_pct}%</span>
            </div>
            <div className="h-2 w-full bg-slate-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-emerald-500"
                style={{ width: `${project.progress_pct}%` }}
              />
            </div>
            {(project.review > 0 || project.blocked > 0 || project.in_progress > 0) && (
              <div className="flex gap-1 text-[10px]">
                {project.in_progress > 0 && (
                  <span className="text-emerald-700">{project.in_progress} active</span>
                )}
                {project.review > 0 && (
                  <span className="text-blue-700">{project.review} review</span>
                )}
                {project.blocked > 0 && (
                  <span className="text-rose-700">{project.blocked} blocked</span>
                )}
              </div>
            )}
          </div>
        </td>
        <td className="px-3 py-2.5">
          <div className="flex flex-col gap-1">
            <span
              className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${status.cls}`}
            >
              <StatusIcon className="h-3 w-3" />
              {status.label}
            </span>
            {project.autonomous_enabled && (
              <span
                className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium bg-indigo-100 text-indigo-700 border-indigo-200"
                title="PEV autonomous mode enabled — new tasks auto-plan + execute + verify"
              >
                ⚡ PEV ON
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2.5">
          {project.priority ? (
            <span
              className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                PRIORITY_STYLE[project.priority] || PRIORITY_STYLE.normal
              }`}
            >
              {project.priority}
            </span>
          ) : (
            <span className="text-[10px] text-slate-400">—</span>
          )}
        </td>
        <td className="px-3 py-2.5">
          {project.last_agent?.agent_name ? (
            <div className="text-xs">
              <p className="font-medium text-slate-800">{project.last_agent.agent_name}</p>
              <p className="text-[10px] text-slate-500">
                {project.last_agent.event_type} · {fmtAgo(project.last_agent.ts)}
              </p>
            </div>
          ) : (
            <span className="text-[10px] text-slate-400">no activity</span>
          )}
        </td>
        <td className="px-3 py-2.5 text-xs">
          {project.boards.slice(0, 2).map((b, i) => (
            <span key={b.id}>
              <a
                onClick={(e) => e.stopPropagation()}
                href={`/boards/${b.id}`}
                className="text-blue-600 hover:underline"
              >
                {b.name}
              </a>
              {i < Math.min(project.boards.length, 2) - 1 ? ", " : ""}
            </span>
          ))}
          {project.boards.length > 2 ? (
            <span className="text-slate-400"> +{project.boards.length - 2}</span>
          ) : null}
        </td>
        <td className="px-3 py-2.5 text-right">
          {project.kind === "group" && (
            <div className="inline-flex gap-1">
              <SettingsGear
                path={`/api/v1/projects/${project.id}`}
                title={project.name}
                values={{ name: project.name, description: project.description }}
                onSaved={onChanged}
              />
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  startEdit();
                }}
                className="p-1 rounded hover:bg-slate-100 text-slate-500"
                title="Rename / re-describe project"
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setRefineOpen(true);
                }}
                className="p-1 rounded hover:bg-indigo-50 text-indigo-600"
                title="Refine project with new info (smart-tier planner re-analyzes + appends)"
              >
                <span className="text-xs font-semibold px-1">+ AI</span>
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  void remove();
                }}
                className="p-1 rounded hover:bg-rose-50 text-rose-500"
                title="Delete project"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </td>
      </tr>
      {open && project.boards.length > 0 && (
        <tr className="bg-slate-50">
          <td colSpan={7} className="px-6 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-2">
              Boards in this project
            </p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              {project.boards.map((b) => (
                <a
                  key={b.id}
                  href={`/boards/${b.id}`}
                  className="rounded border border-slate-200 bg-white px-3 py-2 hover:border-blue-300 hover:bg-blue-50 transition text-sm"
                >
                  <Eye className="inline h-3 w-3 mr-1 text-slate-400" />
                  {b.name}
                </a>
              ))}
            </div>
          </td>
        </tr>
      )}
      {refineOpen && (
        <RefineProjectDialog
          projectId={project.id}
          projectName={project.name}
          authHeader={authHeader}
          onClose={() => setRefineOpen(false)}
          onRefined={() => {
            setRefineOpen(false);
            onChanged();
          }}
        />
      )}
    </>
  );
}

function RefineProjectDialog({
  projectId,
  projectName,
  authHeader,
  onClose,
  onRefined,
}: {
  projectId: string;
  projectName: string;
  authHeader: () => Promise<HeadersInit>;
  onClose: () => void;
  onRefined: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    added_boards: number;
    added_tasks: number;
    cross_deps: number;
    planner_model: string;
  } | null>(null);

  const submit = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `${getApiBaseUrl()}/api/v1/projects/${projectId}/refine`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(await authHeader()),
          },
          body: JSON.stringify({ additional_intent: text }),
        },
      );
      if (!r.ok) {
        const body = await r.text();
        setError(`${r.status}: ${body.slice(0, 200)}`);
      } else {
        const data = await r.json();
        setResult({
          added_boards: data.added_boards,
          added_tasks: data.added_tasks,
          cross_deps: data.cross_deps,
          planner_model: data.planner_model,
        });
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="bg-indigo-50/60">
      <td colSpan={7} className="px-6 py-4">
        <div className="rounded border border-indigo-200 bg-white p-3">
          <p className="text-xs font-semibold uppercase tracking-wider text-indigo-700">
            Refine project · {projectName}
          </p>
          <p className="text-[11px] text-slate-500 mt-0.5 mb-2">
            Planner re-analyzes the existing project + the new info below, then appends new boards / tasks where relevant. Existing rows are never removed.
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="New requirements, scope changes, additional features, bugs to track..."
            rows={4}
            className="w-full rounded border border-slate-300 px-2 py-1.5 text-sm font-mono"
            disabled={busy || !!result}
          />
          {error && (
            <p className="text-xs text-rose-700 mt-1">Error: {error}</p>
          )}
          {result && (
            <p className="text-xs text-emerald-700 mt-2">
              Done. +{result.added_boards} boards, +{result.added_tasks} tasks, +{result.cross_deps} deps. Planner: {result.planner_model}
            </p>
          )}
          <div className="mt-2 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded border border-slate-300 px-3 py-1 text-xs hover:bg-slate-50"
            >
              {result ? "Close" : "Cancel"}
            </button>
            {!result && (
              <button
                onClick={() => void submit()}
                disabled={busy || !text.trim()}
                className="rounded bg-indigo-600 px-3 py-1 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {busy ? "Analyzing..." : "Run planner"}
              </button>
            )}
            {result && (
              <button
                onClick={onRefined}
                className="rounded bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-700"
              >
                Refresh project
              </button>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}

type Template = {
  id: string;
  label: string;
  description: string;
  icon: string;
  board_count: number;
  task_count: number;
  phases: string[];
};

function CreateProjectDialog({
  authHeader,
  onClose,
  onCreated,
}: {
  authHeader: () => Promise<HeadersInit>;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [mode, setMode] = useState<"empty" | "template" | "intent">("template");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [intent, setIntent] = useState("");
  const [templateId, setTemplateId] = useState<string>("software");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`${getApiBaseUrl()}/api/v1/projects/templates`, {
          headers: await authHeader(),
        });
        if (r.ok) setTemplates(await r.json());
      } catch {
        /* ignore */
      }
    })();
  }, [authHeader]);

  const submit = async () => {
    if (!name.trim()) {
      setErr("name required");
      return;
    }
    if (mode === "intent" && !intent.trim()) {
      setErr("intent required for AI scaffold");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      let url: string;
      let body: any;
      if (mode === "empty") {
        url = `${getApiBaseUrl()}/api/v1/projects`;
        body = { name, description };
      } else if (mode === "template") {
        url = `${getApiBaseUrl()}/api/v1/projects/from-template`;
        body = { name, template_id: templateId, description: description || undefined };
      } else {
        url = `${getApiBaseUrl()}/api/v1/projects/from-intent`;
        body = { name, intent };
      }
      const r = await fetch(url, {
        method: "POST",
        headers: { ...(await authHeader()), "Content-Type": "application/json" },
        body: JSON.stringify(body),
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
      <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-sm font-semibold">New project</h2>
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

          {/* Mode tabs */}
          <div className="flex gap-1 border-b border-slate-200">
            {(["template", "intent", "empty"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-1.5 text-xs font-medium border-b-2 -mb-px transition ${
                  mode === m
                    ? "border-slate-900 text-slate-900"
                    : "border-transparent text-slate-500 hover:text-slate-700"
                }`}
              >
                {m === "template" ? "📋 From template" : m === "intent" ? "🤖 From intent (AI)" : "Empty"}
              </button>
            ))}
          </div>

          {/* Name (always shown) */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">Project name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={
                mode === "intent"
                  ? "e.g. Python Calculator Development"
                  : mode === "template"
                    ? "e.g. Customer Auth Service"
                    : "e.g. Agent Org Infrastructure"
              }
              className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
            />
          </div>

          {mode === "empty" && (
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">Description</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded font-mono"
                placeholder="What is this project about?"
              />
              <p className="text-[11px] text-slate-500 mt-1">
                Creates empty project (board_group). Attach boards manually after.
              </p>
            </div>
          )}

          {mode === "template" && (
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">Template</label>
              <div className="space-y-1.5">
                {templates.map((t) => (
                  <label
                    key={t.id}
                    className={`flex items-start gap-2 rounded-lg border p-2.5 cursor-pointer transition ${
                      templateId === t.id
                        ? "border-blue-400 bg-blue-50"
                        : "border-slate-200 hover:border-slate-300"
                    }`}
                  >
                    <input
                      type="radio"
                      name="template"
                      checked={templateId === t.id}
                      onChange={() => setTemplateId(t.id)}
                      className="mt-1"
                    />
                    <div className="flex-1">
                      <p className="text-sm font-medium">
                        {t.icon} {t.label}
                      </p>
                      <p className="text-[11px] text-slate-600 mt-0.5">{t.description}</p>
                      <p className="text-[10px] text-slate-500 mt-1 font-mono">
                        {t.board_count} boards · {t.task_count} seeded tasks · phases:{" "}
                        {t.phases.join(" → ")}
                      </p>
                    </div>
                  </label>
                ))}
                {templates.length === 0 && (
                  <p className="text-xs text-slate-500">Loading templates…</p>
                )}
              </div>
              <div className="mt-2">
                <label className="block text-xs font-medium text-slate-700 mb-1">
                  Description (optional override)
                </label>
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
                  placeholder="Leave blank to use template's default"
                />
              </div>
            </div>
          )}

          {mode === "intent" && (
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                Project intent (free-text — smart-tier planner decomposes)
              </label>
              <textarea
                value={intent}
                onChange={(e) => setIntent(e.target.value)}
                rows={6}
                className="w-full px-2 py-1.5 text-sm border border-slate-300 rounded"
                placeholder="Describe what you want. The smart-tier model (deepseek-v4-pro) will plan boards + tasks.

Example: 'Build a Python calculator with CLI support, history of past calculations, and ability to handle complex expressions. Should pass 80% test coverage and ship as a pip-installable wheel.'"
              />
              <p className="text-[11px] text-slate-500 mt-1">
                AI scaffold uses planner-tier model. May take 10-30s. Requires
                smart-tier provider online (deepseek / kimi / glm fallback).
              </p>
            </div>
          )}
        </div>
        <div className="px-4 py-3 border-t border-slate-200 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => void submit()} disabled={busy}>
            {busy
              ? mode === "intent"
                ? "Planning… (10-30s)"
                : "Creating…"
              : mode === "template"
                ? "Scaffold project"
                : mode === "intent"
                  ? "Plan + scaffold"
                  : "Create empty"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function ProjectsPage() {
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
          <ProjectsContent />
        </main>
      </SignedIn>
    </DashboardShell>
  );
}
