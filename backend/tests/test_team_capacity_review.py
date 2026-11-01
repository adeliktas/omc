"""Regressions from independent live Codex review of the capacity implementation."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import timedelta

from app.core.time import utcnow
from app.services import team_cycle as svc
from tests.test_team_capacity import planned, another_task
from tests.test_team_cycle import env, claim, BASE, RANKING
from tests.test_team_pool import setup_pool, team_task

ROOT = Path(__file__).resolve().parents[2]


async def test_failed_worker_keeps_module_until_own_cycle_ends(env):
    s, board, task = env
    a = await planned(s, board, task, 'backend')
    worker, token = await claim(s, a)
    await svc.complete_attempt(s, board, a, worker, token, ok=False, error='transient failure')
    assert worker.status == 'failed'
    b = await planned(s, board, await another_task(s, board), 'backend/api')
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING) is None
    await svc.cancel_cycle(s, a, 'owner stopped')
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING)


async def test_publication_resume_reaps_only_abandoned_reservation(env):
    s, board, task = env
    c,_=await svc.start_cycle(s,board,task,RANKING)
    c.state='qa_passed'; c.reviewed_sha=BASE
    await svc.reserve_publication(s,c,BASE,c.version)
    await svc.recover(s,c)
    assert c.state=='publishing'
    c.updated_at=utcnow()-timedelta(seconds=svc.PUBLISH_LEASE_SECONDS+1)
    await svc.recover(s,c)
    assert c.state=='blocked' and 'publisher vanished' in c.block_reason


def test_pool_shutdown_kills_deployment_descendants_not_only_runner(tmp_path):
    repo,env,record=setup_pool(tmp_path,[team_task('job')])
    body="""import os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path(os.environ['RUNNER_RECORD']).write_text(f'{os.getpid()} {p.pid}')
time.sleep(60)
"""
    (repo/'scripts/mc-team-runner.py').write_text(body)
    subprocess.run(['git','-C',str(repo),'add','scripts/mc-team-runner.py'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@x.invalid','commit','-qm','fixture descendant'],check=True)
    p=subprocess.Popen([sys.executable,str(repo/'scripts/mc-team-pool.py')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    parent=None
    try:
        deadline=time.monotonic()+8
        while not record.read_text() and time.monotonic()<deadline:time.sleep(.05)
        parent,child=map(int,record.read_text().split())
        p.terminate();p.wait(timeout=15)
        path=Path(f'/proc/{child}/stat')
        assert not path.exists() or path.read_text().split()[2]=='Z'
    finally:
        if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()
        if parent:
            try:os.killpg(parent,signal.SIGKILL)
            except ProcessLookupError:pass


def test_copied_ad_cannot_automatically_refresh_near_expiry(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('lease_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    (tmp_path/'auth.json').write_text(json.dumps({'openai-codex':{'expires':0}}))
    monkeypatch.delenv('FIXTURE_LOG',raising=False)
    monkeypatch.setattr(r,'harness_cmd',lambda *a: (['must-not-run'],{'PI_CODING_AGENT_DIR':str(tmp_path)}))
    monkeypatch.setattr(r,'api',lambda *a,**k:{})
    result=r.run_harness({'attempt':{'id':'12345678','provider':'openai-codex-2','model':'gpt-5.6-sol','thinking_level':'medium'},'role_kind':'qa'},'read-only',tmp_path,tmp_path,120)
    assert not result['success'] and not result['retryable']
    assert result['usage']['availability_reason']=='credential_repair_required'
