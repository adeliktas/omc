import json,re
from uuid import UUID,uuid4
import pytest
from sqlalchemy import text
from sqlmodel import select
from app.services import pev_loop as pev, team_cycle as svc
from app.api import tasks as api
from app.api.deps import ActorContext
from app.schemas.tasks import TaskUpdate
from app.models.users import User
from app.models.organization_members import OrganizationMember
from app.models.approvals import Approval
from app.models.activity_events import ActivityEvent
from tests.test_team_backlog import env,RANKING,BASE

@pytest.mark.parametrize('phase',['planner','executor','verifier'])
@pytest.mark.parametrize('native',[True,False])
async def test_live_pev_verdict_cannot_close_converted_native_cycle(env,monkeypatch,phase,native):
 s,board,task=env
 task.title='legacy non-tool task';task.execution_mode='single';task.execution_lane='agent';board.autonomous_enabled=True
 task.description='OLD LEGACY GOAL'
 user=User(id=uuid4(),clerk_user_id='independent-owner',email='synthetic@invalid',active_organization_id=board.organization_id)
 s.add(user);await s.flush()
 s.add(OrganizationMember(organization_id=board.organization_id,user_id=user.id,role='owner',all_boards_write=True))
 await s.execute(text('CREATE TABLE task_pev_state (task_id TEXT PRIMARY KEY,status TEXT,plan TEXT,step_index INTEGER,step_retries INTEGER,updated_at TEXT,last_verdict_pass BOOLEAN,last_verdict_reason TEXT,verifier_model TEXT,executor_model TEXT,planner_model TEXT,total_calls_planner INTEGER,total_calls_executor INTEGER,total_calls_verifier INTEGER)'))
 await s.execute(text('INSERT INTO task_pev_state (task_id,status,plan,step_index,step_retries,total_calls_executor,total_calls_verifier) VALUES (:tid,:status,:plan,0,0,0,0)'),{'tid':task.id.hex,'status':pev.STATUS_PLANNING if phase=='planner' else pev.STATUS_EXECUTING,'plan':json.dumps([{'description':'old scope check','verification':'old goal verified'}])})
 await s.commit()
 original=s.execute
 async def syntax(stmt,params=None,*a,**kw):
  sql=str(stmt).replace('NOW()','CURRENT_TIMESTAMP').replace('CAST(:plan AS jsonb)',':plan')
  params=dict(params or {})
  for k,v in params.items():
   if isinstance(v,str):
    try: params[k]=UUID(v).hex
    except ValueError:pass
  return await original(text(sql),params,*a,**kw)
 monkeypatch.setattr(s,'execute',syntax)
 original_get=pev._get_state
 async def get_state(*args):
  state=await original_get(*args)
  if state and isinstance(state.get('plan'),str):state['plan']=json.loads(state['plan'])
  return state
 monkeypatch.setattr(pev,'_get_state',get_state)
 async def noop(*a,**kw):pass
 monkeypatch.setattr(api,'_notify_task_update_assignment_changes',noop)
 async def pick(db,tier):return ('synthetic','synthetic-'+tier,[])
 monkeypatch.setattr(pev,'pick_model_for_tier',pick)
 cycle=None;calls=[];edited=False;before_events=[]
 async def model(model_id,*a,**kw):
  nonlocal cycle,edited,before_events
  calls.append(model_id)
  response={'steps':[{'description':'old scope check','verification':'old goal verified'}]} if model_id.endswith('planner') else ({'action':'old goal checked','evidence':'synthetic execution evidence','self_assessment':'pass'} if model_id.endswith('executor') else {'pass':True,'reason':'the OLD legacy result meets OLD criteria'})
  if not model_id.endswith(phase):return json.dumps(response)
  before_events=[e.id for e in await s.exec(select(ActivityEvent))]
  await s.refresh(task);await s.refresh(board);await s.refresh(user)
  actor=ActorContext(actor_type='user',user=user)
  for payload in [TaskUpdate(status='inbox'),TaskUpdate(description='NEW EXPLICIT OWNER GOAL',**({'execution_lane':'harness','execution_mode':'team'} if native else {})),TaskUpdate(status='in_progress')]:
   await api.update_task(payload,task=task,session=s,actor=actor)
  edited=True
  if native:
   cycle,_=await svc.start_cycle(s,board,task,RANKING)
   await svc.claim_next(s,cycle,base_sha=BASE,ranking=RANKING);await s.commit()
  return json.dumps(response)
 monkeypatch.setattr(pev,'call_model',model)
 result=await pev.advance_pev(s,task.id)
 await s.refresh(task)
 if cycle is not None:await s.refresh(cycle)
 approvals=list(await s.exec(select(Approval).where(Approval.task_id==task.id)))
 events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==task.id)))
 print('LIVE PEV:',calls,result,task.execution_mode,task.status,cycle.state if cycle is not None else None,[(a.action_type,a.status) for a in approvals],[(e.message or '')[:120] for e in events if '[auto-pev]' in (e.message or '')])
 assert calls==(['synthetic-executor','synthetic-verifier'] if phase=='verifier' else ['synthetic-'+phase]) and edited
 assert not native or cycle is not None
 assert task.status=='in_progress' and task.description=='NEW EXPLICIT OWNER GOAL' and not approvals
 prior_ids=set(before_events)
 assert not [e for e in events if e.id not in prior_ids and '[pev-' in (e.message or '')], 'Stale PEV phase wrote markers for the newer owner goal'
