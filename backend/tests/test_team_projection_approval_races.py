import importlib.util,json,subprocess,sys,threading,re
from pathlib import Path
import pytest
from sqlalchemy import text
from app.models.activity_events import ActivityEvent
from app.models.approvals import Approval
from app.services import watchdog_loop as wl,owner_reviews
from tests.test_team_backlog import env
from tests.test_team_legacy_marker_races import convert,patch_sql

ROOT=Path(__file__).resolve().parents[2]
def runner():
 spec=importlib.util.spec_from_file_location('independent_extra_runner',ROOT/'scripts/mc-team-runner.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('private_alias',['.env.synthetic','backend/scripts/ollama_monitor.py'])
def test_masked_symlink_target_must_not_receive_exact_sha_pass(tmp_path,monkeypatch,private_alias):
 r=runner();repo=tmp_path/'repo';repo.mkdir()
 def git(*args):return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*args],text=True).strip()
 git('init','-q','-b','main');git('config','user.name','synthetic');git('config','user.email','synthetic@invalid')
 (repo/'public.txt').write_text('ORIGINAL_REQUIRED_UNCHANGED_BYTES\n'+'unchanged\n'*100+'old\n')
 alias=repo/private_alias;alias.parent.mkdir(parents=True,exist_ok=True);alias.symlink_to(Path(__import__('os').path.relpath(repo/'public.txt',alias.parent)))
 git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
 (repo/'public.txt').write_text((repo/'public.txt').read_text().replace('old\n','new\n'))
 git('add','.');git('commit','-qm','integration');sha=git('rev-parse','HEAD')
 monkeypatch.setattr(r,'REPO',repo);monkeypatch.setattr(r,'WORKDIR',str(tmp_path/'work'))
 calls=[]
 def qa(claim,prompt,cwd,scratch,*args):
  diff=(scratch/'qa-changes.diff').read_text();assert 'ORIGINAL_REQUIRED_UNCHANGED_BYTES' not in diff
  command=r.sandbox_command([sys.executable,'-c',"from pathlib import Path\ntry:\n data=Path('public.txt').read_bytes()\nexcept OSError:\n data=b''\nassert data==b''\nprint('REQUIRED_PUBLIC_BYTES_MASKED')"],cwd,scratch,writable=False)
  subprocess.run(command,check=True)
  calls.append(True)
  return {'success':True,'text':'Evidence-only review\nVERDICT: PASS','usage_observed':False}
 monkeypatch.setattr(r,'run_harness',qa)
 c=r.Cycle('t'*40,{});c.cycle={'base_sha':base,'integration_sha':sha};monkeypatch.setattr(c,'remaining',lambda:300)
 scratch=tmp_path/'scratch';scratch.mkdir()
 out=c._execute({'role_kind':'qa','attempt':{'id':'q'*40},'brief':'QA'},scratch,threading.Event())
 print(private_alias,calls,out)
 assert not calls and not out['ok'],'QA invoked and accepted PASS while the required current public file was masked'

async def test_legacy_confidence_autoapprove_excludes_native_tasks(env,monkeypatch):
 s,board,task=env
 approval=Approval(board_id=board.id,task_id=task.id,action_type='security_authority',confidence=95,status='pending')
 s.add(approval);await s.commit();patch_sql(s,monkeypatch)
 monkeypatch.setattr(owner_reviews,'automation_enabled',lambda org:False)
 assert await wl._auto_approve_high_confidence_approvals(s)==0
 await s.refresh(approval)
 assert approval.status=='pending'

async def test_legacy_autoclose_must_not_resolve_new_native_approval(env,monkeypatch):
 s,board,task=env
 task.execution_mode='single';task.execution_lane='agent';task.title='legacy';task.status='review';task.complexity=4
 approval=Approval(board_id=board.id,task_id=task.id,action_type='security_authority',confidence=95,status='pending')
 s.add(approval);await s.commit()
 patch_sql(s,monkeypatch)
 execute=s.execute
 async def syntax(stmt,*args,**kwargs):
  sql=str(stmt).replace("'-infinity'::timestamp","'1970-01-01'").replace(' ILIKE ',' LIKE ')
  return await execute(text(sql),*args,**kwargs)
 monkeypatch.setattr(s,'execute',syntax)
 monkeypatch.setattr(owner_reviews,'automation_enabled',lambda org:False)
 cycle=None
 async def markers(*args):
  nonlocal cycle
  cycle=await convert(s,board,task)
  return [{'tag':'ready-to-close','kv':{}},{'tag':'qa-verdict','kv':{'result':'pass','confidence':'95'}},{'tag':'pm-signoff','kv':{'verdict':'accept'}}]
 monkeypatch.setattr(wl,'_task_markers',markers)
 n=await wl._auto_close_review_tasks(s)
 await s.refresh(task);await s.refresh(approval)
 print('autoclose',n,task.execution_mode,task.status,approval.status)
 assert cycle is not None,'authorized conversion was not reached'
 assert approval.status=='pending','Legacy confidence completion resolved an approval for a freshly started native task'
