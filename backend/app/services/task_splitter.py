"""Task splitting service — decomposes a task into parallel subtasks.

A "split" operation creates N child tasks on the same board, each with a
portion of the parent description. Children are auto-routed by the watchdog's
existing inbox triage. Parent auto-completes when all children reach "done".
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.tasks import Task
from app.services import runtime_settings

logger = get_logger(__name__)


def should_auto_split(task: Task) -> bool:
    """Keep optional fan-out separate from explicit, user-requested splitting."""
    return (
        bool(runtime_settings.get_setting("TASK_AUTO_SPLIT_ENABLED"))
        and (task.complexity or 0) >= 4
        and not task.parent_task_id
        and task.execution_lane != "harness"
        and not (task.execution_lane is None and (task.title or "").startswith(
            ("[agent]", "[claude]", "[codex]")
        ))
    )

_HEADING_RE = re.compile(r"^#{2,4}\s+(.+)$", re.MULTILINE)


# T104 (2026-05-23): role fan-out hints. After splitter creates children
# we tag each one with a preferred_role hint so watchdog inbox triage
# distributes children to DIFFERENT spec roles instead of collapsing them
# back to the parent's assignee. Order = rotation order; len > splits is
# fine, splitter takes [:n].
_FANOUT_ROLES_RE = (
    "Reverse Engineer",
    "Binary Analyst",
    "Security Architect",
)
_FANOUT_ROLES_GENERIC = (
    "Software Architect",
    "QA Engineer",
    "DevOps Engineer",
)


def _pick_fanout_roles(parent_title: str, parent_desc: str, n: int) -> list[str]:
    """Pick N distinct spec roles for parent's children based on its body."""
    body = f"{parent_title} {parent_desc}".lower()
    if any(k in body for k in ("crackme", "ida pro", "ida-pro", "reverse", "disassembl", "binary", "malware", "elf")):
        seq = _FANOUT_ROLES_RE
    else:
        seq = _FANOUT_ROLES_GENERIC
    if n <= len(seq):
        return list(seq[:n])
    out = list(seq)
    while len(out) < n:
        out.append(seq[len(out) % len(seq)])
    return out


async def _verify_children_persisted(
    db: AsyncSession, parent_id: UUID, expected_ids: list[UUID]
) -> tuple[int, list[UUID]]:
    """Read-back guard: count persisted children matching expected IDs.

    Returns (persisted_count, missing_ids). Caller should fail-loud
    when missing_ids is non-empty so the parent's split_count + activity
    log do NOT claim more children than actually exist.
    """
    res = await db.execute(
        text(
            "SELECT id FROM tasks WHERE parent_task_id = :pid AND id = ANY(:ids)"
        ),
        {"pid": str(parent_id), "ids": [str(i) for i in expected_ids]},
    )
    found = {r[0] for r in res.fetchall()}
    missing = [i for i in expected_ids if str(i) not in {str(f) for f in found}]
    return len(found), missing


