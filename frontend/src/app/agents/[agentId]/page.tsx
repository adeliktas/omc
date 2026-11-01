"use client";

export const dynamic = "force-dynamic";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { SignInButton, SignedIn, SignedOut, useAuth } from "@/auth/clerk";
import { getApiBaseUrl } from "@/lib/api-base";
import { getLocalAuthToken, isLocalAuthMode } from "@/auth/localAuth";

import { ApiError } from "@/api/mutator";
import {
  type getAgentApiV1AgentsAgentIdGetResponse,
  useDeleteAgentApiV1AgentsAgentIdDelete,
  useGetAgentApiV1AgentsAgentIdGet,
} from "@/api/generated/agents/agents";
import {
  type listActivityApiV1ActivityGetResponse,
  useListActivityApiV1ActivityGet,
} from "@/api/generated/activity/activity";
import {
  type listBoardsApiV1BoardsGetResponse,
  useListBoardsApiV1BoardsGet,
} from "@/api/generated/boards/boards";
import {
  formatRelativeTimestamp as formatRelative,
  formatTimestamp,
} from "@/lib/formatters";
import { useOrganizationMembership } from "@/lib/use-organization-membership";
import type {
  ActivityEventRead,
  AgentRead,
  BoardRead,
} from "@/api/generated/model";
import { AgentToolConfigEditor } from "@/components/agents/AgentToolConfigEditor";
import { Markdown } from "@/components/atoms/Markdown";
import { StatusPill } from "@/components/atoms/StatusPill";
import { DashboardSidebar } from "@/components/organisms/DashboardSidebar";
import { DashboardShell } from "@/components/templates/DashboardShell";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export default function AgentDetailPage() {
  const { isSignedIn } = useAuth();
  const router = useRouter();
  const params = useParams();
  const agentIdParam = params?.agentId;
  const agentId = Array.isArray(agentIdParam) ? agentIdParam[0] : agentIdParam;

  const { isAdmin } = useOrganizationMembership(isSignedIn);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const agentQuery = useGetAgentApiV1AgentsAgentIdGet<
    getAgentApiV1AgentsAgentIdGetResponse,
    ApiError
  >(agentId ?? "", {
    query: {
      enabled: Boolean(isSignedIn && isAdmin && agentId),
      refetchInterval: 30_000,
      refetchOnMount: "always",
      retry: false,
    },
  });

  const activityQuery = useListActivityApiV1ActivityGet<
    listActivityApiV1ActivityGetResponse,
    ApiError
  >(
    { limit: 200 },
    {
      query: {
        enabled: Boolean(isSignedIn && isAdmin),
        refetchInterval: 30_000,
        retry: false,
      },
    },
  );

  const boardsQuery = useListBoardsApiV1BoardsGet<
    listBoardsApiV1BoardsGetResponse,
    ApiError
  >(undefined, {
    query: {
      enabled: Boolean(isSignedIn && isAdmin),
      refetchInterval: 60_000,
      refetchOnMount: "always",
      retry: false,
    },
  });

  const agent: AgentRead | null =
    agentQuery.data?.status === 200 ? agentQuery.data.data : null;
  const events = useMemo<ActivityEventRead[]>(() => {
    if (activityQuery.data?.status !== 200) return [];
    return activityQuery.data.data.items ?? [];
  }, [activityQuery.data]);
  const boards = useMemo<BoardRead[]>(() => {
    if (boardsQuery.data?.status !== 200) return [];
    return boardsQuery.data.data.items ?? [];
  }, [boardsQuery.data]);

  const agentEvents = useMemo(() => {
    if (!agent) return [];
    return events.filter((event) => event.agent_id === agent.id);
  }, [events, agent]);
  const linkedBoard =
    !agent?.board_id || agent?.is_gateway_main
      ? null
      : (boards.find((board) => board.id === agent.board_id) ?? null);

  const deleteMutation = useDeleteAgentApiV1AgentsAgentIdDelete<ApiError>({
    mutation: {
      onSuccess: () => {
        setDeleteOpen(false);
        router.push("/agents");
      },
      onError: (err) => {
        setDeleteError(err.message || "Something went wrong.");
      },
    },
  });

  const isLoading =
    agentQuery.isLoading || activityQuery.isLoading || boardsQuery.isLoading;
  const error =
    agentQuery.error?.message ??
    activityQuery.error?.message ??
    boardsQuery.error?.message ??
    null;

  const isDeleting = deleteMutation.isPending;
  const agentStatus = agent?.status ?? "unknown";

  const handleDelete = () => {
    if (!agentId || !isSignedIn) return;
    setDeleteError(null);
    deleteMutation.mutate({ agentId });
  };

  return (
    <DashboardShell>
      <SignedOut>
        <div className="flex h-full flex-col items-center justify-center gap-4 rounded-2xl surface-panel p-10 text-center">
          <p className="text-sm text-muted">Sign in to view agents.</p>
          <SignInButton
            mode="modal"
            forceRedirectUrl="/agents"
            signUpForceRedirectUrl="/agents"
          >
            <Button>Sign in</Button>
          </SignInButton>
        </div>
      </SignedOut>
      <SignedIn>
        <DashboardSidebar />
        {!isAdmin ? (
          <div className="flex h-full flex-col gap-6 rounded-2xl surface-panel p-4 md:p-8">
            <div className="rounded-xl border border-[color:var(--border)] bg-[color:var(--surface)] px-6 py-5 text-sm text-muted">
              Only organization owners and admins can access agents.
            </div>
          </div>
        ) : (
          <div className="flex h-full flex-col gap-6 rounded-2xl surface-panel p-4 md:p-8">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-2">
                <p className="text-xs font-semibold uppercase tracking-[0.3em] text-quiet">
                  Agents
                </p>
                <h1 className="text-2xl font-semibold text-strong">
                  {agent?.name ?? "Agent"}
                </h1>
                <p className="text-sm text-muted">
                  Review agent health, session binding, and recent activity.
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  onClick={() => router.push("/agents")}
                >
                  Back to agents
                </Button>
                {agent ? (
                  <Link
                    href={`/agents/${agent.id}/edit`}
                    className="inline-flex h-10 items-center justify-center rounded-xl border border-[color:var(--border)] px-4 text-sm font-semibold text-muted transition hover:border-[color:var(--accent)] hover:text-[color:var(--accent)]"
                  >
                    Edit
                  </Link>
                ) : null}
                {agent ? (
                  <Button variant="outline" onClick={() => setDeleteOpen(true)}>
                    Delete
                  </Button>
                ) : null}
              </div>
            </div>

            {error ? (
              <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-xs text-muted">
                {error}
              </div>
            ) : null}

            {isLoading ? (
              <div className="flex flex-1 items-center justify-center text-sm text-muted">
                Loading agent details…
              </div>
            ) : agent ? (
              <>
              {/* T104 (2026-05-24): workspace MD viewer — SOUL.md, IDENTITY.md, etc. */}
              <WorkspaceDefinitions agentId={agent.id} />
              {/* Per-agent tool/skill/MCP overrides. */}
              <AgentToolConfigEditor agentId={agent.id} />
              <div className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
                <div className="space-y-6">
                  <div className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] p-5">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Overview
                        </p>
                        <p className="mt-1 text-lg font-semibold text-strong">
                          {agent.name}
                        </p>
                      </div>
                      <StatusPill status={agentStatus} />
                    </div>
                    <div className="mt-4 grid gap-4 md:grid-cols-2">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Agent ID
                        </p>
                        <p className="mt-1 text-sm text-muted">{agent.id}</p>
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Session key
                        </p>
                        <p className="mt-1 text-sm text-muted">
                          {agent.openclaw_session_id ?? "—"}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Board
                        </p>
                        {agent.is_gateway_main ? (
                          <p className="mt-1 text-sm text-strong">
                            Gateway main (no board)
                          </p>
                        ) : linkedBoard ? (
                          <Link
                            href={`/boards/${linkedBoard.id}`}
                            className="mt-1 inline-flex text-sm font-medium text-[color:var(--accent)] transition hover:underline"
                          >
                            {linkedBoard.name}
                          </Link>
                        ) : (
                          <p className="mt-1 text-sm text-strong">—</p>
                        )}
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Last seen
                        </p>
                        <p className="mt-1 text-sm text-strong">
                          {formatRelative(agent.last_seen_at)}
                        </p>
                        <p className="text-xs text-quiet">
                          {formatTimestamp(agent.last_seen_at)}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Updated
                        </p>
                        <p className="mt-1 text-sm text-muted">
                          {formatTimestamp(agent.updated_at)}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                          Created
                        </p>
                        <p className="mt-1 text-sm text-muted">
                          {formatTimestamp(agent.created_at)}
                        </p>
                      </div>
                    </div>
                  </div>

                  <div className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] p-5">
                    <div className="flex items-center justify-between">
                      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                        Health
                      </p>
                      <StatusPill status={agentStatus} />
                    </div>
                    <div className="mt-4 grid gap-3 text-sm text-muted">
                      <div className="flex items-center justify-between">
                        <span>Heartbeat window</span>
                        <span>{formatRelative(agent.last_seen_at)}</span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span>Session binding</span>
                        <span>
                          {agent.openclaw_session_id ? "Bound" : "Unbound"}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span>Status</span>
                        <span className="text-strong">{agentStatus}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-5">
                  <div className="mb-4 flex items-center justify-between">
                    <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
                      Activity
                    </p>
                    <p className="text-xs text-quiet">
                      {agentEvents.length} events
                    </p>
                  </div>
                  <div className="space-y-3">
                    {agentEvents.length === 0 ? (
                      <div className="rounded-lg border border-dashed border-[color:var(--border)] bg-[color:var(--surface)] p-4 text-sm text-muted">
                        No activity yet for this agent.
                      </div>
                    ) : (
                      agentEvents.map((event) => (
                        <div
                          key={event.id}
                          className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface)] p-4 text-sm text-muted"
                        >
                          {event.message?.trim() ? (
                            <div className="select-text cursor-text leading-relaxed text-strong break-words">
                              <Markdown
                                content={event.message}
                                variant="comment"
                              />
                            </div>
                          ) : (
                            <p className="font-medium text-strong">
                              {event.event_type}
                            </p>
                          )}
                          <p className="mt-1 text-xs text-quiet">
                            {formatTimestamp(event.created_at)}
                          </p>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              </div>
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center text-sm text-muted">
                Agent not found.
              </div>
            )}
          </div>
        )}
      </SignedIn>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent aria-label="Delete agent">
          <DialogHeader>
            <DialogTitle>Delete agent</DialogTitle>
            <DialogDescription>
              This will remove {agent?.name}. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {deleteError ? (
            <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-3 text-xs text-muted">
              {deleteError}
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleDelete} disabled={isDeleting}>
              {isDeleting ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </DashboardShell>
  );
}


// T104 (2026-05-24): per-agent workspace MD viewer.
// Fetches /api/v1/agents/{id}/workspace-files which lists + reads
// SOUL.md / IDENTITY.md / USER.md / AGENTS.md / MEMORY.md / TOOLS.md /
// HEARTBEAT.md / DREAMS.md / BOOTSTRAP.md / PERSONALITY.md / SKILLS.md / ROLE.md
// from the agent's workspace dir. Tabbed view; renders markdown.

type WorkspaceFile = {
  name: string;
  exists: boolean;
  size: number;
  mtime?: number;
  content: string | null;
  truncated?: boolean;
  error?: string;
};

type WorkspaceResp = {
  workspace_dir: string | null;
  files: WorkspaceFile[];
  other_md_files?: string[];
  missing_reason?: string;
};

function WorkspaceDefinitions({ agentId }: { agentId: string }) {
  const { getToken } = useAuth();
  const [data, setData] = useState<WorkspaceResp | null>(null);
  const [active, setActive] = useState<string>("SOUL.md");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [extraFiles, setExtraFiles] = useState<Record<string, string>>({});

  const authHeader = useCallback(async (): Promise<HeadersInit> => {
    if (isLocalAuthMode()) {
      const t = getLocalAuthToken();
      return t ? { Authorization: `Bearer ${t}` } : {};
    }
    const t = await getToken();
    return t ? { Authorization: `Bearer ${t}` } : {};
  }, [getToken]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/agents/${encodeURIComponent(agentId)}/workspace-files`,
          { headers: await authHeader() },
        );
        if (!r.ok) throw new Error(String(r.status));
        const d = (await r.json()) as WorkspaceResp;
        if (!cancelled) {
          setData(d);
          // Pick first existing file as active
          const first = d.files.find((f) => f.exists);
          if (first) setActive(first.name);
          else if (d.other_md_files && d.other_md_files.length > 0) {
            setActive(d.other_md_files[0]);
          }
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId, authHeader]);

  // Lazy-load non-whitelisted *.md when user picks them
  useEffect(() => {
    if (!data?.other_md_files?.includes(active)) return;
    if (extraFiles[active]) return;
    (async () => {
      try {
        const r = await fetch(
          `${getApiBaseUrl()}/api/v1/agents/${encodeURIComponent(agentId)}/workspace-files/${encodeURIComponent(active)}`,
          { headers: await authHeader() },
        );
        if (!r.ok) return;
        const d = await r.json();
        setExtraFiles((prev) => ({ ...prev, [active]: d.content || "" }));
      } catch {
        /* ignore */
      }
    })();
  }, [active, data, authHeader, agentId, extraFiles]);

  const fileMap = useMemo(() => {
    const m: Record<string, WorkspaceFile> = {};
    for (const f of data?.files || []) m[f.name] = f;
    return m;
  }, [data]);

  const activeFile = fileMap[active];
  const activeContent =
    activeFile?.content ??
    extraFiles[active] ??
    null;

  if (loading) {
    return (
      <section className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] p-5">
        <p className="text-xs text-muted">Loading workspace definitions…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-2xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
        Workspace fetch failed: {error}
      </section>
    );
  }

  if (!data || !data.workspace_dir) {
    return (
      <section className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface-muted)] p-4">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet mb-1">
          Workspace definitions
        </p>
        <p className="text-xs text-muted">
          No workspace dir found for this agent
          {data?.missing_reason ? ` (${data.missing_reason})` : ""}.
          Provision the agent or set <code>workspace</code> in openclaw.json.
        </p>
      </section>
    );
  }

  const knownTabs = (data.files || []).filter((f) => f.exists).map((f) => f.name);
  const extraTabs = data.other_md_files || [];
  const allTabs = [...knownTabs, ...extraTabs];

  return (
    <section className="rounded-2xl border border-[color:var(--border)] bg-[color:var(--surface)] overflow-hidden">
      <header className="px-4 py-2 border-b border-[color:var(--border)] flex items-center justify-between bg-[color:var(--surface-muted)]">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-quiet">
            Workspace definitions
          </p>
          <p className="text-[10px] text-muted font-mono">{data.workspace_dir}</p>
        </div>
        <p className="text-[10px] text-muted">
          {knownTabs.length} known · {extraTabs.length} other
        </p>
      </header>
      <div className="flex flex-wrap gap-1 px-3 py-2 border-b border-[color:var(--border)]">
        {allTabs.length === 0 && (
          <p className="text-xs text-muted">No markdown files found in workspace.</p>
        )}
        {allTabs.map((name) => (
          <button
            key={name}
            onClick={() => setActive(name)}
            className={
              "px-2 py-1 text-[11px] font-mono rounded " +
              (active === name
                ? "bg-slate-900 text-white"
                : "text-slate-700 hover:bg-slate-100 border border-slate-200")
            }
          >
            {name.replace(/\.md$/, "")}
          </button>
        ))}
      </div>
      <div className="p-4 max-h-[480px] overflow-y-auto">
        {activeContent === null ? (
          <p className="text-xs text-muted italic">No content (file missing or empty).</p>
        ) : (
          <div className="prose prose-sm max-w-none">
            <Markdown content={activeContent} variant="basic" />
          </div>
        )}
        {activeFile?.truncated && (
          <p className="text-[10px] text-amber-600 mt-2">⚠ file truncated at 64KB</p>
        )}
      </div>
    </section>
  );
}
