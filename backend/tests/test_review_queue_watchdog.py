"""Queue recurrence and legacy/harness ownership, without inference or live writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import sqlite3
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.boards import Board
from app.models.users import User
from app.models.organization_members import OrganizationMember
from app.api.deps import ActorContext
from app.schemas.tasks import TaskCreate, TaskUpdate

from app.core.time import utcnow
from app.services import owner_reviews, watchdog_loop
from app.services import pev_loop
from app.models.owner_reviews import OwnerReview
from app.models.agents import Agent
from app.models.tasks import Task
from app.models.task_dependencies import TaskDependency
from app.models.activity_events import ActivityEvent
from app.api import tasks as task_api
from app.api import agent as agent_api
from app.core.agent_auth import AgentAuthContext
from tests.test_team_legacy_marker_races import convert, noop
from sqlmodel import select
from tests.test_team_backlog import env
from tests.test_team_legacy_marker_races import patch_sql


@pytest.mark.parametrize("mode,lane,title,expected", [
    ("single", "agent", "real stuck agent", True),
    ("single", "harness", "legacy harness awaiting QA", False),
    ("team", "harness", "expired native team", False),
    ("team", "agent", "native team", False),
    ("single", None, "[claude] legacy harness", False),
])
async def test_stale_scan_and_effect_fences(env, monkeypatch, mode, lane, title, expected):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.title = mode, lane, title
    task.status, task.updated_at = "review", utcnow() - timedelta(days=2)
    s.add(task)
    await s.commit()
    patch_sql(s, monkeypatch)
    await s.execute(text("CREATE TABLE task_pev_state (task_id TEXT, status TEXT)"))
    stamp, task_id = task.updated_at, str(task.id)
    scanned = await watchdog_loop._stale_tasks(s)
    entry = next((t for t in scanned if t["id"].replace("-", "") == task_id.replace("-", "")), None)
    if mode == "team" or lane == "harness":
        assert entry is None
    assert bool(await watchdog_loop._lock_legacy_task(s, task_id, stamp, "review")) == expected
    await s.rollback()
    if not expected:
        from app.services import owner_reviews as reviews
        monkeypatch.setattr(reviews, "insert_sync", lambda **kw: pytest.fail("no harness escalation"))
        assert not watchdog_loop._escalate_to_owner_review({
            "id": task_id, "title": title, "execution_mode": mode, "execution_lane": lane,
        })


async def test_scan_then_harness_conversion_cannot_emit_legacy_effects(env, monkeypatch):
    s, board, task = env
    task.execution_mode, task.execution_lane = "single", "agent"
    task.updated_at, task.status = utcnow() - timedelta(days=2), "review"
    s.add(task)
    await s.commit()
    stamp, task_id, board_id = task.updated_at, str(task.id), str(board.id)
    patch_sql(s, monkeypatch)
    task.execution_lane = "harness"  # A lane-only owner edit is also an authority change.
    s.add(task)
    await s.commit()
    assert await watchdog_loop._lock_legacy_task(s, task_id, stamp, "review") is None
    with pytest.raises(RuntimeError, match="legacy comment fenced"):
        await watchdog_loop._post_comment(s, task_id.replace("-", ""), board_id.replace("-", ""), "false stale nudge", commit=False)


@pytest.mark.parametrize("status", ["pending", "revision_requested"])
async def test_pev_consent_preserves_open_step_clock(env, monkeypatch, status):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.title = "single", "agent", "consent"
    old = utcnow() - timedelta(days=10)
    item = OwnerReview(task_id=task.id, title="PEV consent: consent", submitted_by="PEV Loop",
                       summary="Step 1/2 touches protected path. Description: retained",
                       status=status, submitted_at=old, reviewer_note="whole owner hold")
    s.add(item)
    await s.commit()
    patch_sql(s, monkeypatch)
    async def state(*args):
        return {"status":"executing", "step_index":0,
                "plan":[{"requires_owner_consent":True, "description":"retained"}, {}]}
    async def save(*args, **kw): pass
    monkeypatch.setattr(pev_loop, "_get_state", state)
    monkeypatch.setattr(pev_loop, "_save_state", save)
    # The production query binds a UUID string; the scratch SQLite column is hex.
    original = s.execute
    async def sql(stmt, params=None, **kw):
        if params:
            params = dict(params)
            for key in ("tid", "rid", "bid"):
                if params.get(key):
                    params[key] = UUID(str(params[key])).hex
        return await original(stmt, params, **kw)
    monkeypatch.setattr(s, "execute", sql)
    assert (await pev_loop._run_executor_step(s, task.id))["escalated"]
    await s.refresh(item)
    assert item.submitted_at == old and item.reviewer_note == "whole owner hold"
    assert len(list(await s.exec(select(OwnerReview)))) == 1


@pytest.mark.parametrize("lane,title", [("harness", "native"), (None, "[claude] legacy")])
async def test_refused_marker_rolls_back_caller_update_and_reports_false(env, monkeypatch, lane, title):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.title = "single", lane, title
    task.status = "inbox"
    await s.commit()
    tid, bid = task.id.hex, board.id.hex
    patch_sql(s, monkeypatch)
    await s.execute(text("UPDATE tasks SET status='done' WHERE id=:tid"), {"tid":tid})
    assert await watchdog_loop._post_comment(s, tid, bid, "false completion") is False
    await s.refresh(task)
    assert task.status == "inbox"
    assert not list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==task.id)))


async def test_dependency_failure_cannot_complete_or_reassign_harness(env, monkeypatch):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.status = "single", "harness", "inbox"
    upstream = Task(id=uuid4(), board_id=board.id, title="failed", status="done",
                    execution_mode="single", execution_lane="agent", split_type="dag", parent_task_id=task.id)
    agent = Agent(id=uuid4(), board_id=board.id, gateway_id=board.gateway_id, name="agent")
    s.add(upstream); s.add(agent)
    await s.flush()
    s.add(TaskDependency(board_id=board.id, task_id=task.id, depends_on_task_id=upstream.id))
    s.add(ActivityEvent(task_id=upstream.id, board_id=board.id, event_type="task.comment",
                        message="[pm-signoff] verdict=reject"))
    await s.commit()
    tid, aid = task.id.hex, agent.id.hex
    patch_sql(s, monkeypatch)
    assert await watchdog_loop._propagate_dag_cancellations(s) == 0
    assert await watchdog_loop._reassign_task(s, tid, aid, task.updated_at, "must not appear") is False
    await s.refresh(task)
    assert task.status == "inbox" and task.assigned_agent_id is None


async def test_owner_question_conversion_fences_irreversible_queue_insert(env, monkeypatch):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.status = "single", "agent", "review"
    await s.commit()
    tid, bid, stamp = str(task.id), str(board.id), task.updated_at
    original = s.execute
    converted, inserted = [], []
    class Rows:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
    monkeypatch.setattr(task_api, "_notify_task_update_assignment_changes", noop)
    async def sql(stmt, *args, **kw):
        if "JOIN activity_events ae" in str(stmt):
            converted.append(await convert(s, board, task))
            return Rows([(tid, task.title, "review", bid, None, task.created_at, stamp,
                          None, "please confirm", "single", "agent")])
        if "SELECT task_id FROM owner_reviews" in str(stmt): return Rows([])
        return await original(stmt, *args, **kw)
    monkeypatch.setattr(s, "execute", sql)
    monkeypatch.setattr(owner_reviews, "insert_sync", lambda **kw: inserted.append(kw) or True)
    await watchdog_loop._detect_owner_questions(s)
    await s.refresh(task)
    assert converted and task.execution_mode == "team" and not inserted


@pytest.mark.parametrize("mode,lane,title,allowed", [
    ("single", "agent", "agent positive", True),
    ("single", "harness", "native", False),
    ("team", "harness", "team", False),
    ("single", None, "[claude] legacy", False),
])
async def test_contested_ballot_ownership_and_conversion(env, monkeypatch, tmp_path, mode, lane, title, allowed):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.title, task.status = mode, lane, title, "review"
    for verdict in ("PASS", "FAIL"):
        s.add(ActivityEvent(board_id=board.id, task_id=task.id, event_type="task.comment",
                            message=f"QA Verdict: {verdict}"))
    await s.commit()
    patch_sql(s, monkeypatch)
    votes = tmp_path / "votes.db"
    with sqlite3.connect(votes) as conn:
        conn.execute("CREATE TABLE ballots (id TEXT, topic TEXT, description TEXT, proposed_action TEXT, "
                     "target_task_id TEXT, target_board_id TEXT, required_quorum INTEGER, veto_roles TEXT, "
                     "status TEXT, created_by TEXT, created_at TEXT, expires_at TEXT)")
    monkeypatch.setenv("VOTES_DB_FILE", str(votes))
    assert await watchdog_loop._detect_contested_verdict(s) == int(allowed)
    with sqlite3.connect(votes) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == int(allowed)
    if not allowed: return
    # Fresh agent row scanned, then actual owner conversion before irreversible insert.
    with sqlite3.connect(votes) as conn:
        conn.execute("DELETE FROM ballots")  # Own disposable fixture only.
    original = s.execute
    converted = []
    async def interleave(stmt, *args, **kw):
        result = await original(stmt, *args, **kw)
        if "SELECT DISTINCT ae.task_id" in str(stmt):
            rows = result.fetchall()
            converted.append(await convert(s, board, task))
            class Rows:
                def fetchall(self): return rows
            return Rows()
        return result
    monkeypatch.setattr(s, "execute", interleave)
    assert await watchdog_loop._detect_contested_verdict(s) == 0
    with sqlite3.connect(votes) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ballots").fetchone()[0] == 0
    assert converted


def test_postgres_competing_sync_escalations(monkeypatch):
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires migrated disposable mc_audit")
    assert url.rstrip("/").endswith("/mc_audit"), "Refusing non-disposable database"
    engine = create_engine(url.replace("postgresql+asyncpg:", "postgresql+psycopg:"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar_one() == "mc_audit"
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(owner_reviews, "sync_session_maker", factory)
    marker = uuid4().hex
    payload = dict(title=f"audit {marker}", type_="other", summary="first complete findings",
                   reason=marker, proposed_action="inspect", submitted_by="Pipeline Watchdog",
                   task_id=str(uuid4()))
    barrier = Barrier(4)
    def insert():
        barrier.wait(timeout=30)
        return owner_reviews.insert_sync(**payload)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sum(pool.map(lambda _: insert(), range(4))) == 1
        with factory() as db:
            rows = db.execute(text("SELECT id, summary FROM owner_reviews WHERE reason=:r"), {"r": marker}).all()
            assert len(rows) == 1 and rows[0][1] == payload["summary"]
            db.execute(text("UPDATE owner_reviews SET status='revision_requested', reviewer_note='owner hold', "
                            "submitted_at='2020-01-01' WHERE reason=:r"), {"r": marker})
            db.commit()
        assert owner_reviews.task_recently_escalated(payload["task_id"])
        assert not owner_reviews.insert_sync(**{**payload, "summary": "new age"})
    finally:
        with factory() as db:
            db.execute(text("DELETE FROM owner_reviews WHERE reason=:r"), {"r": marker})
            db.commit()
        engine.dispose()


@pytest.mark.parametrize('valid_key',[False,True])
async def test_real_conversion_before_renotify_does_not_commit_legacy_event(env,monkeypatch,valid_key):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.title,t.status='single','agent','old worker','inbox'
    t.updated_at=utcnow()-timedelta(minutes=15)
    a=Agent(board_id=b.id,gateway_id=b.gateway_id,name='scratch worker',openclaw_session_id='agent:mc-scratch:main' if valid_key else None)
    s.add(a);await s.flush();t.assigned_agent_id=a.id;await s.commit()
    tid,bid=t.id,b.id
    original=s.execute
    async def syntax(stmt,*args,**kw):
        sql=str(stmt).replace("NOW() - INTERVAL '600 seconds'","datetime('now','-600 seconds')").replace('NOW()','CURRENT_TIMESTAMP')
        return await original(text(sql),*args,**kw)
    monkeypatch.setattr(s,'execute',syntax)
    converted=[];sent=[]
    from app.api import tasks as api
    async def noop(*args,**kw):pass
    async def config(*args,**kw):return {'synthetic':True}
    async def send(**kw):sent.append(kw);return None
    monkeypatch.setattr(api,'_wake_agent_online_for_task',noop)
    monkeypatch.setattr(api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(api,'_send_agent_task_message',send) # No network; records real notification caller reaching dispatch.
    async def count(*args,**kw):
        if not converted:
            async with AsyncSession(s.bind,expire_on_commit=False) as owner:
                current=await owner.get(Task,tid);board=await owner.get(Board,bid)
                # Real owner stop/unassign prerequisite before harness lane selection.
                from uuid import uuid4
                from app.api import tasks as api
                from app.api.deps import ActorContext
                from app.schemas.tasks import TaskUpdate
                from app.models.users import User
                from app.models.organization_members import OrganizationMember
                user=User(id=uuid4(),clerk_user_id='scratch-unassign',email='unassign@invalid',active_organization_id=board.organization_id)
                owner.add(user);await owner.flush()
                owner.add(OrganizationMember(organization_id=board.organization_id,user_id=user.id,role='owner',all_boards_write=True));await owner.commit()
                patch_sql(owner,monkeypatch)
                await api.update_task(TaskUpdate(assigned_agent_id=None),task=current,session=owner,actor=ActorContext(actor_type='user',user=user))
                converted.append(await convert(owner,board,current))
                assert (current.execution_mode,current.execution_lane,current.status)==('team','harness','in_progress')
        return 0
    async def age(*args,**kw):return None
    monkeypatch.setattr(watchdog_loop,'_count_comments_this_cycle',count)
    monkeypatch.setattr(watchdog_loop,'_seconds_since_comment_matching',age)
    monkeypatch.setattr(watchdog_loop,'_agent_recently_throttled',lambda *a:False)
    await watchdog_loop._recover_stuck_claimed_inbox(s)
    await s.refresh(t)
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    bad=[e for e in events if e.event_type in ('task.assignee_notify_failed','task.assignee_notified')]
    print('converted:',t.execution_mode,t.execution_lane,t.status,'durable legacy events:',[(e.event_type,e.message) for e in bad])
    assert converted and not bad and not sent

@pytest.mark.parametrize("lane,title", [("", "[codex] historic harness"), (None, "[agent] historic harness"), ("harness", "native")])
async def test_notification_shared_boundary_preserves_harness(env, monkeypatch, lane, title):
    s, board, task = env
    task.execution_mode, task.execution_lane, task.title = "single", lane, title
    agent = Agent(board_id=board.id, gateway_id=board.gateway_id, name="scratch worker")
    s.add(agent)
    await s.flush()
    task.assigned_agent_id = agent.id
    await s.commit()
    tid = task.id
    await task_api.notify_agent_on_task_assign(session=s, board=board, task=task, agent=agent)
    assert not list(await s.exec(select(ActivityEvent).where(
        ActivityEvent.task_id == tid,
        ActivityEvent.event_type.in_(["task.assignee_notify_failed", "task.assignee_notified"]),
    )))


@pytest.mark.parametrize('converted',[False,True])
async def test_post_commit_unassignment_notice_refuses_real_native_conversion(env,monkeypatch,converted):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.title,t.status='single','agent','old gateway assignment','inbox'
    lead=Agent(board_id=b.id,gateway_id=b.gateway_id,name='scratch lead',is_board_lead=True,openclaw_session_id='agent:lead-scratch:main')
    worker=Agent(board_id=b.id,gateway_id=b.gateway_id,name='old worker')
    s.add_all([lead,worker]);await s.flush();t.assigned_agent_id=worker.id
    user=User(clerk_user_id=str(uuid4()),email='unassign@invalid',active_organization_id=b.organization_id)
    s.add(user);await s.flush();s.add(OrganizationMember(organization_id=b.organization_id,user_id=user.id,role='owner',all_boards_write=True));await s.commit()
    tid,bid=t.id,b.id
    patch_sql(s,monkeypatch)
    captured=[]
    async def defer(session,*,update):captured.append(update)
    monkeypatch.setattr(task_api,'_notify_task_update_assignment_changes',defer)
    # Real authorized owner API unassignment commits, then defer only the notification
    # to interleave a second legitimate owner conversion before that real caller.
    await task_api.update_task(TaskUpdate(assigned_agent_id=None),task=t,session=s,actor=ActorContext(actor_type='user',user=user))
    assert len(captured)==1 and t.assigned_agent_id is None
    update=captured[0]
    if converted:
        async with AsyncSession(s.bind,expire_on_commit=False) as owner:
            current=await owner.get(Task,tid);board=await owner.get(Board,bid)
            patch_sql(owner,monkeypatch)
            cycle=await convert(owner,board,current)
            assert current.execution_mode=='team' and current.status=='in_progress' and cycle.state=='planning'
    assert t.execution_lane=='agent' and t.status=='inbox'
    # Restore REAL notification coordinator, which conversion fixture silences only to avoid network.
    # It is retained before patch_sql below by module-global original saved at import.
    sent=[]
    async def config(*args,**kw):return object()
    async def send(**kw):sent.append(kw);return None
    monkeypatch.setattr(task_api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(task_api,'_send_lead_task_message',send)
    await REAL_NOTIFY(s,update=update)
    await s.refresh(t)
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    bad=[e.event_type for e in events if e.event_type.startswith('task.lead_unassigned_')]
    print('actual current task',t.execution_mode,t.execution_lane,t.status,'durable events',bad,'intercepted dispatches',len(sent))
    if converted:
        assert t.execution_mode=='team' and t.status=='in_progress'
        assert not sent and not bad, 'Legacy lead dispatch/bookkeeping survived real native conversion'
    else:
        assert len(sent)==1 and bad==['task.lead_unassigned_notified']

REAL_NOTIFY=task_api._notify_task_update_assignment_changes


@pytest.mark.parametrize('change',['none','pause','goal'])
async def test_actual_stage1_does_not_report_wake_after_authority_lost(env,monkeypatch,change):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.title,t.status='single','agent','real stage1','inbox'
    t.updated_at=utcnow()-timedelta(minutes=15)
    a=Agent(board_id=b.id,gateway_id=b.gateway_id,name='worker',openclaw_session_id='agent:mc-scratch:main')
    s.add(a);await s.flush();t.assigned_agent_id=a.id
    u=User(clerk_user_id=str(uuid4()),email='owner@invalid',active_organization_id=b.organization_id)
    s.add(u);await s.flush();s.add(OrganizationMember(organization_id=b.organization_id,user_id=u.id,role='owner',all_boards_write=True));await s.commit()
    tid,uid=t.id,u.id
    original=s.execute
    async def syntax(stmt,*args,**kw):
        return await original(text(str(stmt).replace("NOW() - INTERVAL '600 seconds'","datetime('now','-600 seconds')").replace('NOW()','CURRENT_TIMESTAMP')),*args,**kw)
    monkeypatch.setattr(s,'execute',syntax)
    async def zero(*args,**kw):return 0
    async def age(*args,**kw):return None
    # Fix timezone-dependent age routing to Stage1 only; preserve real notifier/effects.
    monkeypatch.setattr(watchdog_loop,'_STUCK_INBOX_REASSIGN_SECONDS',999999)
    monkeypatch.setattr(watchdog_loop,'_count_comments_this_cycle',zero)
    monkeypatch.setattr(watchdog_loop,'_seconds_since_comment_matching',age)
    monkeypatch.setattr(watchdog_loop,'_agent_recently_throttled',lambda *args:False)
    sent=[]
    async def config(*args,**kw):return object()
    async def send(**kw):sent.append(kw);return None
    monkeypatch.setattr(task_api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(task_api,'_send_agent_task_message',send)
    heartbeat=task_api.AgentLifecycleService.commit_heartbeat
    changes=[]
    async def interleave(service,**kw):
        out=await heartbeat(service,**kw)
        if change!='none':
            async with AsyncSession(s.bind,expire_on_commit=False) as owner:
                current=await owner.get(Task,tid);user=await owner.get(User,uid)
                patch_sql(owner,monkeypatch)
                await task_api.update_task(TaskUpdate(**({'status':'paused'} if change=='pause' else {'description':'NEW OWNER GOAL'})),task=current,session=owner,actor=ActorContext(actor_type='user',user=user))
            changes.append(True)
        return out
    monkeypatch.setattr(task_api.AgentLifecycleService,'commit_heartbeat',interleave)
    actions=await watchdog_loop._recover_stuck_claimed_inbox(s)
    await s.refresh(t)
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    stale=[(e.event_type,e.message) for e in events if e.event_type.startswith('task.assignee_') or (e.event_type=='task.comment' and 'Stuck-inbox re-notify' in (e.message or ''))]
    print('current',t.status,t.description,'actions',actions,'stale events',stale)
    if change=='none':
        assert len(sent)==1 and actions==1
        assert [e[0] for e in stale]==['task.assignee_woken','task.assignee_notified','task.comment']
    else:
        assert changes and not sent
        assert not stale, 'Real Stage1 claimed Wake message re-sent after original generation was refused'


@pytest.mark.parametrize('failure', ['missing_key', 'malformed_key', 'no_config', 'send_error'])
async def test_failed_assignment_cannot_claim_wake_message_resent(env,monkeypatch,failure):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.status='single','agent','inbox'
    t.title='agent with undeliverable assignment';t.updated_at=utcnow()-timedelta(minutes=15)
    key = None if failure == 'missing_key' else ('agent:broken' if failure == 'malformed_key' else 'agent:mc-scratch:main')
    agent=Agent(board_id=b.id,gateway_id=b.gateway_id,name='worker',openclaw_session_id=key)
    s.add(agent);await s.flush();t.assigned_agent_id=agent.id;await s.commit();tid=t.id
    original=s.execute
    async def syntax(stmt,*args,**kw):return await original(text(str(stmt).replace("NOW() - INTERVAL '600 seconds'","datetime('now','-600 seconds')").replace('NOW()','CURRENT_TIMESTAMP')),*args,**kw)
    monkeypatch.setattr(s,'execute',syntax)
    async def zero(*args,**kw):return 0
    async def age(*args,**kw):return None
    monkeypatch.setattr(watchdog_loop,'_STUCK_INBOX_REASSIGN_SECONDS',999999)
    monkeypatch.setattr(watchdog_loop,'_count_comments_this_cycle',zero)
    monkeypatch.setattr(watchdog_loop,'_seconds_since_comment_matching',age)
    monkeypatch.setattr(watchdog_loop,'_agent_recently_throttled',lambda *args:False)
    # Preserve real heartbeat/notifier/commits; intercept transport only.
    from app.api import tasks as api
    sent=[]
    async def config(*args,**kw):return None if failure=='no_config' else object()
    async def send(**kw):sent.append(kw);return 'synthetic transport refusal'
    monkeypatch.setattr(api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(api,'_send_agent_task_message',send)
    actions=await watchdog_loop._recover_stuck_claimed_inbox(s)
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    assert len(sent)==int(failure=='send_error')
    assert [e.event_type for e in events if e.event_type.startswith('task.assignee_') and e.event_type!='task.assignee_woken']==['task.assignee_notify_failed']
    success=[e.message for e in events if e.event_type=='task.comment' and 'Wake message re-sent' in (e.message or '')]
    print('durable events',[(e.event_type,e.message) for e in events],'actions',actions)
    assert not success, 'Actual Stage1 persisted success marker although real notifier recorded failed delivery'
    assert actions == 0
    assert await watchdog_loop._recover_stuck_claimed_inbox(s) == 0
    failures = list(await s.exec(select(ActivityEvent).where(
        ActivityEvent.task_id == tid,
        ActivityEvent.event_type == 'task.assignee_notify_failed',
    )))
    assert len(failures) == 1  # Real failure cooldown, not a fake successful nudge.


@pytest.mark.parametrize('window', ['heartbeat', 'protocol'])
@pytest.mark.parametrize('change',['none','goal','pause'])
async def test_real_consensus_fanout_does_not_recapture_refused_generation(env,monkeypatch,caplog,change,window):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.title,t.status='single','agent','crackme static analysis','inbox'
    t.assigned_agent_id=None;b.autonomous_enabled=True
    agents=[Agent(board_id=b.id,gateway_id=b.gateway_id,name=n,status='online',openclaw_session_id=f'agent:scratch-{i}:main') for i,n in enumerate(watchdog_loop._RE_CONSENSUS_POOL[:2])]
    u=User(clerk_user_id=str(uuid4()),email='owner@invalid',active_organization_id=b.organization_id)
    s.add_all([*agents,u]);await s.flush();s.add(OrganizationMember(organization_id=b.organization_id,user_id=u.id,role='owner',all_boards_write=True));await s.commit()
    tid,uid=t.id,u.id
    s.expunge(t) # Real watchdog begins from raw SQL scan, not a preloaded task identity.
    original=s.execute
    async def syntax(stmt,*args,**kw):return await original(text(str(stmt).replace('NOW()','CURRENT_TIMESTAMP')),*args,**kw)
    monkeypatch.setattr(s,'execute',syntax)
    sent=[];changes=[]
    async def config(*args,**kw):return object()
    async def send(**kw):sent.append(kw);return None
    monkeypatch.setattr(task_api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(task_api,'_send_agent_task_message',send)
    async def owner_edit():
        if change!='none' and not changes:
            async with AsyncSession(s.bind,expire_on_commit=False) as owner:
                task=await owner.get(Task,tid);user=await owner.get(User,uid)
                patch_sql(owner,monkeypatch)
                await task_api.update_task(TaskUpdate(**({'description':'NEW OWNER GOAL'} if change=='goal' else {'status':'paused'})),task=task,session=owner,actor=ActorContext(actor_type='user',user=user))
            changes.append(True)
    if window == 'heartbeat':
        heartbeat=task_api.AgentLifecycleService.commit_heartbeat
        async def interleave(service,**kw):
            result=await heartbeat(service,**kw)
            await owner_edit()
            return result
        monkeypatch.setattr(task_api.AgentLifecycleService,'commit_heartbeat',interleave)
    else:
        commit=s.commit
        async def interleave_protocol():
            await commit()
            await owner_edit()
        monkeypatch.setattr(s,'commit',interleave_protocol)
    count=await watchdog_loop._solicit_consensus_for_tool_tasks(s)
    current=await s.get(Task,tid);await s.refresh(current)
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    notifications=[e.event_type for e in events if e.event_type.startswith('task.assignee_')]
    print('current',current.status,current.description,'solicited',count,'sends',len(sent),'notifications',notifications)
    if change=='none':assert len(sent)==2 and notifications==['task.assignee_woken','task.assignee_notified']*2
    else:assert changes and not sent and not notifications, 'Sibling analyst recaptured new owner generation after refusal hydration'
    assert not [r for r in caplog.records if 'consensus_solicit.wake_failed' in r.message]


async def owner_user(s,b):
    u=User(clerk_user_id=str(uuid4()),email='audit@invalid',active_organization_id=b.organization_id)
    s.add(u);await s.flush();s.add(OrganizationMember(organization_id=b.organization_id,user_id=u.id,role='owner',all_boards_write=True));await s.commit();return u

def intercept(monkeypatch):
    sent=[]
    async def config(*args,**kw):return object()
    async def send(**kw):sent.append(kw);return None
    monkeypatch.setattr(task_api.GatewayDispatchService,'optional_gateway_config_for_board',config)
    monkeypatch.setattr(task_api,'_send_agent_task_message',send)
    return sent

async def edit_goal(s,tid,uid,monkeypatch):
    async with AsyncSession(s.bind,expire_on_commit=False) as owner:
        t=await owner.get(Task,tid);u=await owner.get(User,uid)
        async def no_notify(*args,**kw):pass # Only the new owner's own notice is intercepted.
        with monkeypatch.context() as local:
            local.setattr(task_api,'_notify_task_update_assignment_changes',no_notify)
            await task_api.update_task(TaskUpdate(description='NEW OWNER GOAL'),task=t,session=owner,actor=ActorContext(actor_type='user',user=u))
        assert t.description=='NEW OWNER GOAL'

@pytest.mark.parametrize('change',['none','goal'])
async def test_owner_update_cannot_adopt_post_assignment_commit_goal(env,monkeypatch,change):
    s,b,t=env
    t.execution_mode,t.execution_lane,t.status,t.title='single','agent','inbox','assignment sibling'
    t.description='ORIGINAL GOAL';t.assigned_agent_id=None
    a=Agent(board_id=b.id,gateway_id=b.gateway_id,name='worker',openclaw_session_id='agent:audit-worker:main')
    s.add(a);await s.commit();u=await owner_user(s,b);tid,uid,aid=t.id,u.id,a.id
    sent=intercept(monkeypatch);changed=[];commit=s.commit
    async def interleave():
        await commit()
        if change=='goal' and not changed:
            changed.append(True)
            await edit_goal(s,tid,uid,monkeypatch)
    monkeypatch.setattr(s,'commit',interleave)
    await task_api.update_task(TaskUpdate(assigned_agent_id=aid),task=t,session=s,actor=ActorContext(actor_type='user',user=u))
    ev=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
    effects=[e.event_type for e in ev if e.event_type.startswith('task.assignee_')]
    print('owner update sibling',change,'sent',len(sent),'events',effects,'messages',[x['message'] for x in sent])
    if change=='none':assert len(sent)==1 and effects==['task.assignee_woken','task.assignee_notified']
    else:assert changed and not sent and not effects,'Post-commit refresh adopted owner goal as notification authority'


@pytest.mark.parametrize('change', ['none', 'goal'])
async def test_agent_create_refusal_returns_without_expired_sibling_read(env, monkeypatch, change):
    session, board, _ = env
    lead = Agent(board_id=board.id, gateway_id=board.gateway_id, name='lead', is_board_lead=True, openclaw_session_id='agent:audit-lead:main')
    pm = Agent(board_id=board.id, gateway_id=board.gateway_id, name='pm', is_project_manager=True, openclaw_session_id='agent:audit-pm:main')
    worker = Agent(board_id=board.id, gateway_id=board.gateway_id, name='worker', openclaw_session_id='agent:audit-worker:main')
    user = User(clerk_user_id=str(uuid4()), email='audit@invalid', active_organization_id=board.organization_id)
    session.add_all([lead, pm, worker, user])
    await session.flush()
    session.add(OrganizationMember(organization_id=board.organization_id, user_id=user.id, role='owner', all_boards_write=True))
    await session.commit()
    uid = user.id
    payload = TaskCreate(title='response sibling', description='ORIGINAL GOAL', complexity=1, execution_lane='agent', assigned_agent_id=worker.id)
    sent, captured = [], []
    async def config(*args, **kwargs): return object()
    async def send(**kwargs): sent.append(kwargs); return None
    monkeypatch.setattr(task_api.GatewayDispatchService, 'optional_gateway_config_for_board', config)
    monkeypatch.setattr(task_api, '_send_agent_task_message', send)
    heartbeat = task_api.AgentLifecycleService.commit_heartbeat
    async def interleave(service, **kwargs):
        result = await heartbeat(service, **kwargs)
        if change == 'goal' and not captured:
            created = (await session.exec(select(Task).where(Task.title == payload.title))).one()
            captured.append(created.id)
            async with AsyncSession(session.bind, expire_on_commit=False) as owner:
                current = await owner.get(Task, captured[0])
                actor = await owner.get(User, uid)
                async def no_owner_notice(*args, **kwargs): pass
                # Only the intervening owner's own notification is intercepted.
                with monkeypatch.context() as local:
                    local.setattr(task_api, '_notify_task_update_assignment_changes', no_owner_notice)
                    await task_api.update_task(TaskUpdate(description='NEW OWNER GOAL'), task=current, session=owner, actor=ActorContext(actor_type='user', user=actor))
        return result
    monkeypatch.setattr(task_api.AgentLifecycleService, 'commit_heartbeat', interleave)
    error = None
    try:
        await agent_api.create_task(payload, board=board, session=session, agent_ctx=AgentAuthContext(actor_type='agent', agent=lead))
    except Exception as exc:
        error = exc
    # Independent read session and captured UUID; no implicit expired fixture read.
    async with AsyncSession(session.bind, expire_on_commit=False) as observer:
        current = (await observer.exec(select(Task).where(Task.title == payload.title))).one()
        events = list(await observer.exec(select(ActivityEvent).where(ActivityEvent.task_id == current.id)))
        effects = [event.event_type for event in events if event.event_type.startswith('task.assignee_')]
        print('change', change, 'error', type(error).__name__ if error else None, 'durable_goal', current.description, 'deliveries', len(sent), 'effects', effects)
    assert error is None, f'Real agent-create caller failed after refusal: {type(error).__name__}: {error}'
    if change == 'goal':
        assert captured and not sent and not effects
    else:
        assert len(sent) == 1 and effects.count('task.assignee_woken') == 3 and effects.count('task.assignee_notified') == 1


@pytest.mark.parametrize('window', ['commit', 'background'])
@pytest.mark.parametrize('change', ['none', 'goal', 'pause'])
async def test_human_create_keeps_original_commit_and_background_generation(env, monkeypatch, window, change):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.db import session as db_session
    s, b, _ = env
    lead = Agent(board_id=b.id, gateway_id=b.gateway_id, name='lead', is_board_lead=True, openclaw_session_id='agent:audit-lead:main')
    worker = Agent(board_id=b.id, gateway_id=b.gateway_id, name='worker', openclaw_session_id='agent:audit-worker:main')
    s.add_all([lead, worker]); await s.commit()
    user = await owner_user(s, b); uid, bid, aid = user.id, b.id, worker.id
    sent = intercept(monkeypatch); lead_sent = []; pending = []; changed = []
    async def send_lead(**kw): lead_sent.append(kw); return None
    monkeypatch.setattr(task_api, '_send_lead_task_message', send_lead)
    original_create = asyncio.create_task
    def capture(coro, *args, **kw):
        if coro.cr_code.co_name == '_bg_notify':
            pending.append(coro); return None
        return original_create(coro, *args, **kw)
    monkeypatch.setattr(asyncio, 'create_task', capture)
    @asynccontextmanager
    async def maker():
        async with AsyncSession(s.bind, expire_on_commit=False) as session:
            yield session
    monkeypatch.setattr(db_session, 'async_session_maker', maker)
    async def edit(tid):
        async with AsyncSession(s.bind, expire_on_commit=False) as owner:
            current = await owner.get(Task, tid); actor = await owner.get(User, uid)
            async def no_owner_notice(*args, **kw): pass
            with monkeypatch.context() as local:
                local.setattr(task_api, '_notify_task_update_assignment_changes', no_owner_notice)
                await task_api.update_task(TaskUpdate(**({'description': 'NEW OWNER GOAL'} if change == 'goal' else {'status': 'paused'})), task=current, session=owner, actor=ActorContext(actor_type='user', user=actor))
        changed.append(True)
    commit = s.commit
    async def interleave():
        await commit()
        if window == 'commit' and change != 'none' and not changed:
            created = (await s.exec(select(Task).where(Task.title == 'human original generation'))).one()
            await edit(created.id)
    monkeypatch.setattr(s, 'commit', interleave)
    try:
        result = await task_api.create_task(TaskCreate(title='human original generation', description='ORIGINAL GOAL', complexity=1, execution_lane='agent', assigned_agent_id=aid), board=b, session=s, auth=SimpleNamespace(user=user))
        first_deliveries = len(sent)
        if window == 'background' and change != 'none':
            await edit(result.id)
        for coro in pending:
            await coro
        async with AsyncSession(s.bind, expire_on_commit=False) as observer:
            events = list(await observer.exec(select(ActivityEvent).where(ActivityEvent.task_id == result.id)))
        assert len([e for e in events if e.event_type == 'task.created']) == 1
        if change == 'none':
            assert len(sent) == 1 and len(lead_sent) == 1
        elif window == 'commit':
            assert changed and not sent and not lead_sent
            assert not [e for e in events if e.event_type.startswith('task.assignee_')]
        else:
            assert changed and first_deliveries == 1 and len(sent) == 1 and not lead_sent
    finally:
        for coro in pending:
            coro.close()


@pytest.mark.parametrize('flow',['rotation','freeform','triage'])
@pytest.mark.parametrize('change',['none','goal','pause'])
async def test_legacy_assignment_siblings_keep_own_epoch(env,monkeypatch,change,flow):
 import re
 s,b,t=env
 t.execution_mode,t.execution_lane,t.status,t.title='single','agent','inbox','rotation caller'
 t.description='ORIGINAL GOAL';t.updated_at=utcnow()-timedelta(hours=4)
 t.created_at=t.updated_at;t.complexity=3
 if flow=='freeform':t.status='in_progress';t.in_progress_at=t.updated_at
 old=Agent(board_id=b.id,gateway_id=b.gateway_id,name='QA Engineer Senior',is_board_lead=True,status='online',openclaw_session_id='agent:audit-old:main')
 new=Agent(board_id=b.id,gateway_id=b.gateway_id,name='QA Engineer',status='online',openclaw_session_id='agent:audit-new:main')
 u=User(clerk_user_id=str(uuid4()),email='rotation@invalid',active_organization_id=b.organization_id)
 s.add_all([old,new,u]);await s.flush();t.assigned_agent_id=old.id if flow!='triage' else None
 if flow=='freeform':s.add(ActivityEvent(board_id=b.id,task_id=t.id,event_type='task.comment',agent_id=old.id,message='COMPLETED: synthetic fixture answer',created_at=utcnow()))
 s.add(OrganizationMember(organization_id=b.organization_id,user_id=u.id,role='owner',all_boards_write=True));await s.commit()
 tid,uid,nid=t.id,u.id,new.id
 await s.execute(text('CREATE TABLE IF NOT EXISTS provider_capacity(provider_name TEXT,max_concurrent INTEGER,is_unlimited BOOLEAN,status TEXT)'))
 await s.execute(text('CREATE TABLE IF NOT EXISTS provider_benchmark(provider_name TEXT,tps REAL,ts TIMESTAMP,ok BOOLEAN)'));await s.commit()
 s.expunge(t) # Production raw scan does not preload a Task identity.
 original=s.execute
 async def sql(stmt,*a,**kw):
  v=str(stmt)
  v=re.sub(r"NOW\(\) - INTERVAL '(\d+) seconds'",r"datetime('now','-\1 seconds')",v)
  v=re.sub(r"NOW\(\) - INTERVAL '(\d+) minutes'",r"datetime('now','-\1 minutes')",v)
  v=v.replace("TIMESTAMP '1970-01-01'","'1970-01-01'").replace('NOW()','CURRENT_TIMESTAMP')
  v=v.replace(' ILIKE ',' LIKE ').replace(' ~* ',' REGEXP ').replace('::timestamp','')
  v=v.replace("CURRENT_TIMESTAMP\n                        - (COALESCE(t.repeat_interval_minutes, 60) || ' minutes')::interval", "datetime('now','-' || COALESCE(t.repeat_interval_minutes,60) || ' minutes')")
  return await original(text(v),*a,**kw)
 monkeypatch.setattr(s,'execute',sql)
 async def provider_config():return {} # Synthetic configuration: no provider/network access.
 monkeypatch.setattr(watchdog_loop,'_agent_provider_map',provider_config)
 sent=[]
 async def config(*a,**kw):return object()
 async def send(**kw):sent.append(kw);return None
 monkeypatch.setattr(task_api.GatewayDispatchService,'optional_gateway_config_for_board',config)
 monkeypatch.setattr(task_api,'_send_agent_task_message',send)
 commit=s.commit;changed=[]
 async def interleave():
  await commit()
  if change!='none' and not changed:
   changed.append(True)
   async with AsyncSession(s.bind,expire_on_commit=False) as owner:
    current=await owner.get(Task,tid);user=await owner.get(User,uid)
    # Owner goal edit uses real API/guards/notification coordinator too.
    await task_api.update_task(TaskUpdate(**({'description':'NEW OWNER GOAL'} if change=='goal' else {'status':'paused'})),task=current,session=owner,actor=ActorContext(actor_type='user',user=user))
 monkeypatch.setattr(s,'commit',interleave)
 actions=await {'rotation':watchdog_loop._recover_stuck_claimed_inbox,'freeform':watchdog_loop._detect_freeform_completion,'triage':watchdog_loop._route_manual_inbox_tasks}[flow](s)
 assert actions==1
 async with AsyncSession(s.bind,expire_on_commit=False) as observer:
  current=await observer.get(Task,tid)
  events=list(await observer.exec(select(ActivityEvent).where(ActivityEvent.task_id==tid)))
  notify=[e.event_type for e in events if e.event_type.startswith('task.assignee_')]
  print('change',change,'actions',actions,'goal',current.description,'assigned',current.assigned_agent_id,'sends',len(sent),'notification_events',notify)
  if change=='pause':assert current.status=='paused' and current.assigned_agent_id is None
  else:assert current.assigned_agent_id==nid
 if change=='none':assert len(sent)==1 and notify==['task.assignee_woken','task.assignee_notified']
 else:assert changed and not sent and not notify,'Legacy sibling adopted changed owner epoch after commit'
