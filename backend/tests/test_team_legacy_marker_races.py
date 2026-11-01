import re
from uuid import uuid4
from sqlalchemy import text
from app.api import tasks as task_api
from app.api.deps import ActorContext
from app.schemas.tasks import TaskUpdate
from app.models.activity_events import ActivityEvent
from app.models.agents import Agent
from app.services import watchdog_loop as wl, team_cycle as svc, llm_evaluator
from tests.test_team_backlog import env, RANKING, BASE

async def convert(s,board,task):
    from app.models.users import User
    from app.models.organization_members import OrganizationMember
    user=User(id=uuid4(),clerk_user_id='scratch-owner',email='owner@invalid',active_organization_id=board.organization_id)
    s.add(user);await s.flush()
    s.add(OrganizationMember(organization_id=board.organization_id,user_id=user.id,role='owner',all_boards_write=True))
    await s.commit()
    actor=ActorContext(actor_type='user',user=user)
    for p in [TaskUpdate(status='inbox'),TaskUpdate(execution_mode='team',execution_lane='harness',description='NEW EXPLICIT OWNER GOAL'),TaskUpdate(status='in_progress')]:
        await task_api.update_task(p,task=task,session=s,actor=actor)
    cycle,_=await svc.start_cycle(s,board,task,RANKING)
    await svc.claim_next(s,cycle,base_sha=BASE,ranking=RANKING)
    await s.commit()
    return cycle

async def noop(*a,**kw): pass

def translate_sql(sql):
    if 'JOIN LATERAL (' in sql:
        start=sql.index('JOIN LATERAL (');end=sql.index(') oc ON TRUE',start)
        inner=sql[start+len('JOIN LATERAL ('):end]
        inner=inner.replace('SELECT message, created_at','SELECT task_id, message, created_at, ROW_NUMBER() OVER(PARTITION BY task_id ORDER BY created_at DESC) rn')
        inner=inner.replace('WHERE task_id = t.id AND','WHERE')
        inner=re.sub(r'ORDER BY created_at DESC\s+LIMIT 1\s*$','',inner)
        sql=sql[:start]+'JOIN ('+inner+') oc ON oc.task_id=t.id AND oc.rn=1'+sql[end+len(') oc ON TRUE'):]
    sql=re.sub(r"message !~ ('(?:[^']|'')*')",r'message NOT REGEXP \1',sql)
    return sql.replace("NOW() - INTERVAL '1 day'","datetime('now','-1 day')").replace("'1970-01-01'::timestamp","'1970-01-01'").replace('NOW()','CURRENT_TIMESTAMP')

def patch_sql(s,monkeypatch):
    original=s.execute
    async def syntax(stmt,*a,**kw):return await original(text(translate_sql(str(stmt))),*a,**kw)
    monkeypatch.setattr(s,'execute',syntax)
    monkeypatch.setattr(task_api,'_notify_task_update_assignment_changes',noop)

async def test_owner_directive_scan_cannot_rewrite_new_native_goal(env,monkeypatch):
    s,board,task=env
    task.execution_mode='single';task.execution_lane='agent';task.title='legacy';task.description='OLD GOAL'
    s.add(ActivityEvent(event_type='task.comment',task_id=task.id,board_id=board.id,message='Adjust the old goal'))
    await s.commit();patch_sql(s,monkeypatch)
    monkeypatch.setattr(wl,'_OWNER_DIRECTIVES_ENABLED',True)
    cycle=None
    async def evaluate(*args):
        nonlocal cycle
        cycle=await convert(s,board,task)
        return 'MODEL REWRITE OF OLD GOAL'
    monkeypatch.setattr(llm_evaluator,'evaluate_owner_directive',evaluate)
    monkeypatch.setattr(wl,'_post_comment',noop)
    applied=await wl._apply_owner_directives(s)
    await s.refresh(task)
    await s.refresh(cycle)
    print('owner directive after conversion:',applied,task.execution_mode,task.description,cycle.state)
    assert task.description=='NEW EXPLICIT OWNER GOAL'

async def test_consensus_commit_cannot_emit_markers_into_new_native_cycle(env,monkeypatch):
    s,board,task=env
    task.execution_mode='single';task.execution_lane='agent';task.title='legacy';task.complexity=4
    s.add(ActivityEvent(event_type='task.comment',task_id=task.id,board_id=board.id,message='[consensus-solicit] synthetic'))
    for i in range(2):
        agent=Agent(id=uuid4(),name=f'analyst-{i}',board_id=board.id,gateway_id=board.gateway_id)
        s.add(agent);await s.flush()
        s.add(ActivityEvent(event_type='task.comment',task_id=task.id,board_id=board.id,agent_id=agent.id,message='[ready-to-close] FINAL VERDICT: completed'))
    await s.commit();patch_sql(s,monkeypatch)
    cycle=None;emitted=[]
    original_post=wl._post_comment
    async def post(db,tid,bid,msg,**kwargs):
        nonlocal cycle
        if cycle is None:
            await s.refresh(task)
            assert task.status=='review'
            cycle=await convert(s,board,task)
        emitted.append(msg)
        return await original_post(db,tid,bid,msg,**kwargs)
    monkeypatch.setattr(wl,'_post_comment',post)
    monkeypatch.setattr(wl,'_seed_kb_from_consensus',noop)
    n=await wl._auto_emit_consensus_verdict(s)
    await s.refresh(task)
    from sqlmodel import select
    events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==task.id)))
    fabricated=[e.message for e in events if (e.message or '').startswith(('[qa-verdict]','[pm-signoff]'))]
    assert cycle is not None  # real authorized conversion did run
    assert emitted  # actual production helper, not a signature/setup failure
    print('consensus after conversion:',n,task.execution_mode,task.status,fabricated)
    assert not fabricated, 'legacy consensus fabricated QA/signoff markers on newly converted native task'
