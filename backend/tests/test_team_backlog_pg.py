"""Real descendant-first locks and deletion rollback; disposable /mc_audit only."""
import asyncio
import os
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.organizations import Organization
from app.models.gateways import Gateway
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamCycle
from app.services import team_cycle as svc, watchdog_loop as wl
from tests.test_team_backlog import run_backlog_pm, RANKING


@pytest.fixture
async def pg():
    url = os.getenv('MC_TEST_POSTGRES_URL')
    if not url: pytest.skip('requires migrated disposable /mc_audit')
    assert url.endswith('/mc_audit'), 'Refusing non-disposable database'
    engine = create_async_engine(url)
    async with engine.begin() as c: await c.execute(text('TRUNCATE organizations CASCADE'))
    async with AsyncSession(engine, expire_on_commit=False) as s:
        org = Organization(id=uuid4(), name='backlog-pg'); s.add(org); await s.flush()
        gw = Gateway(id=uuid4(), organization_id=org.id, name='g', url='https://g.invalid', workspace_root='/tmp/w'); s.add(gw); await s.flush()
        board = Board(id=uuid4(), organization_id=org.id, name='b', slug=uuid4().hex, gateway_id=gw.id); s.add(board); await s.flush()
        parent = Task(id=uuid4(), board_id=board.id, title='[backlog] bounded', status='in_progress', execution_mode='team', execution_lane='harness', complexity=3)
        s.add(parent); await s.commit()
        await run_backlog_pm(s, board, parent)
        children = list(await s.exec(select(Task).where(Task.parent_task_id == parent.id).order_by(Task.id)))
        for child in children: child.status = 'done'
        await s.commit()
        yield engine, s, board, parent, children
    await engine.dispose()


async def test_parent_promotion_serializes_human_child_edit(pg, monkeypatch):
    engine, s, board, parent, children = pg
    original = s.commit
    changed = asyncio.Event()
    job = None
    async def edit_child():
        async with AsyncSession(engine, expire_on_commit=False) as human:
            changed.set()
            c = await human.get(Task, children[0].id)
            await human.refresh(c, with_for_update=True)
            c.status = 'blocked'
            await human.commit()
    async def guarded_commit():
        nonlocal job
        if parent.status == 'review' and job is None:
            job = asyncio.create_task(edit_child())
            await changed.wait()
            # Human cannot commit between final locked child read and promotion.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(job), .2)
        await original()
    monkeypatch.setattr(s, 'commit', guarded_commit)
    assert await asyncio.wait_for(wl._detect_native_split_parent_completion(s), 5) == 1
    await asyncio.wait_for(job, 5)
    await s.refresh(parent); await s.refresh(children[0])
    assert parent.status == 'review' and children[0].status == 'blocked'


async def test_committed_child_edit_before_promotion_is_seen(pg):
    engine, s, board, parent, children = pg
    async with AsyncSession(engine, expire_on_commit=False) as human:
        c = await human.get(Task, children[0].id)
        c.status = 'blocked'
        await human.commit()
    assert await wl._detect_native_split_parent_completion(s) == 0
    await s.refresh(parent)
    assert parent.status == 'in_progress'


async def test_child_cycle_and_parent_completion_do_not_reverse_lock_order(pg):
    engine, s, board, parent, children = pg
    child = children[0]; child.status = 'in_progress'
    cycle, _ = await svc.start_cycle(s, board, child, RANKING)
    child.status = 'done'; cycle.state = 'published'
    await s.commit()
    async with AsyncSession(engine, expire_on_commit=False) as worker:
        c = await worker.get(TeamCycle, cycle.id)
        await svc._lock_cycle(worker, c)  # holds child then cycle, no active-parent fence
        await svc._parent_problem(worker, await worker.get(Task, child.id))
        job = asyncio.create_task(wl._detect_native_split_parent_completion(s))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(job), .2)
        await worker.commit()
        assert await asyncio.wait_for(job, 5) == 1


async def test_native_parent_deletion_fk_failure_rolls_back_all_rows(pg):
    from app.api.tasks import delete_task_and_related_records
    engine, s, board, parent, children = pg
    pid, cids = parent.id, [c.id for c in children]
    with pytest.raises(IntegrityError):
        await delete_task_and_related_records(s, task=parent)
    await s.rollback()
    # Known legacy delete does not cascade native history. Failure is atomic;
    # this check does not claim successful deletion/archive support.
    assert await s.get(Task, pid)
    assert all([await s.get(Task, cid) for cid in cids])
    assert (await s.exec(select(TeamCycle).where(TeamCycle.task_id == pid))).first()


