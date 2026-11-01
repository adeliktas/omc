import type { TaskCardRead } from "@/api/generated/model";

type MoveFields = Pick<
  TaskCardRead,
  "id" | "status" | "assigned_agent_id" | "assignee"
>;

export function rollbackTaskMove<T extends MoveFields>(
  tasks: T[],
  previous: MoveFields,
): T[] {
  return tasks.map((task) =>
    task.id === previous.id
      ? {
          ...task,
          status: previous.status,
          assigned_agent_id: previous.assigned_agent_id,
          assignee: previous.assignee,
        }
      : task,
  );
}
