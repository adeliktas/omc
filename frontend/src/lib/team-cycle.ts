import { parseApiDatetime } from "@/lib/datetime";

export type TaskWork = {
  split_count?: number;
  work_state?: "running" | "reviewing" | "waiting" | null;
  work_detail?: string | null;
  work_started_at?: string | null;
  work_expires_at?: string | null;
};

/** Board activity is not workflow authority: idle parents/queues cannot become workers. */
export function taskDisplayStatus<T extends TaskWork & { status: string }>(
  task: T,
): T["status"] | "waiting" {
  if (task.status !== "in_progress" && task.status !== "review")
    return task.status;
  const expiry = parseApiDatetime(task.work_expires_at);
  if (
    (task.work_state === "running" || task.work_state === "reviewing") &&
    expiry &&
    expiry.getTime() > Date.now()
  )
    return "in_progress";
  if (task.status === "in_progress" && (task.work_state || task.split_count))
    return "waiting";
  return task.status;
}

/** Gateway assignees are not native team members or host harness processes. */
export function taskExecutionLabel(
  task: TaskWork & {
    assignee?: string | null;
    execution_mode?: string | null;
    execution_lane?: string | null;
  },
): string {
  const expiry = parseApiDatetime(task.work_expires_at);
  if (expiry && expiry.getTime() <= Date.now()) return task.work_state === "reviewing" ? "Queued for review" : "Worker lease expired";
  if (task.work_detail) return task.work_detail;
  if (task.execution_mode === "team") return "Native team · see execution";
  if (task.execution_lane === "harness") return "Host harness · see execution";
  return task.assignee ?? "Unassigned";
}

/** Read model of GET /boards/{board}/tasks/{task}/team (native team cycles). */
export interface TeamAttempt {
  id: string;
  member_name: string | null;
  role_kind: string;
  stage_key: string;
  round: number;
  attempt_no: number;
  status: string;
  provider: string;
  model: string;
  verdict: string | null;
  error: string | null;
  usage_reported: boolean;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cli_cost_estimate_usd: number | null;
}

export interface TeamCycleView {
  cycle: {
    id: string;
    state: string;
    round: number;
    max_rounds: number;
    block_reason: string | null;
  };
  attempts: TeamAttempt[];
}

/**
 * Usage is observed, never assumed: an attempt that reported nothing says so
 * instead of showing zeros, cache reads are separate, and the cost is the
 * CLI's own estimate rather than a billed amount.
 */
export function usageLabel(a: TeamAttempt): string {
  if (!a.usage_reported) return "usage not reported";
  const parts = [
    `in ${a.input_tokens ?? "?"}`,
    `out ${a.output_tokens ?? "?"}`,
    `cache read ${a.cache_read_tokens ?? "?"}`,
  ];
  if (a.cli_cost_estimate_usd !== null) {
    parts.push(
      `CLI estimate $${a.cli_cost_estimate_usd.toFixed(4)} (not billed)`,
    );
  }
  return parts.join(" · ");
}