@pytest.mark.parametrize('mode,message', [
    ('team', 'Owner directive: NEW UNAPPROVED GOAL'),
    ('single', '**team/team-developer**: NEW UNAPPROVED GOAL'),
    ('single', '**team-coordinator**: NEW UNAPPROVED GOAL'),
    ('single', '**team-runner**: NEW UNAPPROVED GOAL'),
])
async def test_native_handoff_is_never_an_owner_directive(pg, monkeypatch, mode, message):
    from app.models.activity_events import ActivityEvent
    from app.services import llm_evaluator
    engine, s, board, parent, children = pg
    parent.execution_mode = mode; parent.description = 'Original owner goal'
    s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, message=message))
    await s.commit()
    async def forbidden(*args): raise AssertionError('machine output treated as human authority')
    monkeypatch.setattr(wl, '_OWNER_DIRECTIVES_ENABLED', True)
    monkeypatch.setattr(llm_evaluator, 'evaluate_owner_directive', forbidden)
    assert await wl._apply_owner_directives(s) == 0
    await s.refresh(parent)
    assert parent.description == 'Original owner goal'


@pytest.mark.parametrize('status', ['in_progress', 'review'])
async def test_native_consensus_emission_never_creates_qa_or_parent_promotion(pg, monkeypatch, status):
    from app.models.activity_events import ActivityEvent
    from app.models.agents import Agent
    engine, s, board, parent, children = pg
    parent.status = status
    for child in children: child.status = 'inbox'
    s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, message='[consensus-solicit] synthetic'))
    for i in range(2):
        agent = Agent(id=uuid4(), name=f'reviewer{i}', board_id=board.id, gateway_id=board.gateway_id)
        s.add(agent); await s.flush()
        s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, agent_id=agent.id, message='[ready-to-close] FINAL VERDICT: completed'))
    await s.commit()
    async def forbidden(*args, **kwargs): raise AssertionError('native consensus effect')
    monkeypatch.setattr(wl, '_post_comment', forbidden)
    monkeypatch.setattr(wl, '_seed_kb_from_consensus', forbidden)
    assert await wl._auto_emit_consensus_verdict(s) == 0
    await s.refresh(parent)
    assert parent.status == status


async def test_native_consensus_solicitation_never_routes_a_gateway_agent(pg, monkeypatch):
    engine, s, board, parent, children = pg
    board.autonomous_enabled = True; parent.status = 'inbox'; parent.title = 'crackme binary analysis'
    await s.commit()
    def forbidden(*args): raise AssertionError('native task entered legacy analyst selection')
    monkeypatch.setattr(wl, '_is_static_re_task', forbidden)
    assert await wl._solicit_consensus_for_tool_tasks(s) == 0
    await s.refresh(parent)
    assert parent.status == 'inbox' and parent.assigned_agent_id is None


async def test_legacy_owner_evaluation_cannot_overwrite_a_new_explicit_goal(pg, monkeypatch):
    from app.api import tasks as api
    from app.api.deps import ActorContext
    from app.schemas.tasks import TaskUpdate
    from app.models.users import User
    from app.models.organization_members import OrganizationMember
    from app.models.activity_events import ActivityEvent
    from app.services import llm_evaluator
    engine, s, board, parent, children = pg
    parent.execution_mode = 'single'; parent.execution_lane = 'agent'; parent.title = 'legacy'; parent.description = 'OLD GOAL'
    user = User(id=uuid4(), clerk_user_id=str(uuid4()), email='owner@invalid', active_organization_id=board.organization_id)
    s.add(user); await s.flush()
    s.add(OrganizationMember(organization_id=board.organization_id, user_id=user.id, role='owner', all_boards_write=True))
    s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, message='Adjust the old goal'))
    await s.commit(); tid, uid = parent.id, user.id
    async def noop(*args, **kwargs): pass
    monkeypatch.setattr(api, '_notify_task_update_assignment_changes', noop)
    monkeypatch.setattr(wl, '_OWNER_DIRECTIVES_ENABLED', True)
    async def evaluate(*args):
        async def owner():
            async with AsyncSession(engine, expire_on_commit=False) as other:
                task = await other.get(Task, tid); actor = await other.get(User, uid)
                await api.update_task(TaskUpdate(description='NEW EXPLICIT OWNER GOAL'), task=task, session=other, actor=ActorContext(actor_type='user', user=actor))
        # A task row lock must NOT be held throughout model evaluation.
        await asyncio.wait_for(owner(), 3)
        return 'MODEL REWRITE OF OLD GOAL'
    monkeypatch.setattr(llm_evaluator, 'evaluate_owner_directive', evaluate)
    assert await wl._apply_owner_directives(s) == 0
    await s.refresh(parent)
    assert parent.description == 'NEW EXPLICIT OWNER GOAL'