async def split_by_count(
    db: AsyncSession,
    task: Task,
    count: int,
    *,
    actor_id: UUID | None = None,
) -> list[Task]:
    """Split a task into `count` equal children.

    The parent description is distributed evenly: child N gets the N-th
    portion. If the description is too short to split evenly, children
    share the full description.
    """
    if count < 2:
        raise ValueError("count must be at least 2")
    if task.split_count > 0:
        raise ValueError("task is already split")

    desc = (task.description or "").strip()
    if desc:
        paragraphs = [p.strip() for p in desc.split("\n\n") if p.strip()]
    else:
        paragraphs = [task.title]

    # T103 v39 (2026-05-23): clamp count to paragraph count. Previous
    # math (start = i*N//count) produced empty subtasks when count >
    # len(paragraphs) — e.g. 2 paragraphs / count=3 left subtask 1/3
    # with an empty description. Owner saw three children, one totally
    # blank, no agent able to act on it.
    effective_count = max(1, min(count, len(paragraphs)))
    fanout_roles = _pick_fanout_roles(task.title or "", task.description or "", effective_count)

    children: list[Task] = []
    for i in range(effective_count):
        start = i * len(paragraphs) // effective_count
        end = (i + 1) * len(paragraphs) // effective_count
        portion = paragraphs[start:end] if start < len(paragraphs) else paragraphs
        if not portion:
            continue

        role_hint = fanout_roles[i] if i < len(fanout_roles) else ""
        child = Task(
            id=uuid4(),
            board_id=task.board_id,
            title=f"[Subtask {i + 1}/{effective_count}] {task.title}",
            description="\n\n".join(portion),
            status="inbox",
            priority=task.priority,
            complexity=task.complexity,
            parent_task_id=task.id,
            split_position=i,
            split_type="by_count",
            auto_created=task.auto_created,
            # T104: encode preferred role into auto_reason so watchdog inbox
            # triage can fan out to distinct specs instead of collapsing
            # all children back to parent's assignee.
            auto_reason=f"split_from:{task.id};role={role_hint}",
            created_by_user_id=task.created_by_user_id,
            created_by_type=task.created_by_type,
            created_by_name=task.created_by_name,
            scheduled_at=task.scheduled_at,
        )
        db.add(child)
        children.append(child)

    task.split_type = "by_count"
    task.status = "in_progress"  # parent stays in_progress while children work
    task.updated_at = datetime.now(timezone.utc)
    db.add(task)

    await db.commit()

    # T104 phantom-subtask guard: read back and verify all children
    # actually persisted before claiming split_count. Previously
    # split_count was set to len(in-memory children) which could exceed
    # the post-commit row count (e.g. partial flush, FK trigger, RLS
    # filter). Owner saw activity log claim "3 subtasks" with only 1
    # row in DB.
    persisted, missing = await _verify_children_persisted(db, task.id, [c.id for c in children])
    if missing:
        logger.error(
            "task_splitter.children_missing parent=%s expected=%d persisted=%d missing=%s",
            task.id, len(children), persisted, missing,
        )
        # Trim in-memory list to what's actually in DB so caller's
        # activity log + split_count match reality.
        kept_ids = {c.id for c in children if c.id not in set(missing)}
        children = [c for c in children if c.id in kept_ids]

    task.split_count = persisted
    db.add(task)
    await db.commit()

    for c in children:
        try:
            await db.refresh(c)
        except Exception:
            pass
    return children


async def split_by_sections(
    db: AsyncSession,
    task: Task,
    *,
    actor_id: UUID | None = None,
) -> list[Task]:
    """Split a task by markdown `## Section` headings.

    Each top-level `## Heading` becomes a child task. Text before the first
    heading is distributed as context in every child.
    """
    if task.split_count > 0:
        raise ValueError("task is already split")

    desc = (task.description or "").strip()
    # Find all section boundaries
    matches = list(_HEADING_RE.finditer(desc))
    if len(matches) < 2:
        raise ValueError("description needs at least 2 ## sections to split")

    # Preamble: everything before the first heading
    preamble = desc[:matches[0].start()].strip()

    sections: list[tuple[str, str]] = []
    for idx, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(desc)
        body = desc[start:end].strip()
        if preamble:
            body = f"{preamble}\n\n{body}"
        sections.append((title, body))

    fanout_roles = _pick_fanout_roles(task.title or "", task.description or "", len(sections))
    children: list[Task] = []
    for i, (section_title, section_body) in enumerate(sections):
        role_hint = fanout_roles[i] if i < len(fanout_roles) else ""
        child = Task(
            id=uuid4(),
            board_id=task.board_id,
            title=f"[Subtask {i + 1}/{len(sections)}] {section_title}",
            description=section_body,
            status="inbox",
            priority=task.priority,
            complexity=task.complexity,
            parent_task_id=task.id,
            split_position=i,
            split_type="by_sections",
            auto_created=task.auto_created,
            auto_reason=f"split_from:{task.id};role={role_hint}",
            created_by_user_id=task.created_by_user_id,
            created_by_type=task.created_by_type,
            created_by_name=task.created_by_name,
            scheduled_at=task.scheduled_at,
        )
        db.add(child)
        children.append(child)

    task.split_type = "by_sections"
    task.status = "in_progress"
    task.updated_at = datetime.now(timezone.utc)
    db.add(task)

    await db.commit()

    persisted, missing = await _verify_children_persisted(db, task.id, [c.id for c in children])
    if missing:
        logger.error(
            "task_splitter.children_missing parent=%s expected=%d persisted=%d missing=%s",
            task.id, len(children), persisted, missing,
        )
        kept_ids = {c.id for c in children if c.id not in set(missing)}
        children = [c for c in children if c.id in kept_ids]

    task.split_count = persisted
    db.add(task)
    await db.commit()

    for c in children:
        try:
            await db.refresh(c)
        except Exception:
            pass
    return children
