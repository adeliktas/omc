import { expect, it } from "vitest";

import { rollbackTaskMove } from "./task-move";

it("rolls back only move fields after concurrent updates without resurrecting tasks", async () => {
  const moved = {
    id: "moved",
    status: "in_progress" as const,
    assigned_agent_id: "agent-1",
    assignee: "Agent One",
    title: "Original title",
  };
  const other = { ...moved, id: "other" };
  const deleted = { ...moved, id: "deleted" };
  let tasks = [moved, other, deleted].map((task) => ({
    ...task,
    status: task.id === moved.id ? "inbox" as const : task.status,
    assigned_agent_id: task.id === moved.id ? null : task.assigned_agent_id,
    assignee: task.id === moved.id ? null : task.assignee,
  }));
  let failMove!: (error: Error) => void;
  const request = new Promise<never>((_, reject) => { failMove = reject; });
  const pendingMove = request.catch(() => {
    tasks = rollbackTaskMove(tasks, moved);
  });

  // SSE merges arrive while the optimistic move is pending.
  const updatedOther = { ...other, title: "New other title" };
  const added = { ...other, id: "added" };
  tasks = [
    { ...tasks[0], title: "New moved title" },
    updatedOther,
    added,
  ];
  failMove(new Error("Move failed"));
  await pendingMove;

  expect(tasks).toEqual([
    { ...moved, title: "New moved title" },
    updatedOther,
    added,
  ]);
  expect(tasks[1]).toBe(updatedOther);
  expect(tasks[2]).toBe(added);
  // A moved task removed by SSE must not be resurrected either.
  expect(rollbackTaskMove(tasks.slice(1), moved)).toEqual(tasks.slice(1));
});
