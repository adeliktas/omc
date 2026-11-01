"use client";

import {
  memo,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { customFetch } from "@/api/mutator";
import { TaskCard } from "@/components/molecules/TaskCard";
import { TaskSettingsDialog } from "@/components/organisms/TaskSettingsDialog";
import {
  taskDisplayStatus,
  taskExecutionLabel,
  type TaskWork,
} from "@/lib/team-cycle";
import { parseApiDatetime } from "@/lib/datetime";
import { cn } from "@/lib/utils";

/** Drag payload type for an agent chip (board sidebar → task card = assign). */
export const AGENT_DRAG_TYPE = "application/x-mc-agent";

// Done alone holds 1000+ cards; rendering and FLIP-measuring all of them on
// every poll is what made the board slow. Columns open on demand.
const COLUMN_RENDER_CAP = 40;

// T103 v15 (2026-05-19): extended with 'blocked' + 'cancelled' to match
// backend TaskStatus Literal. Without these, the `?? buckets.inbox`
// fallback in `grouped` rendered blocked/cancelled tasks as fake inbox
// — owner thought tasks were stuck.
type TaskStatus =
  | "inbox"
  | "in_progress"
  | "review"
  | "done"
  | "blocked"
  | "cancelled"
  | "paused";

type ColumnStatus = TaskStatus | "waiting";

type Task = TaskWork & {
  id: string;
  title: string;
  status: TaskStatus;
  priority: string;
  paused_from_status?: TaskStatus | null;
  description?: string | null;
  due_at?: string | null;
  assigned_agent_id?: string | null;
  assignee?: string | null;
  approvals_pending_count?: number;
  tags?: Array<{ id: string; name: string; slug: string; color: string }>;
  depends_on_task_ids?: string[];
  blocked_by_task_ids?: string[];
  is_blocked?: boolean;
  auto_created?: boolean;
  auto_reason?: string | null;
  created_by_type?: string | null;
  created_by_name?: string | null;
  // Timing / repeat fields for TaskCard rendering
  created_at?: string | null;
  updated_at?: string | null;
  complexity?: number | null;
  repeat_enabled?: boolean;
  repeat_interval_minutes?: number | null;
  repeat_cron?: string | null;
  stage_timings?: Array<{
    id: string;
    status: string;
    agent_id?: string | null;
    agent_name?: string | null;
    entered_at: string;
    exited_at?: string | null;
    duration_seconds?: number | null;
  }>;
  // T62 (2026-05-15): expose budgets + DAG + parent linkage so the
  // TaskCard can render everything the watchdog reasons about.
  execution_lane?: string | null;
  execution_mode?: string | null;
  workspace_path?: string | null;
  harness_provider?: string | null;
  harness_model?: string | null;
  thinking_level?: string | null;
  max_seconds_budget?: number | null;
  max_tokens_budget?: number | null;
  tokens_used?: number;
  budget_exhausted_at?: string | null;
  parent_task_id?: string | null;
  pm_lock_agent_id?: string | null;
  pm_lock_at?: string | null;
  split_type?: string | null;
  split_count?: number;
  split_position?: number | null;
  dag_done_count?: number | null;
};

type TaskBoardProps = {
  tasks: Task[];
  onTaskSelect?: (task: Task) => void;
  onTaskMove?: (taskId: string, status: TaskStatus) => void | Promise<void>;
  readOnly?: boolean;
  /** Board id, required to address the task API from the settings dialog.
   *  Without it the gear is hidden rather than rendering a dead control. */
  boardId?: string;
  /** Called after settings are saved so the board can refetch. */
  onTaskUpdated?: () => void;
};

type ReviewBucket = "all" | "approval_needed" | "waiting_lead" | "blocked";

const columns: Array<{
  title: string;
  status: ColumnStatus;
  dot: string;
  accent: string;
  text: string;
  badge: string;
}> = [
  {
    title: "Inbox",
    status: "inbox",
    dot: "bg-slate-400",
    accent: "hover:border-slate-400 hover:bg-slate-50",
    text: "group-hover:text-slate-700 text-slate-500",
    badge: "bg-slate-100 text-slate-600",
  },
  {
    title: "Waiting",
    status: "waiting",
    dot: "bg-slate-400",
    accent: "border-slate-200",
    text: "text-slate-500",
    badge: "bg-slate-100 text-slate-600",
  },
  {
    title: "In Progress",
    status: "in_progress",
    dot: "bg-purple-500",
    accent: "hover:border-purple-400 hover:bg-purple-50",
    text: "group-hover:text-purple-600 text-slate-500",
    badge: "bg-purple-100 text-purple-700",
  },
  {
    title: "Review",
    status: "review",
    dot: "bg-indigo-500",
    accent: "hover:border-indigo-400 hover:bg-indigo-50",
    text: "group-hover:text-indigo-600 text-slate-500",
    badge: "bg-indigo-100 text-indigo-700",
  },
  {
    title: "Done",
    status: "done",
    dot: "bg-green-500",
    accent: "hover:border-green-400 hover:bg-green-50",
    text: "group-hover:text-green-600 text-slate-500",
    badge: "bg-emerald-100 text-emerald-700",
  },
  {
    title: "Blocked",
    status: "blocked",
    dot: "bg-rose-500",
    accent: "hover:border-rose-400 hover:bg-rose-50",
    text: "group-hover:text-rose-700 text-slate-500",
    badge: "bg-rose-100 text-rose-700",
  },
  {
    title: "Paused",
    status: "paused",
    dot: "bg-amber-500",
    accent: "hover:border-amber-400 hover:bg-amber-50",
    text: "group-hover:text-amber-700 text-slate-500",
    badge: "bg-amber-100 text-amber-700",
  },
  {
    title: "Cancelled",
    status: "cancelled",
    dot: "bg-slate-400",
    accent: "hover:border-slate-400 hover:bg-slate-50",
    text: "group-hover:text-slate-700 text-slate-500",
    badge: "bg-slate-200 text-slate-600",
  },
];

/**
 * Build compact due-date UI state for a task card.
 *
 * - Returns `due: undefined` when the task has no due date (or it's invalid), so
 *   callers can omit the due-date UI entirely.
 * - Treats a task as overdue only if it is not `done` (so "Done" tasks don't
 *   keep showing as overdue forever).
 */
const resolveDueState = (
  task: Task,
): { due: string | undefined; isOverdue: boolean } => {
  const date = parseApiDatetime(task.due_at);
  if (!date) return { due: undefined, isOverdue: false };

  const dueLabel = date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });

  const isOverdue = task.status !== "done" && date.getTime() < Date.now();
  return {
    due: isOverdue ? `Overdue · ${dueLabel}` : dueLabel,
    isOverdue,
  };
};

