"use client";

import { useEffect, useState } from "react";

import { customFetch } from "@/api/mutator";
import { usageLabel, type TeamCycleView } from "@/lib/team-cycle";

type ExecutionConfig = {
  execution_mode?: string | null;
  execution_lane?: string | null;
  harness_provider?: string | null;
  harness_model?: string | null;
};

/** Recorded native workers are separate from gateway assignment/configuration. */
export function TeamCyclePanel({ boardId, taskId, task }: { boardId: string; taskId: string; task?: ExecutionConfig }) {
  const key = `${boardId}:${taskId}`;
  const [state, setState] = useState<{ key: string; cycles: TeamCycleView[] | null; error: string | null }>({ key, cycles: null, error: null });
  const current = state.key === key ? state : { cycles: null, error: null };
  const latest = current.cycles?.[0];

  useEffect(() => {
    let controller: AbortController | null = null;
    let closed = false;
    const refresh = async () => {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      try {
        const { data } = await customFetch<{ data: { cycles: TeamCycleView[] } }>(`/api/v1/boards/${boardId}/tasks/${taskId}/team`, { signal: request.signal });
        if (!closed && !request.signal.aborted) setState({ key, cycles: data.cycles ?? [], error: null });
      } catch (caught) {
        if (!closed && !request.signal.aborted) setState({ key, cycles: null, error: String(caught) });
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 30_000);
    return () => { closed = true; controller?.abort(); clearInterval(timer); };
  }, [boardId, taskId, key]);

  return (
    <section className="rounded-lg border border-slate-200 p-3" data-testid="team-cycle-panel" aria-label="Task execution">
      <h3 className="mb-2 text-xs font-semibold text-slate-700">Task execution</h3>
      {task && <p className="mb-2 text-xs text-slate-600">
        Configured: {task.execution_mode === "team" ? "native team" : "single"} · {task.execution_lane ?? "gateway/default"} · engine {task.harness_provider ? `${task.harness_provider}/${task.harness_model ?? "automatic model"}` : "automatic (not proof of dispatch)"}
      </p>}
      {current.error && <p role="alert" className="text-xs text-rose-700">Execution records unavailable: {current.error}</p>}
      {!current.error && !current.cycles && <p className="text-xs text-slate-500">Loading execution records…</p>}
      {!current.error && current.cycles && !latest && <p className="text-xs text-slate-500">
        No cycle yet. No native worker/model execution is recorded here. {task?.execution_mode === "team" ? "The host runner has not started this task." : "Single/gateway or external host runs are separate; see their activity evidence. An empty gateway assignee does not prove a harness is idle or running."}
      </p>}
      {latest && <>
        <p className="mb-1 text-xs text-slate-600">
          {latest.cycle.state} · round {latest.cycle.round}/{latest.cycle.max_rounds}
          {latest.cycle.state === "delegated" ? " · children delegated, not complete" : ""}
          {latest.cycle.block_reason ? ` · ${latest.cycle.block_reason}` : ""}
        </p>
        <p className="mb-2 text-xs text-slate-500">Host coordinator (no model). Recorded attempts below; queued engines are selections, not completed inference. Publication/Review are not accepted Done.</p>
        <ul className="space-y-1">
          {latest.attempts.map((a) => <li key={a.id} className="rounded bg-slate-50 px-2 py-1 text-xs text-slate-600">
            <span className="font-semibold">{a.member_name ?? a.role_kind}</span> ({a.role_kind}, {a.provider}/{a.model}) · harness {a.provider === "claude-code" ? "official Claude CLI" : "Pi CLI"} · {a.stage_key} r{a.round}#{a.attempt_no}: {a.status}
            {a.verdict ? ` · verdict ${a.verdict}` : ""}{a.error ? ` · ${a.error}` : ""}
            <span className="block text-slate-500">{usageLabel(a)}</span>
          </li>)}
        </ul>
      </>}
    </section>
  );
}
