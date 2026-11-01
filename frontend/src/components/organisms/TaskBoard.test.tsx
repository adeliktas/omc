import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { customFetch } from "@/api/mutator";
import { TaskBoard } from "./TaskBoard";

vi.mock("@/api/mutator", () => ({ customFetch: vi.fn() }));

type Task = ComponentProps<typeof TaskBoard>["tasks"][number];

const buildTask = (overrides: Partial<Task> = {}): Task => ({
  id: `task-${Math.random().toString(16).slice(2)}`,
  title: "Task",
  status: "inbox",
  priority: "medium",
  approvals_pending_count: 0,
  blocked_by_task_ids: [],
  is_blocked: false,
  ...overrides,
});

describe("TaskBoard", () => {
  it("renders delegated waiting and real active reviews without a task status PATCH", () => {
    const move = vi.fn();
    vi.mocked(customFetch).mockClear();
    const parent = buildTask({
      title: "Delegated parent",
      status: "in_progress",
      split_count: 20,
      split_type: "dag",
      dag_done_count: 3,
      work_state: "waiting",
      work_detail: "Waiting for children",
    });
    const review = buildTask({
      title: "Active acceptance review",
      status: "review",
      work_state: "reviewing",
      work_detail: "Reviewing · qualified reviewer",
      work_expires_at: new Date(Date.now() + 60_000).toISOString(),
    });
    render(<TaskBoard tasks={[parent, review]} onTaskMove={move} />);
    const waiting = screen
      .getByRole("heading", { name: "Waiting" })
      .closest(".kanban-column") as HTMLElement;
    const active = screen
      .getByRole("heading", { name: "In Progress" })
      .closest(".kanban-column") as HTMLElement;
    expect(within(waiting).getByText("Delegated parent")).toBeInTheDocument();
    expect(within(waiting).getByText(/3\/20/)).toBeInTheDocument();
    expect(
      within(active).getByText("Active acceptance review"),
    ).toBeInTheDocument();
    expect(
      within(active).getByText("Reviewing · qualified reviewer"),
    ).toBeInTheDocument();
    fireEvent.drop(waiting, {
      dataTransfer: {
        getData: () => JSON.stringify({ taskId: review.id, status: "review" }),
      },
    });
    expect(move).not.toHaveBeenCalled();
    expect(customFetch).not.toHaveBeenCalled();
  });

  it("pauses and resumes with stale-card fences and keeps failed updates visible", async () => {
    const saved = vi.fn();
    vi.mocked(customFetch).mockResolvedValue({ status: 200 });
    const task = buildTask({
      id: "poem",
      title: "Poem scraper",
      updated_at: "2026-10-08T19:00:00",
    });
    const { rerender } = render(
      <TaskBoard tasks={[task]} boardId="board" onTaskUpdated={saved} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pause Poem scraper" }));
    await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
    expect(customFetch).toHaveBeenLastCalledWith(
      "/api/v1/boards/board/tasks/poem",
      {
        method: "PATCH",
        body: JSON.stringify({
          status: "paused",
          expected_status: "inbox",
          expected_updated_at: task.updated_at,
        }),
      },
    );
    rerender(
      <TaskBoard
        tasks={[{ ...task, status: "paused", paused_from_status: "inbox" }]}
        boardId="board"
        onTaskUpdated={saved}
      />,
    );
    const paused = screen
      .getByRole("heading", { name: "Paused" })
      .closest(".kanban-column")!;
    expect(
      within(paused as HTMLElement).getByText("Poem scraper"),
    ).toBeInTheDocument();
    vi.mocked(customFetch).mockRejectedValueOnce(
      new Error("Task changed since it was read"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Resume Poem scraper" }),
    );
    await screen.findByText(/Task changed since it was read/);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Resume Poem scraper" }),
    ).toBeEnabled();
    vi.mocked(customFetch).mockResolvedValue({ status: 200 });
    fireEvent.click(
      screen.getByRole("button", { name: "Resume Poem scraper" }),
    );
    await waitFor(() => expect(saved).toHaveBeenCalledTimes(2));
    expect(
      JSON.parse(vi.mocked(customFetch).mock.calls.at(-1)![1].body as string),
    ).toMatchObject({ status: "inbox", expected_status: "paused" });
    rerender(<TaskBoard tasks={[task]} boardId="board" readOnly />);
    expect(
      screen.queryByRole("button", { name: "Pause Poem scraper" }),
    ).not.toBeInTheDocument();
  });
  it("uses a mobile-first stacked layout (no horizontal scroll) with responsive kanban columns on larger screens", () => {
    render(
      <TaskBoard
        tasks={[
          {
            id: "t1",
            title: "Inbox item",
            status: "inbox",
            priority: "medium",
          },
        ]}
      />,
    );

    const board = screen.getByTestId("task-board");

    expect(board.className).toContain("overflow-x-hidden");
    expect(board.className).toContain("sm:overflow-x-auto");
    expect(board.className).toContain("grid-cols-1");
    expect(board.className).toContain("sm:grid-flow-col");
  });

  it("only sticks column headers on larger screens (avoids weird stacked sticky headers on mobile)", () => {
    render(
      <TaskBoard
        tasks={[
          {
            id: "t1",
            title: "Inbox item",
            status: "inbox",
            priority: "medium",
          },
        ]}
      />,
    );

    const header = screen
      .getByRole("heading", { name: "Inbox" })
      .closest(".column-header");
    expect(header?.className).toContain("sm:sticky");
    expect(header?.className).toContain("sm:top-0");
    // Ensure we didn't accidentally keep unscoped sticky behavior.
    expect(header?.className).not.toContain("sticky top-0");
  });

  it("renders the 4 columns and shows per-column counts", () => {
    const tasks: Task[] = [
      buildTask({ id: "t1", title: "Inbox A", status: "inbox" }),
      buildTask({ id: "t2", title: "Doing A", status: "in_progress" }),
      buildTask({ id: "t3", title: "Review A", status: "review" }),
      buildTask({ id: "t4", title: "Done A", status: "done" }),
      buildTask({ id: "t5", title: "Inbox B", status: "inbox" }),
    ];

    render(<TaskBoard tasks={tasks} />);

    const inboxHeading = screen.getByRole("heading", { name: "Inbox" });
    const inProgressHeading = screen.getByRole("heading", {
      name: "In Progress",
    });
    const reviewHeading = screen.getByRole("heading", { name: "Review" });
    const doneHeading = screen.getByRole("heading", { name: "Done" });

    expect(inboxHeading).toBeInTheDocument();
    expect(inProgressHeading).toBeInTheDocument();
    expect(reviewHeading).toBeInTheDocument();
    expect(doneHeading).toBeInTheDocument();

    const inboxColumn = inboxHeading.closest(
      ".kanban-column",
    ) as HTMLElement | null;
    const inProgressColumn = inProgressHeading.closest(
      ".kanban-column",
    ) as HTMLElement | null;
    const reviewColumn = reviewHeading.closest(
      ".kanban-column",
    ) as HTMLElement | null;
    const doneColumn = doneHeading.closest(
      ".kanban-column",
    ) as HTMLElement | null;
    expect(inboxColumn).toBeTruthy();
    expect(inProgressColumn).toBeTruthy();
    expect(reviewColumn).toBeTruthy();
    expect(doneColumn).toBeTruthy();
    if (!inboxColumn || !inProgressColumn || !reviewColumn || !doneColumn) return;

    const getColumnCountBadge = (column: HTMLElement) =>
      column.querySelector(
        ".column-header span.h-6.w-6.rounded-full",
      ) as HTMLElement | null;

    const inboxCountBadge = getColumnCountBadge(inboxColumn);
    const inProgressCountBadge = getColumnCountBadge(inProgressColumn);
    const reviewCountBadge = getColumnCountBadge(reviewColumn);
    const doneCountBadge = getColumnCountBadge(doneColumn);

    expect(inboxCountBadge).toHaveTextContent("2");
    expect(inProgressCountBadge).toHaveTextContent("1");
    expect(reviewCountBadge).toHaveTextContent("1");
    expect(doneCountBadge).toHaveTextContent("1");

    expect(screen.getByText("Inbox A")).toBeInTheDocument();
    expect(screen.getByText("Inbox B")).toBeInTheDocument();
  });

  it("filters the review column by bucket", () => {
    const tasks: Task[] = [
      buildTask({
        id: "blocked",
        title: "Blocked Review",
        status: "review",
        is_blocked: true,
        blocked_by_task_ids: ["dep-1"],
      }),
      buildTask({
        id: "approval",
        title: "Needs Approval",
        status: "review",
        approvals_pending_count: 2,
      }),
      buildTask({
        id: "lead",
        title: "Lead Review",
        status: "review",
      }),
    ];

    render(<TaskBoard tasks={tasks} />);

    const reviewHeading = screen.getByRole("heading", { name: "Review" });
    const reviewColumn = reviewHeading.closest(
      ".kanban-column",
    ) as HTMLElement | null;
    expect(reviewColumn).toBeTruthy();
    if (!reviewColumn) return;

    const header = reviewColumn.querySelector(
      ".column-header",
    ) as HTMLElement | null;
    expect(header).toBeTruthy();
    if (!header) return;

    const headerQueries = within(header);

    expect(
      headerQueries.getByRole("button", { name: /All · 3/i }),
    ).toBeInTheDocument();
    expect(
      headerQueries.getByRole("button", { name: /Approval needed · 1/i }),
    ).toBeInTheDocument();
    expect(
      headerQueries.getByRole("button", { name: /Queued review · 1/i }),
    ).toBeInTheDocument();
    expect(
      headerQueries.getByRole("button", { name: /Blocked · 1/i }),
    ).toBeInTheDocument();

    fireEvent.click(
      headerQueries.getByRole("button", { name: /Blocked · 1/i }),
    );
    expect(screen.getByText("Blocked Review")).toBeInTheDocument();
    expect(screen.queryByText("Needs Approval")).not.toBeInTheDocument();
    expect(screen.queryByText("Lead Review")).not.toBeInTheDocument();

    fireEvent.click(
      headerQueries.getByRole("button", { name: /Approval needed · 1/i }),
    );
    expect(screen.getByText("Needs Approval")).toBeInTheDocument();
    expect(screen.queryByText("Blocked Review")).not.toBeInTheDocument();
    expect(screen.queryByText("Lead Review")).not.toBeInTheDocument();

    fireEvent.click(
      headerQueries.getByRole("button", { name: /Queued review · 1/i }),
    );
    expect(screen.getByText("Lead Review")).toBeInTheDocument();
    expect(screen.queryByText("Blocked Review")).not.toBeInTheDocument();
    expect(screen.queryByText("Needs Approval")).not.toBeInTheDocument();
  });

  it("invokes onTaskMove when a task is dropped onto a different column", () => {
    const onTaskMove = vi.fn();
    const tasks: Task[] = [
      buildTask({ id: "t1", title: "Inbox A", status: "inbox" }),
    ];

    render(<TaskBoard tasks={tasks} onTaskMove={onTaskMove} />);

    const dropTarget = screen
      .getByRole("heading", { name: "Done" })
      .closest(".kanban-column") as HTMLElement | null;
    expect(dropTarget).toBeTruthy();
    if (!dropTarget) return;

    fireEvent.drop(dropTarget, {
      dataTransfer: {
        getData: () => JSON.stringify({ taskId: "t1", status: "inbox" }),
      },
    });

    expect(onTaskMove).toHaveBeenCalledWith("t1", "done");
  });

  it("does not allow dragging when readOnly is true", () => {
    const tasks: Task[] = [buildTask({ id: "t1", title: "Inbox A" })];

    render(<TaskBoard tasks={tasks} readOnly />);

    expect(screen.getByRole("button", { name: /Inbox A/i })).toHaveAttribute(
      "draggable",
      "false",
    );
  });
});

it.each([
  "00000000-0000-4000-8000-0000000000c1",
  "00000000-0000-4000-8000-0000000000c2",
])("shows backend refusal without claiming or changing historical Inbox fixture %s", (id) => {
  vi.mocked(customFetch).mockClear();
  const task = {
    ...buildTask({ id, title: "Historical safe fixture" }),
    execution_lane: "harness", execution_mode: "single",
    harness_provider: "omniroute-api", harness_model: "vllm/Qwen3.8-27B-FP8",
    workspace_path: null, assigned_agent_id: null, blocked_by_task_ids: [],
    in_progress_at: null, max_seconds_budget: 1800, max_tokens_budget: null,
    work_detail: "provider_unregistered: qualified read-only research runtime and accounting approval required; single executor unavailable",
  };
  const before = JSON.stringify(task);
  render(<TaskBoard tasks={[task]} readOnly />);
  const inbox = screen.getByRole("heading", { name: "Inbox" }).closest(".kanban-column") as HTMLElement;
  expect(within(inbox).getByText(task.work_detail)).toBeInTheDocument();
  expect(JSON.stringify(task)).toBe(before);
  expect(customFetch).not.toHaveBeenCalled();
});
