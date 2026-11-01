"""Native DAG leaves run, but parent edits/decisions fence their descendants."""
import pytest
from sqlmodel import select
from app.models.tasks import Task
from app.models.activity_events import ActivityEvent
from app.services import team_cycle as svc
from app.services import watchdog_loop as wl
from tests.test_team_backlog import env, run_backlog_pm, RANKING, BASE


async def child_context(env):
    s, board, parent = env
    await run_backlog_pm(s, board, parent)
    child = (await s.exec(select(Task).where(Task.parent_task_id == parent.id).order_by(Task.split_position))).first()
    child.status = "in_progress"
    await s.commit()
    return s, board, parent, child


async def test_dag_leaf_is_runnable_despite_its_membership_split_type(env):
    s, board, parent, child = await child_context(env)
    assert child.split_type == "dag" and child.split_count == 0
    cycle, _ = await svc.start_cycle(s, board, child, RANKING)
    assert cycle.task_id == child.id and cycle.state == "planning"
    assert await svc.claim_next(s, cycle, base_sha=BASE)
    assert parent.status == "in_progress"


@pytest.mark.parametrize("field,value", [("status", "blocked"), ("status", "done"), ("description", "changed owner goal")])
async def test_changed_parent_prevents_a_new_child_cycle(env, field, value):
    s, board, parent, child = await child_context(env)
    setattr(parent, field, value)
    await s.commit()
    with pytest.raises(svc.TeamFenced, match="parent"):
        await svc.start_cycle(s, board, child, RANKING)
    assert child.status == "blocked"
    assert getattr(parent, field) == value  # no automatic reopening/goal restoration


async def test_parent_change_fences_running_child_at_completion(env):
    s, board, parent, child = await child_context(env)
    cycle, _ = await svc.start_cycle(s, board, child, RANKING)
    pm, token = await svc.claim_next(s, cycle, base_sha=BASE)
    await s.commit()
    parent.status = "blocked"
    await s.commit()
    with pytest.raises(svc.TeamFenced, match="parent"):
        await svc.complete_attempt(s, board, cycle, pm, token, ok=True,
                                   artifact='{"workers":[{"key":"one","scope":["backend"]}]}', ranking=RANKING)
    assert cycle.state == "cancelled" and child.status == "blocked"
    assert parent.status == "blocked"


async def test_done_children_of_an_old_goal_do_not_promote_edited_parent(env):
    s, board, parent = env
    await run_backlog_pm(s, board, parent)
    for child in await s.exec(select(Task).where(Task.parent_task_id == parent.id)):
        child.status = "done"
    parent.description = "new owner goal after original children finished"
    await s.commit()
    assert await wl._detect_native_split_parent_completion(s) == 0
    await s.refresh(parent)
    assert parent.status == "in_progress"


async def test_native_dag_never_uses_legacy_reject_markers_to_fake_done(env):
    s, board, parent = env
    await run_backlog_pm(s, board, parent)
    children = list(await s.exec(select(Task).where(Task.parent_task_id == parent.id).order_by(Task.split_position)))
    children[0].status = "done"
    s.add(ActivityEvent(event_type="task.comment", task_id=children[0].id,
                        board_id=board.id, message="[pm-signoff] verdict=reject"))
    await s.commit()
    assert await wl._propagate_dag_cancellations(s) == 0
    await s.refresh(children[1])
    assert children[1].status == "inbox"


async def test_native_pm_artifact_cannot_activate_legacy_marker_materializer(env):
    s, board, parent = env
    s.add(ActivityEvent(event_type="task.comment", task_id=parent.id, board_id=board.id,
                        message='[pm-plan] subtasks=\'[{"title":"unapproved","deps":[]}]\''))
    await s.commit()
    assert await wl._materialize_dag_from_pm_plan(s) == 0
    assert parent.split_count == 0

