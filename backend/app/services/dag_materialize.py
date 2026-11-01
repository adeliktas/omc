"""Subtask DAG materialization service (E1, 2026-05-14).

Parses PM `[pm-plan]` markers that declare a subtask DAG, validates structure
+ limits + cycle-freedom, and materializes child Task rows + TaskDependency
edges atomically.

DAG declaration syntax (Q1 default — JSON inline):
    [pm-plan] level=L4 plan="..." subtasks='[
        {"title": "Step A", "deps": []},
        {"title": "Step B", "deps": [0]},
        {"title": "Step C", "deps": [0]},
        {"title": "Step D", "deps": [1, 2]}
    ]'

`deps` entries are 0-based indices into the same subtasks array.

Limits (Q5 defaults): max 20 children per DAG, max chain depth 3.
Cycle-freedom: deps must form a DAG.

Public surface:
    parse_subtasks_payload(raw: str) -> list[Subtask]    # validates structure
    validate_subtasks(items: list[Subtask]) -> None       # raises on violation
    materialize_dag(db, parent_task, items)
        -> dict[str, Any]                                 # creates rows
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.logging import get_logger
from app.models.task_dependencies import TaskDependency
from app.models.tasks import Task

logger = get_logger(__name__)

MAX_CHILDREN = 20
MAX_DEPTH = 3


class DagValidationError(ValueError):
    """Raised when a proposed subtasks payload is malformed or violates limits."""


@dataclass
class Subtask:
    title: str
    deps: list[int]
    description: str | None = None
    priority: str | None = None
    complexity: int | None = None


def parse_subtasks_payload(raw: str) -> list[Subtask]:
    """Parse + structurally validate a subtasks JSON payload.

    Raises DagValidationError on any malformed input. Does not validate
    semantic limits (size/cycles) — see `validate_subtasks`.
    """
    if not raw or not raw.strip():
        raise DagValidationError("empty subtasks payload")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DagValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise DagValidationError("subtasks must be a JSON array")
    if not data:
        raise DagValidationError("subtasks array is empty")

    items: list[Subtask] = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise DagValidationError(f"subtask[{idx}] must be an object")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            raise DagValidationError(f"subtask[{idx}].title must be a non-empty string")
        deps_raw = entry.get("deps", [])
        if not isinstance(deps_raw, list) or not all(
            type(d) is int for d in deps_raw
        ):  # bool is not an int here
            raise DagValidationError(f"subtask[{idx}].deps must be a list of ints")
        desc = entry.get("description")
        if desc is not None and not isinstance(desc, str):
            raise DagValidationError(f"subtask[{idx}].description must be a string or omitted")
        priority = entry.get("priority")
        if priority is not None and not isinstance(priority, str):
            raise DagValidationError(f"subtask[{idx}].priority must be a string or omitted")
        complexity = entry.get("complexity")
        if complexity is not None and type(complexity) is not int:
            raise DagValidationError(f"subtask[{idx}].complexity must be an int or omitted")
        items.append(
            Subtask(
                title=title.strip(),
                deps=list(deps_raw),
                description=desc,
                priority=priority,
                complexity=complexity,
            )
        )
    return items


def validate_subtasks(items: list[Subtask]) -> None:
    """Semantic validation: limits, dep-index bounds, cycle-freedom, depth."""
    n = len(items)
    if n > MAX_CHILDREN:
        raise DagValidationError(f"DAG exceeds {MAX_CHILDREN}-child limit (got {n})")
    for idx, sub in enumerate(items):
        if sub.complexity is not None and (type(sub.complexity) is not int or not 1 <= sub.complexity <= 5):
            raise DagValidationError(f"subtask[{idx}].complexity must be in 1..5")
        for d in sub.deps:
            if not (0 <= d < n):
                raise DagValidationError(f"subtask[{idx}].deps[{d}] out of range [0..{n-1}]")
            if d == idx:
                raise DagValidationError(f"subtask[{idx}] cannot depend on itself")
    # Cycle detection (DFS).
    if _has_cycle(items):
        raise DagValidationError("DAG contains a cycle in `deps`")
    # Depth check (longest dep chain).
    if _max_depth(items) > MAX_DEPTH:
        raise DagValidationError(f"DAG depth exceeds limit {MAX_DEPTH}")


def _has_cycle(items: list[Subtask]) -> bool:
    n = len(items)
    visited = [0] * n  # 0=unseen, 1=in-stack, 2=done

    def dfs(node: int) -> bool:
        if visited[node] == 1:
            return True
        if visited[node] == 2:
            return False
        visited[node] = 1
        for dep in items[node].deps:
            if dfs(dep):
                return True
        visited[node] = 2
        return False

    return any(dfs(i) for i in range(n) if visited[i] == 0)


def _max_depth(items: list[Subtask]) -> int:
    """Longest path from any root to any leaf (1-based: root = depth 1)."""
    n = len(items)
    cache: dict[int, int] = {}

    def depth(node: int) -> int:
        if node in cache:
            return cache[node]
        if not items[node].deps:
            cache[node] = 1
            return 1
        d = 1 + max(depth(dep) for dep in items[node].deps)
        cache[node] = d
        return d

    return max((depth(i) for i in range(n)), default=0)


async def materialize_dag(
    db: AsyncSession,
    parent_task: Task,
    items: list[Subtask],
) -> dict[str, Any]:
    """Create child Task rows + TaskDependency edges atomically.

    Children inherit `board_id` + `created_by_*` from the parent, plus the
    execution settings the parent carries (execution_lane/execution_mode/
    workspace_path/harness_provider/harness_model/thinking_level/
    max_seconds_budget/max_tokens_budget/due_at) so a team/harness parent gets
    team/harness children — never a silently default single-lane child. Each
    child gets `parent_task_id = parent.id`, `split_position = i`,
    `split_type = "dag"`. Children start in `inbox` with no `assigned_agent_id`
    (watchdog routes them after grace + dep gate).

    Returns summary `{"created": N, "edges": M, "child_ids": [...]}`.

    Hardened boundary: refuses to double-materialize a parent that already
    carries split children, and re-reads the parent row under the same
    transaction lock order the team cycle uses (task first). Callers keep
    owning their own commits; nothing here commits.
    """
    if not parent_task.board_id:
        raise DagValidationError("parent task has no board_id — cannot materialize DAG")
    await db.flush()
    await db.refresh(parent_task, with_for_update=True)
    if parent_task.split_count:
        raise DagValidationError(
            "parent task already has split children; refusing to materialize again"
        )

    # Bind native children to this exact owner goal, not later parent edits.
    reason = "pm-plan DAG materialization"
    if parent_task.execution_mode == "team":
        from app.services.team_cycle import task_fingerprint
        reason = f"native-backlog:{parent_task.id}:{task_fingerprint(parent_task)}"
    # Pre-allocate child IDs so we can wire edges in the same transaction
    # without an intermediate flush.
    child_ids: list[UUID] = [uuid4() for _ in items]

    for idx, sub in enumerate(items):
        child = Task(
            id=child_ids[idx],
            board_id=parent_task.board_id,
            title=sub.title,
            description=sub.description,
            status="inbox",
            priority=sub.priority or parent_task.priority or "medium",
            complexity=sub.complexity if sub.complexity is not None else parent_task.complexity,
            parent_task_id=parent_task.id,
            split_position=idx,
            split_type="dag",
            auto_created=True,
            auto_reason=reason,
            # Inherit the owner's identity: the inbox router skips `system` tasks,
            # so children stamped `system` were never assigned.
            created_by_type=parent_task.created_by_type,
            created_by_name=parent_task.created_by_name,
            created_by_user_id=parent_task.created_by_user_id,
            # Inherit the parent's execution contract verbatim: a team/harness
            # parent must produce team/harness children runnable by the pool,
            # and no gateway assignee/workspace is enlarged here (children keep
            # exactly the parent's workspace, never a wider one).
            execution_lane=parent_task.execution_lane,
            execution_mode=parent_task.execution_mode,
            workspace_path=parent_task.workspace_path,
            harness_provider=parent_task.harness_provider,
            harness_model=parent_task.harness_model,
            thinking_level=parent_task.thinking_level,
            max_seconds_budget=parent_task.max_seconds_budget,
            max_tokens_budget=parent_task.max_tokens_budget,
            due_at=parent_task.due_at,
        )
        db.add(child)

    # Flush so children exist in DB before task_dependencies edges
    # (depends_on_task_id FK can't be resolved across un-flushed rows).
    await db.flush()

    edge_count = 0
    for idx, sub in enumerate(items):
        for dep_idx in sub.deps:
            db.add(
                TaskDependency(
                    board_id=parent_task.board_id,
                    task_id=child_ids[idx],
                    depends_on_task_id=child_ids[dep_idx],
                )
            )
            edge_count += 1

    # Also mark the parent: split_count tracks child count for DAG too.
    parent_task.split_count = len(items)
    parent_task.split_type = "dag"
    db.add(parent_task)

    logger.info(
        "dag.materialized parent=%s children=%d edges=%d",
        parent_task.id,
        len(items),
        edge_count,
    )
    return {
        "created": len(items),
        "edges": edge_count,
        "child_ids": [str(cid) for cid in child_ids],
    }