type CardPosition = { left: number; top: number };

const KANBAN_MOVE_ANIMATION_MS = 240;
const KANBAN_MOVE_EASING = "cubic-bezier(0.2, 0.8, 0.2, 1)";

/**
 * Kanban-style task board with 4 columns.
 *
 * Notes:
 * - Uses a lightweight FLIP animation (via `useLayoutEffect`) to animate cards
 *   to their new positions when tasks move between columns.
 * - Drag interactions can temporarily fight browser-managed drag images; the
 *   animation is disabled while a card is being dragged.
 * - Respects `prefers-reduced-motion`.
 */
export const TaskBoard = memo(function TaskBoard({
  tasks,
  onTaskSelect,
  onTaskMove,
  readOnly = false,
  boardId,
  onTaskUpdated,
}: TaskBoardProps) {
  const [settingsTask, setSettingsTask] = useState<Task | null>(null);
  const boardRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const prevPositionsRef = useRef<Map<string, CardPosition>>(new Map());
  const animationRafRef = useRef<number | null>(null);
  const cleanupTimeoutRef = useRef<number | null>(null);
  const animatedTaskIdsRef = useRef<Set<string>>(new Set());

  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [activeColumn, setActiveColumn] = useState<ColumnStatus | null>(null);
  const [reviewBucket, setReviewBucket] = useState<ReviewBucket>("all");
  const [expanded, setExpanded] = useState<Set<ColumnStatus>>(new Set());
  const [dropCardId, setDropCardId] = useState<string | null>(null);
  const [dropError, setDropError] = useState<string | null>(null);
  const [pausingId, setPausingId] = useState<string | null>(null);

  const togglePause = async (task: Task) => {
    if (readOnly || !boardId || pausingId) return;
    setPausingId(task.id);
    setDropError(null);
    try {
      await customFetch(`/api/v1/boards/${boardId}/tasks/${task.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          status:
            task.status === "paused"
              ? (task.paused_from_status ?? "inbox")
              : "paused",
          expected_status: task.status,
          expected_updated_at: task.updated_at,
        }),
      });
      onTaskUpdated?.();
    } catch (caught) {
      setDropError(`${task.title}: ${(caught as Error).message ?? caught}`);
    } finally {
      setPausingId(null);
    }
  };

  // A card accepts an agent (assign) or files (attach); task-move drags fall
  // through to the column.
  const cardDropKind = (event: React.DragEvent) => {
    const types = Array.from(event.dataTransfer.types);
    if (types.includes(AGENT_DRAG_TYPE)) return "agent";
    if (types.includes("Files")) return "files";
    return null;
  };

  const handleCardDrop = (task: Task) => async (event: React.DragEvent) => {
    const kind = cardDropKind(event);
    if (!kind || readOnly || !boardId) return;
    event.preventDefault();
    event.stopPropagation();
    setDropCardId(null);
    setDropError(null);
    const base = `/api/v1/boards/${boardId}/tasks/${task.id}`;
    try {
      if (kind === "agent") {
        const agentId = event.dataTransfer.getData(AGENT_DRAG_TYPE);
        if (!agentId || agentId === task.assigned_agent_id) return;
        await customFetch(base, {
          method: "PATCH",
          body: JSON.stringify({ assigned_agent_id: agentId }),
        });
      } else {
        for (const file of Array.from(event.dataTransfer.files)) {
          const body = new FormData();
          body.append("file", file);
          await customFetch(`${base}/attachments`, { method: "POST", body });
        }
      }
      onTaskUpdated?.();
    } catch (caught) {
      setDropError(`${task.title}: ${(caught as Error).message ?? caught}`);
    }
  };

  const setCardRef = useCallback(
    (taskId: string) => (node: HTMLDivElement | null) => {
      if (node) {
        cardRefs.current.set(taskId, node);
        return;
      }
      cardRefs.current.delete(taskId);
    },
    [],
  );

  /**
   * Snapshot each card's position relative to the scroll container.
   *
   * We store these measurements so we can compute deltas (prev - next) and
   * apply the FLIP technique on the next render.
   */
  const measurePositions = useCallback((): Map<string, CardPosition> => {
    const positions = new Map<string, CardPosition>();
    const container = boardRef.current;
    const containerRect = container?.getBoundingClientRect();
    const scrollLeft = container?.scrollLeft ?? 0;
    const scrollTop = container?.scrollTop ?? 0;

    for (const [taskId, element] of cardRefs.current.entries()) {
      const rect = element.getBoundingClientRect();
      positions.set(taskId, {
        left:
          containerRect && container
            ? rect.left - containerRect.left + scrollLeft
            : rect.left,
        top:
          containerRect && container
            ? rect.top - containerRect.top + scrollTop
            : rect.top,
      });
    }

    return positions;
  }, []);

  // Animate card reordering smoothly by applying FLIP whenever layout positions change.
  useLayoutEffect(() => {
    const cardRefsSnapshot = cardRefs.current;
    if (animationRafRef.current !== null) {
      window.cancelAnimationFrame(animationRafRef.current);
      animationRafRef.current = null;
    }
    if (cleanupTimeoutRef.current !== null) {
      window.clearTimeout(cleanupTimeoutRef.current);
      cleanupTimeoutRef.current = null;
    }
    for (const taskId of animatedTaskIdsRef.current) {
      const element = cardRefsSnapshot.get(taskId);
      if (!element) continue;
      element.style.transform = "";
      element.style.transition = "";
      element.style.willChange = "";
      element.style.position = "";
      element.style.zIndex = "";
    }
    animatedTaskIdsRef.current.clear();

    const prevPositions = prevPositionsRef.current;
    const nextPositions = measurePositions();
    prevPositionsRef.current = nextPositions;

    // Avoid fighting the browser while it manages the drag image.
    if (draggingId) return;

    const prefersReducedMotion =
      window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
    if (prefersReducedMotion) return;

    const moved: Array<{
      taskId: string;
      element: HTMLDivElement;
      dx: number;
      dy: number;
    }> = [];
    for (const [taskId, next] of nextPositions.entries()) {
      const prev = prevPositions.get(taskId);
      if (!prev) continue;
      const dx = prev.left - next.left;
      const dy = prev.top - next.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
      const element = cardRefsSnapshot.get(taskId);
      if (!element) continue;
      moved.push({ taskId, element, dx, dy });
    }

    if (!moved.length) return;
    animatedTaskIdsRef.current = new Set(moved.map(({ taskId }) => taskId));

    // FLIP: invert to the previous position before paint, then animate back to 0.
    for (const { element, dx, dy } of moved) {
      element.style.transform = `translate(${dx}px, ${dy}px)`;
      element.style.transition = "transform 0s";
      element.style.willChange = "transform";
      element.style.position = "relative";
      element.style.zIndex = "1";
    }

    animationRafRef.current = window.requestAnimationFrame(() => {
      for (const { element } of moved) {
        element.style.transition = `transform ${KANBAN_MOVE_ANIMATION_MS}ms ${KANBAN_MOVE_EASING}`;
        element.style.transform = "";
      }

      cleanupTimeoutRef.current = window.setTimeout(() => {
        for (const { element } of moved) {
          element.style.transition = "";
          element.style.willChange = "";
          element.style.position = "";
          element.style.zIndex = "";
        }
        animatedTaskIdsRef.current.clear();
        cleanupTimeoutRef.current = null;
      }, KANBAN_MOVE_ANIMATION_MS + 60);

      animationRafRef.current = null;
    });

    return () => {
      if (animationRafRef.current !== null) {
        window.cancelAnimationFrame(animationRafRef.current);
        animationRafRef.current = null;
      }
      if (cleanupTimeoutRef.current !== null) {
        window.clearTimeout(cleanupTimeoutRef.current);
        cleanupTimeoutRef.current = null;
      }
      for (const taskId of animatedTaskIdsRef.current) {
        const element = cardRefsSnapshot.get(taskId);
        if (!element) continue;
        element.style.transform = "";
        element.style.transition = "";
        element.style.willChange = "";
        element.style.position = "";
        element.style.zIndex = "";
      }
      animatedTaskIdsRef.current.clear();
    };
  }, [draggingId, measurePositions, tasks]);

  const grouped = useMemo(() => {
    const buckets: Record<ColumnStatus, Task[]> = {
      waiting: [],
      inbox: [],
      in_progress: [],
      review: [],
      done: [],
      blocked: [],
      cancelled: [],
      paused: [],
    };
    for (const column of columns) {
      buckets[column.status] = [];
    }
    tasks.forEach((task) => {
      const bucket = buckets[taskDisplayStatus(task)] ?? buckets.inbox;
      bucket.push(task);
    });
    return buckets;
  }, [tasks]);

  // Keep drag/drop state and payload handling centralized for column move interactions.
  const handleDragStart =
    (task: Task) => (event: React.DragEvent<HTMLDivElement>) => {
      if (readOnly) {
        event.preventDefault();
        return;
      }
      if (task.is_blocked) {
        event.preventDefault();
        return;
      }
      setDraggingId(task.id);
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData(
        "text/plain",
        JSON.stringify({ taskId: task.id, status: task.status }),
      );
    };

  const handleDragEnd = () => {
    setDraggingId(null);
    setActiveColumn(null);
  };

  const handleDrop =
    (status: ColumnStatus) => (event: React.DragEvent<HTMLDivElement>) => {
      if (readOnly || status === "waiting") return;
      event.preventDefault();
      setActiveColumn(null);
      const raw = event.dataTransfer.getData("text/plain");
      if (!raw) return;
      try {
        const payload = JSON.parse(raw) as { taskId?: string; status?: string };
        if (!payload.taskId || !payload.status) return;
        if (payload.status === status) return;
        onTaskMove?.(payload.taskId, status);
      } catch {
        // Ignore malformed payloads.
      }
    };

  const handleDragOver =
    (status: ColumnStatus) => (event: React.DragEvent<HTMLDivElement>) => {
      if (readOnly || status === "waiting") return;
      event.preventDefault();
      if (activeColumn !== status) {
        setActiveColumn(status);
      }
    };

  const handleDragLeave = (status: ColumnStatus) => () => {
    if (readOnly) return;
    if (activeColumn === status) {
      setActiveColumn(null);
    }
  };

  return (
    <div
      ref={boardRef}
      data-testid="task-board"
      className={cn(
        // Mobile-first: stack columns vertically to avoid horizontal scrolling.
        "grid grid-cols-1 gap-4 overflow-x-hidden pb-6",
        // Desktop/tablet: switch back to horizontally scrollable kanban columns.
        "sm:grid-flow-col sm:auto-cols-[minmax(200px,1fr)] sm:grid-cols-none sm:overflow-x-auto",
      )}
    >
      {columns.map((column) => {
        const columnTasks = grouped[column.status] ?? [];
        // Derive review tab counts and the active subset from one canonical task list.
        const reviewCounts =
          column.status === "review"
            ? columnTasks.reduce(
                (acc, task) => {
                  if (task.is_blocked) {
                    acc.blocked += 1;
                    return acc;
                  }
                  if ((task.approvals_pending_count ?? 0) > 0) {
                    acc.approval_needed += 1;
                    return acc;
                  }
                  acc.waiting_lead += 1;
                  return acc;
                },
                {
                  all: columnTasks.length,
                  approval_needed: 0,
                  waiting_lead: 0,
                  blocked: 0,
                },
              )
            : null;

        const filteredTasks =
          column.status === "review" && reviewBucket !== "all"
            ? columnTasks.filter((task) => {
                if (reviewBucket === "blocked") return Boolean(task.is_blocked);
                if (reviewBucket === "approval_needed")
                  return (
                    (task.approvals_pending_count ?? 0) > 0 && !task.is_blocked
                  );
                if (reviewBucket === "waiting_lead")
                  return (
                    !task.is_blocked &&
                    (task.approvals_pending_count ?? 0) === 0
                  );
                return true;
              })
            : columnTasks;

        return (
          <div
            key={column.title}
            className={cn(
              // On mobile, columns are stacked, so avoid forcing tall fixed heights.
              "kanban-column min-h-0",
              // On larger screens, keep columns tall to reduce empty space during drag.
              "sm:min-h-[calc(100vh-260px)]",
              activeColumn === column.status &&
                !readOnly &&
                "ring-2 ring-slate-200",
            )}
            onDrop={readOnly ? undefined : handleDrop(column.status)}
            onDragOver={readOnly ? undefined : handleDragOver(column.status)}
            onDragLeave={readOnly ? undefined : handleDragLeave(column.status)}
          >
            <div className="column-header z-10 rounded-t-xl border border-b-0 border-slate-200 bg-white px-4 py-3 sm:sticky sm:top-0 sm:bg-white/80 sm:backdrop-blur">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={cn("h-2 w-2 rounded-full", column.dot)} />
                  <h3 className="text-sm font-semibold text-slate-900">
                    {column.title}
                  </h3>
                </div>
                <span
                  className={cn(
                    "flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold",
                    column.badge,
                  )}
                >
                  {filteredTasks.length}
                </span>
              </div>
              {column.status === "review" && reviewCounts ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                  {(
                    [
                      { key: "all", label: "All", count: reviewCounts.all },
                      {
                        key: "approval_needed",
                        label: "Approval needed",
                        count: reviewCounts.approval_needed,
                      },
                      {
                        key: "waiting_lead",
                        label: "Queued review",
                        count: reviewCounts.waiting_lead,
                      },
                      {
                        key: "blocked",
                        label: "Blocked",
                        count: reviewCounts.blocked,
                      },
                    ] as const
                  ).map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => setReviewBucket(option.key)}
                      className={cn(
                        "rounded-full border px-2.5 py-1 transition",
                        reviewBucket === option.key
                          ? "border-slate-900 bg-slate-900 text-white"
                          : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50",
                      )}
                      aria-pressed={reviewBucket === option.key}
                    >
                      {option.label} · {option.count}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="rounded-b-xl border border-t-0 border-slate-200 bg-white p-3">
              <div className="space-y-3">
                {(expanded.has(column.status)
                  ? filteredTasks
                  : filteredTasks.slice(0, COLUMN_RENDER_CAP)
                ).map((task) => {
                  const dueState = resolveDueState(task);
                  return (
                    <div
                      key={task.id}
                      ref={setCardRef(task.id)}
                      className={cn(
                        "rounded-lg transition",
                        dropCardId === task.id && "ring-2 ring-blue-400 ring-offset-2",
                      )}
                      onDragOver={(event) => {
                        if (readOnly || !boardId || !cardDropKind(event)) return;
                        event.preventDefault();
                        event.stopPropagation();
                        if (dropCardId !== task.id) setDropCardId(task.id);
                      }}
                      onDragLeave={() => dropCardId === task.id && setDropCardId(null)}
                      onDrop={(event) => void handleCardDrop(task)(event)}
                    >
                      <TaskCard
                        title={task.title}
                        status={taskDisplayStatus(task)}
                        priority={task.priority}
                        assignee={task.auto_reason === "tests-autonomy-remediation" && !task.assigned_agent_id
                          ? "Awaiting owner: tool-capable executor"
                          : taskExecutionLabel(task)}
                        due={dueState.due}
                        isOverdue={dueState.isOverdue}
                        approvalsPendingCount={task.approvals_pending_count}
                        tags={task.tags}
                        isBlocked={task.is_blocked}
                        blockedByCount={task.blocked_by_task_ids?.length ?? 0}
                        onClick={() => onTaskSelect?.(task)}
                        onOpenSettings={
                          boardId && !readOnly ? () => setSettingsTask(task) : undefined
                        }
                        autoCreated={task.auto_created}
                        createdByName={task.created_by_name}
                        createdAt={
                          task.work_started_at ??
                          (task.repeat_enabled
                            ? (task.updated_at ?? task.created_at)
                            : task.created_at)
                        }
                        taskId={task.id}
                        repeatIntervalMinutes={task.repeat_interval_minutes ?? undefined}
                        stageDurations={(() => {
                          // T68 (2026-05-15): show CURRENT/LAST cycle only,
                          // not lifetime. For repeating tasks, "lifetime"
                          // includes every prior cycle summed; the card
                          // would label "121m 7s last cycle" meaning the
                          // whole task age. We slice from the most recent
                          // `inbox` entry (= start of latest cycle) and
                          // sum from there. For one-shot tasks the slice
                          // equals lifetime (no prior `inbox` events).
                          const sts = task.stage_timings ?? [];
                          if (sts.length === 0) return [];
                          // Sort ascending by entered_at to find latest inbox.
                          const sorted = [...sts].sort((a, b) =>
                            a.entered_at.localeCompare(b.entered_at),
                          );
                          let cycleStartIdx = 0;
                          for (let i = sorted.length - 1; i >= 0; i--) {
                            if (sorted[i].status === "inbox") {
                              cycleStartIdx = i;
                              break;
                            }
                          }
                          const cycle = sorted.slice(cycleStartIdx);
                          const byStatus: Record<string, number> = {};
                          for (const st of cycle) {
                            let dur = st.duration_seconds;
                            if (dur == null && !st.exited_at) {
                              // Open row — count elapsed up to now.
                              dur = Math.max(
                                0,
                                Math.floor(
                                  (Date.now() - new Date(st.entered_at).getTime()) / 1000,
                                ),
                              );
                            }
                            if (dur != null) {
                              byStatus[st.status] = (byStatus[st.status] ?? 0) + dur;
                            }
                          }
                          return Object.entries(byStatus).map(
                            ([status, sec]) => ({ status, totalSeconds: sec }),
                          );
                        })()}
                        stageDurationsLifetime={(() => {
                          // Full lifetime aggregation for the expand toggle.
                          const byStatus: Record<string, number> = {};
                          for (const st of task.stage_timings ?? []) {
                            if (st.duration_seconds != null) {
                              byStatus[st.status] = (byStatus[st.status] ?? 0) + st.duration_seconds;
                            }
                          }
                          return Object.entries(byStatus).map(
                            ([status, sec]) => ({ status, totalSeconds: sec }),
                          );
                        })()}
                        draggable={
                          !readOnly &&
                          !task.is_blocked &&
                          task.status !== "paused" &&
                          task.work_state !== "reviewing"
                        }
                        isDragging={draggingId === task.id}
                        onDragStart={
                          readOnly ? undefined : handleDragStart(task)
                        }
                        onDragEnd={readOnly ? undefined : handleDragEnd}
                        splitType={task.split_type}
                        splitCount={task.split_count}
                        dagDoneCount={task.dag_done_count ?? undefined}
                        complexity={task.complexity ?? undefined}
                        maxSecondsBudget={task.max_seconds_budget ?? undefined}
                        maxTokensBudget={task.max_tokens_budget ?? undefined}
                        tokensUsed={task.tokens_used ?? undefined}
                        budgetExhaustedAt={task.budget_exhausted_at ?? undefined}
                        parentTaskId={task.parent_task_id ?? undefined}
                        splitPosition={task.split_position ?? undefined}
                        pmLockAgentId={task.pm_lock_agent_id ?? undefined}
                      />
                      {boardId && !readOnly && task.status !== "cancelled" && (task.status !== "done" || task.repeat_enabled) && (
                        <button
                          type="button"
                          className="mt-1 rounded border border-slate-200 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                          aria-label={`${task.status === "paused" ? "Resume" : "Pause"} ${task.title}`}
                          disabled={pausingId !== null}
                          onClick={(event) => { event.stopPropagation(); void togglePause(task); }}
                          title={task.status === "paused" && task.paused_from_status === "blocked" ? "Return to Blocked for safe recovery; does not replay unfinished work" : "Keeps task history, budgets and recurrence settings"}
                        >
                          {pausingId === task.id ? "Updating…" : task.status === "paused" ? "Resume" : "Pause"}
                        </button>
                      )}
                    </div>
                  );
                })}
                {filteredTasks.length > COLUMN_RENDER_CAP && (
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(column.status)) next.delete(column.status);
                        else next.add(column.status);
                        return next;
                      })
                    }
                    className="w-full rounded-lg border border-dashed border-slate-200 py-2 text-xs font-semibold text-slate-500 hover:border-slate-300 hover:bg-slate-50"
                  >
                    {expanded.has(column.status)
                      ? `Show latest ${COLUMN_RENDER_CAP}`
                      : `Show all ${filteredTasks.length}`}
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}
      {dropError && (
        <div
          role="alert"
          className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700 shadow"
          onClick={() => setDropError(null)}
        >
          {dropError}
        </div>
      )}
      {settingsTask && boardId && (
        <TaskSettingsDialog
          open
          onClose={() => setSettingsTask(null)}
          boardId={boardId}
          taskId={settingsTask.id}
          taskTitle={settingsTask.title}
          initial={{
            execution_lane: settingsTask.execution_lane ?? null,
            execution_mode: settingsTask.execution_mode ?? "single",
            workspace_path: settingsTask.workspace_path ?? null,
            harness_provider: settingsTask.harness_provider ?? null,
            harness_model: settingsTask.harness_model ?? null,
            thinking_level: settingsTask.thinking_level ?? null,
            complexity: settingsTask.complexity ?? null,
            max_seconds_budget: settingsTask.max_seconds_budget ?? null,
            max_tokens_budget: settingsTask.max_tokens_budget ?? null,
            repeat_enabled: settingsTask.repeat_enabled ?? false,
            repeat_interval_minutes: settingsTask.repeat_interval_minutes ?? null,
          }}
          onSaved={onTaskUpdated}
        />
      )}
    </div>
  );
});

TaskBoard.displayName = "TaskBoard";
