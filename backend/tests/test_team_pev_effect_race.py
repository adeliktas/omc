from pathlib import Path
import pytest
from sqlalchemy import text
from sqlmodel import select
from app.services import watchdog_loop as wl,llm_evaluator,journal_writer
from app.models.activity_events import ActivityEvent
from app.models.approvals import Approval
from tests.test_team_backlog import env
from tests.test_team_legacy_marker_races import convert,patch_sql

class CheckedStepFinished(BaseException):pass
class Empty:
 rowcount=0
 def fetchall(self):return []
 def first(self):return None

async def test_real_watchdog_pev_reconciliation_cannot_fabricate_native_signoff(env,monkeypatch,tmp_path):
 s,board,task=env
 task.execution_mode='single';task.execution_lane='agent';task.title='legacy';task.status='review';board.autonomous_enabled=True
 await s.execute(text('CREATE TABLE task_pev_state (task_id TEXT, status TEXT, last_verdict_pass BOOLEAN, verifier_model TEXT)'))
 await s.execute(text("INSERT INTO task_pev_state VALUES (:tid,'done',true,'synthetic-verifier')"),{'tid':task.id.hex})
 await s.commit();patch_sql(s,monkeypatch)
 async def zero(*a,**kw):return 0
 async def jw(*a,**kw):return {}
 for name in ['_recover_offline_agents','_auto_reprovision_session_dropped','_provider_live_usage','_refresh_stale_souls','_ensure_lead_election_ballot','_ensure_capacity_owner_review','_trigger_test_run','_autorecover_providers','_gate_failing_providers','_sync_vllm_omniroute_combo','_route_manual_inbox_tasks','_recover_stuck_claimed_inbox','_auto_promote_inbox_on_assignee_activity','_apply_owner_directives','_reconcile_expected_artifacts','_flag_unbudgeted_long_runners','_detect_freeform_completion','_detect_llm_completion','_detect_split_parent_completion','_detect_split_parent_majority_completion','_reassign_stuck_subtasks','_materialize_dag_from_pm_plan','_aggregate_token_usage','_enforce_task_budgets','_propagate_dag_cancellations','_reconcile_openclaw_session_status','_detect_owner_questions']:
  monkeypatch.setattr(wl,name,zero)
 async def empty_list(*a,**kw):return []
 monkeypatch.setattr(wl,'_provider_live_usage',jw)
 monkeypatch.setattr(wl,'_reconcile_openclaw_session_status',jw)
 monkeypatch.setattr(wl,'_gate_failing_providers',empty_list)
 monkeypatch.setattr(wl,'_gateway_alive',lambda:True)
 monkeypatch.setattr(wl,'_write_gateway_heartbeat',lambda *a:None)
 monkeypatch.setattr(wl,'_cleanup_harness_items',lambda:0)
 monkeypatch.setattr(journal_writer,'tick',jw);monkeypatch.setattr(journal_writer,'write_daily_summary',zero)
 monkeypatch.setattr(llm_evaluator,'get_or_semaphore_strikes',lambda:[])
 monkeypatch.setenv('OPENCLAW_HOME',str(tmp_path/'empty-openclaw'))
 monkeypatch.setenv('TRAFFIC_DB_FILE',str(tmp_path/'nonexistent.sqlite'))
 monkeypatch.setattr(wl,'_OPENCLAW_HOME',tmp_path/'empty-openclaw')
 cycle=None;effects=False
 class Proxy:
  def __getattr__(self,name):return getattr(s,name)
  async def execute(self,stmt,*args,**kw):
   nonlocal cycle,effects
   sql=str(stmt)
   if 'SELECT t.id, t.board_id, s.verifier_model' in sql:
    result=await s.execute(stmt,*args,**kw)
    assert result.rowcount==-1
    rows=result.fetchall()
    if len(rows)!=1:raise CheckedStepFinished('setup failed: real PEV selection absent')
    cycle=await convert(s,board,task)
    class Rows:
     def fetchall(self):return rows
    effects=True
    return Rows()
   if effects:return await s.execute(stmt,*args,**kw)
   return Empty()
  async def commit(self):
   if effects:
    await s.commit()
    raise CheckedStepFinished() # stop the real watchdog before any later unrelated phase
  async def rollback(self):
   await s.rollback()
   if effects:raise CheckedStepFinished('checked step rolled back; stop unrelated phases')
 class Context:
  async def __aenter__(self):return Proxy()
  async def __aexit__(self,*a):return False
 monkeypatch.setattr(wl,'async_session_maker',Context)
 with pytest.raises(CheckedStepFinished):await wl._process_once()
 await s.refresh(task)
 approvals=list(await s.exec(select(Approval).where(Approval.task_id==task.id)))
 events=list(await s.exec(select(ActivityEvent).where(ActivityEvent.task_id==task.id)))
 fabricated=[e.message for e in events if '[auto-pev-reconcile]' in (e.message or '')]
 print('real watchdog PEV race',task.execution_mode,task.status,[(a.action_type,a.status) for a in approvals],fabricated)
 assert cycle is not None
 assert not approvals and not fabricated,'stale legacy PEV reconciliation inserted approved signoff/markers into a new native cycle'