async def test_consensus_promotion_and_both_markers_hold_one_transaction(pg, monkeypatch):
    from app.models.activity_events import ActivityEvent
    from app.models.agents import Agent
    engine, s, board, parent, children = pg
    parent.execution_mode = 'single'; parent.execution_lane = 'agent'; parent.title = 'legacy'; parent.complexity = 4
    s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, message='[consensus-solicit] synthetic'))
    for i in range(2):
        agent = Agent(id=uuid4(), name=f'analyst{i}', board_id=board.id, gateway_id=board.gateway_id)
        s.add(agent); await s.flush()
        s.add(ActivityEvent(event_type='task.comment', task_id=parent.id, board_id=board.id, agent_id=agent.id, message='[ready-to-close] FINAL VERDICT: completed'))
    await s.commit(); tid = parent.id
    original = wl._post_comment; pending = []; started = asyncio.Event()
    async def owner():
        async with AsyncSession(engine, expire_on_commit=False) as other:
            started.set()
            await other.execute(text('UPDATE tasks SET execution_mode=:mode, description=:goal WHERE id=:id'), {'mode':'team', 'goal':'NEW EXPLICIT OWNER GOAL', 'id':tid})
            await other.commit()
    async def post(db, *args, **kwargs):
        assert kwargs.get('commit') is False
        result = await original(db, *args, **kwargs)
        if not pending:
            pending.append(asyncio.create_task(owner())); await started.wait()
        await asyncio.sleep(.1)
        assert not pending[0].done(), 'owner row write escaped before both markers committed'
        async with AsyncSession(engine) as observer:
            task = await observer.get(Task, tid)
            assert task.status == 'in_progress'
            events = list(await observer.exec(select(ActivityEvent).where(ActivityEvent.task_id == tid)))
            assert not any((e.message or '').startswith(('[qa-verdict]', '[pm-signoff]')) for e in events)
        return result
    async def noop(*args, **kwargs): pass
    monkeypatch.setattr(wl, '_post_comment', post); monkeypatch.setattr(wl, '_seed_kb_from_consensus', noop)
    try:
        assert await wl._auto_emit_consensus_verdict(s) == 2
        await asyncio.wait_for(pending[0], 3)
    finally:
        await s.rollback()
        for job in pending:
            if not job.done(): job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    await s.refresh(parent)
    assert parent.execution_mode == 'team' and parent.description == 'NEW EXPLICIT OWNER GOAL'
    events = list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id == tid)))
    assert sum((e.message or '').startswith(('[qa-verdict]', '[pm-signoff]')) for e in events) == 2


@pytest.mark.parametrize('method', ['_auto_approve_high_confidence_approvals', '_auto_close_review_tasks'])
async def test_legacy_approval_helpers_exclude_native_task_scope(pg, monkeypatch, method):
    from app.models.approvals import Approval
    from app.services import owner_reviews
    engine, s, board, parent, children = pg
    parent.status = 'review'
    approval = Approval(board_id=board.id, task_id=parent.id, action_type='security_authority', confidence=95, status='pending')
    s.add(approval); await s.commit()
    monkeypatch.setattr(owner_reviews, 'automation_enabled', lambda org:False)
    assert await getattr(wl, method)(s) == 0
    await s.refresh(approval); await s.refresh(parent)
    assert approval.status == 'pending' and parent.status == 'review'


@pytest.mark.parametrize('native', [True, False])
async def test_live_pev_inference_releases_task_lock_and_rejects_new_owner_goal(pg, monkeypatch, native):
    import asyncio, json
    from datetime import datetime, timezone
    from app.services import pev_loop as pev
    from app.models.approvals import Approval
    engine, s, board, parent, children = pg
    parent.execution_mode='single'; parent.execution_lane='agent'; parent.title='legacy text task'
    parent.split_count=0; parent.description='OLD GOAL'; board.autonomous_enabled=True
    await s.commit()
    tid=parent.id
    await pev._ensure_state(s, tid)
    await pev._save_state(s, tid, status=pev.STATUS_EXECUTING, plan=[{'description':'text check','verification':'old scope'}], step_index=0)
    await s.commit()
    async def pick(db,tier): return ('synthetic','synthetic-'+tier,[])
    monkeypatch.setattr(pev,'pick_model_for_tier',pick)
    wrote=False
    async def model(model_id,*args,**kwargs):
        nonlocal wrote
        if model_id.endswith('executor'):
            return json.dumps({'action':'old check','evidence':'synthetic evidence','self_assessment':'pass'})
        assert model_id.endswith('verifier')
        async def owner_edit():
            async with AsyncSession(engine, expire_on_commit=False) as other:
                current=await other.get(Task, tid)
                current.description='NEW OWNER GOAL'; current.updated_at=datetime.now(timezone.utc)
                if native: current.execution_mode='team'; current.execution_lane='harness'
                await other.commit()
        await asyncio.wait_for(owner_edit(), 3)
        wrote=True
        return json.dumps({'pass':True,'reason':'old goal passed'})
    monkeypatch.setattr(pev,'call_model',model)
    result=await pev.advance_pev(s, tid)
    await s.refresh(parent)
    assert wrote and not result['ok'] and parent.description=='NEW OWNER GOAL' and parent.status=='in_progress'
    assert not list(await s.exec(select(Approval).where(Approval.task_id==tid)))


async def test_native_parent_legacy_wallclock_budget_does_not_requeue(pg):
    engine, s, board, parent, children = pg
    await s.execute(text("UPDATE tasks SET max_seconds_budget=1, in_progress_at=NOW()-INTERVAL '2 hours' WHERE id=:id"), {'id':parent.id})
    await s.commit()
    assert await wl._enforce_task_budgets(s) == 0
    await s.refresh(parent)
    assert parent.status == 'in_progress'
