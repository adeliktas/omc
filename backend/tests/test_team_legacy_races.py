import re
import pytest
from sqlalchemy import text
from app.api import tasks as task_api
from app.api.deps import ActorContext
from app.schemas.tasks import TaskUpdate
from app.models.activity_events import ActivityEvent
from app.services import watchdog_loop as wl, team_cycle as svc
from app.core.time import utcnow
from tests.test_team_backlog import env, RANKING, BASE

async def test_freeform_stale_scan_cannot_close_new_native_cycle(env, monkeypatch):
    s,board,task=env
    task.title='normal task';task.execution_mode='single';task.execution_lane='agent';task.complexity=1;task.in_progress_at=utcnow()
    await s.commit()
    s.add(ActivityEvent(event_type='task.comment',task_id=task.id,board_id=board.id,message='COMPLETED: old single-lane work'))
    await s.commit()
    original=s.execute
    async def syntax(stmt,*args,**kwargs):
        sql=str(stmt).replace(' ILIKE ',' LIKE ').replace('NOW()','CURRENT_TIMESTAMP')
        sql=re.sub(r"ae\.message ~\* ('(?:[^']|'')*')",r'ae.message REGEXP \1',sql)
        return await original(text(sql),*args,**kwargs)
    monkeypatch.setattr(s,'execute',syntax)
    async def noop(*a,**kw): pass
    monkeypatch.setattr(task_api,'_notify_task_update_assignment_changes',noop)
    cycle=None
    async def interleave(*args,**kwargs):
        nonlocal cycle
        from uuid import uuid4
        from app.models.users import User
        from app.models.organization_members import OrganizationMember
        user=User(id=uuid4(),clerk_user_id='scratch-owner',email='owner@invalid',active_organization_id=board.organization_id)
        s.add(user);await s.flush()
        s.add(OrganizationMember(organization_id=board.organization_id,user_id=user.id,role='owner',all_boards_write=True))
        await s.commit()
        actor=ActorContext(actor_type='user',user=user)
        # The actual owner update handler, not direct row tampering. All
        # operations are on the disposable test session only.
        for payload in [TaskUpdate(status='inbox',expected_status='in_progress'),TaskUpdate(execution_mode='team',execution_lane='harness',description='NEW OWNER NATIVE GOAL'),TaskUpdate(status='in_progress',expected_status='inbox')]:
            await task_api.update_task(payload,task=task,session=s,actor=actor)
        cycle,_=await svc.start_cycle(s,board,task,RANKING)
        await svc.claim_next(s,cycle,base_sha=BASE,ranking=RANKING)
        await s.commit()
        assert task.status=='in_progress' and cycle.state=='planning'
        return []
    monkeypatch.setattr(wl,'_task_markers',interleave)
    monkeypatch.setattr(wl,'_post_comment',noop)
    promoted=await wl._detect_freeform_completion(s)
    await s.refresh(task)
    await s.refresh(cycle)
    print('actual owner conversion then legacy result:',promoted,task.execution_mode,task.status,cycle.state)
    assert task.status=='in_progress', 'stale legacy completion closed a freshly started native task'
