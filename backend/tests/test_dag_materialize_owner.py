"""DAG children belong to whoever created the parent, so the inbox router assigns them.

Also covers the hardened materialize boundary: children inherit the parent's
execution contract (team/harness settings, budgets, deadline) verbatim, and a
parent that already carries split children is refused.
"""

import asyncio
from uuid import uuid4

import pytest

from app.models.tasks import Task
from app.services.dag_materialize import DagValidationError, Subtask, materialize_dag


class _Db:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        pass

    async def refresh(self, obj, with_for_update=False):
        pass  # rows stay as in-memory instances, like a transaction-local read


def _parent(**over) -> Task:
    fields = dict(
        board_id=uuid4(),
        title="p",
        created_by_user_id=uuid4(),
        execution_mode="team",
        execution_lane="harness",
        workspace_path=None,
        harness_provider="claude-code",
        harness_model="opus-5",
        thinking_level="low",
        max_seconds_budget=1800,
        max_tokens_budget=80_000,
        due_at=None,
    )
    fields.update(over)
    return Task(**fields)


def test_children_inherit_the_parent_creator():
    owner = uuid4()
    parent = _parent(created_by_user_id=owner)
    db = _Db()
    asyncio.run(materialize_dag(db, parent, [Subtask("a", []), Subtask("b", [0])]))
    children = [r for r in db.rows if isinstance(r, Task) and r is not parent]
    assert len(children) == 2
    assert all(c.created_by_type is None and c.created_by_user_id == owner for c in children)


def test_children_inherit_the_parent_execution_contract():
    parent = _parent()
    db = _Db()
    asyncio.run(materialize_dag(db, parent, [Subtask("a", [])]))
    child = next(r for r in db.rows if isinstance(r, Task) and r is not parent)
    assert child.execution_mode == "team"
    assert child.execution_lane == "harness"
    assert child.workspace_path == parent.workspace_path
    assert (child.harness_provider, child.harness_model) == ("claude-code", "opus-5")
    assert child.thinking_level == "low"
    assert child.max_seconds_budget == 1800
    assert child.max_tokens_budget == 80_000
    assert child.due_at == parent.due_at
    assert child.assigned_agent_id is None  # no gateway assignee enlargement
    assert child.parent_task_id == parent.id and child.split_type == "dag"


def test_a_parent_with_existing_children_is_refused():
    parent = _parent(split_count=2, split_type="dag")
    db = _Db()
    with pytest.raises(DagValidationError, match="already has split children"):
        asyncio.run(materialize_dag(db, parent, [Subtask("a", [])]))
    assert db.rows == []  # validated before any effect
