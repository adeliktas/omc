"""Untrusted SDK code cannot redirect privileged host Git or run commit hooks."""
import importlib.util
from pathlib import Path
import subprocess

from tests.test_team_runner_e2e import setup_world, tick, snapshot, git

ROOT=Path(__file__).resolve().parents[2]


def test_worker_git_pointer_tampering_blocks_before_host_git(tmp_path):
    repo,mc,env=setup_world(tmp_path,FIXTURE_GIT_TAMPER='1')
    before=git(repo,'rev-parse','HEAD')
    tick(repo,env)
    task,cycles,attempts,_=snapshot(mc)
    assert task.status=='blocked' and cycles[0].state=='blocked'
    assert any('Git metadata' in (a.error or '') for a in attempts)
    assert git(repo,'rev-parse','HEAD')==before
    assert git(repo,'status','--porcelain')==''


def test_worker_that_checks_git_status_first_still_implements(tmp_path):
    # 2026-10-24: Git is absent in the sandbox by design; private-board workers obeying
    # "Inspect `git status` before editing" stopped with no changes and blocked 3 tasks.
    repo,mc,env=setup_world(tmp_path,FIXTURE_GIT_STATUS_FIRST='1')
    tick(repo,env)
    task,cycles,attempts,_=snapshot(mc)
    assert task.status=='review' and cycles[0].state=='published'
    assert not any('must commit a change' in (a.error or '') for a in attempts)


def test_previous_cycle_integration_branch_cannot_wedge_a_new_cycle(tmp_path):
    repo,mc,env=setup_world(tmp_path)
    old_branch=f"team/{str(mc.ids['task'])[:8]}-int-1"
    before=git(repo,'rev-parse','HEAD')
    git(repo,'branch',old_branch,before)
    tick(repo,env)
    task,cycles,_,_=snapshot(mc)
    assert task.status=='review' and cycles[0].state=='published'
    assert git(repo,'rev-parse',old_branch)==before  # preserve failed-cycle evidence


def test_resumed_cycle_uses_its_durable_base_not_new_production_head(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('resume_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    old,new="a"*40,"b"*40
    run=r.Cycle("task",{"max_seconds_budget":300})
    views=iter([({"state":"developing","base_sha":old},[]),({"state":"planning","id":"cycle","base_sha":old},[]),({"state":"published"},[])])
    monkeypatch.setattr(run,'view',lambda:next(views))
    monkeypatch.setattr(r,'git',lambda *a,**k:new)
    observed=[]
    monkeypatch.setattr(r,'api',lambda method,path,body:observed.append(body["base_sha"]))
    monkeypatch.setattr(run,'budget_ready',lambda:True)
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    run.drive()
    assert observed==[old]  # later publication must still merge/re-QA onto new production


def test_host_commit_never_executes_repository_hooks(tmp_path):
    spec=importlib.util.spec_from_file_location('boundary_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    for key,value in [('user.name','test'),('user.email','t@x.invalid')]:r.git(repo,'config',key,value)
    hooks=repo/'.git/hooks';marker=tmp_path/'host-escape'
    hook=hooks/'post-commit';hook.write_text(f'#!/bin/sh\ntouch {marker}\n');hook.chmod(0o755)
    (repo/'data').write_text('safe\n')
    r.git(repo,'add','data');r.git(repo,'commit','-qm','safe host commit')
    assert not marker.exists()


def test_a_finished_attempt_frees_its_slot_while_a_slow_one_runs(monkeypatch):
    # 2026-10-24: the runner waited for its whole claim batch, so 8 stages of one cycle
    # sat idle behind a single hour-long attempt while engine slots were free.
    import threading
    spec=importlib.util.spec_from_file_location('refill_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    monkeypatch.setattr(r,'PARALLEL',2)
    run=r.Cycle("task",{"max_seconds_budget":300})
    claims,order,released=iter(["slow","fast","next"]),[],threading.Event()

    def api(method,path,body):
        got=next(claims,None)
        if got:
            order.append(f"claim {got}")
        if got=="next":
            released.set()
        return got

    def attempt(got):
        if got=="slow":
            released.wait(5)
            order.append("slow done")

    monkeypatch.setattr(run,'view',lambda:({"state":"published" if "slow done" in order else "developing","id":"c","base_sha":"a"*40},[]))
    monkeypatch.setattr(run,'attempt',attempt)
    monkeypatch.setattr(run,'budget_ready',lambda:True)
    monkeypatch.setattr(run,'integrate',lambda c,a:False)
    monkeypatch.setattr(r,'api',api)
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    run.drive()
    assert "claim next" in order and order.index("claim next")<order.index("slow done")


def test_attempt_time_is_bounded_from_claim_not_cycle_or_runner_age(monkeypatch):
    import time
    spec=importlib.util.spec_from_file_location('budget_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    run=r.Cycle("task",{})
    run.deadline=time.monotonic()-1  # this runner's own hour only stops new claims
    run.cycle={"deadline_at":"expired"}
    now=time.monotonic()
    monkeypatch.setattr(r.time,'monotonic',lambda:now)
    claim={"_runner_deadline":now+600}
    assert run.remaining(claim)==600  # cycle deadline is extendable during healthy work
    monkeypatch.setattr(r.time,'monotonic',lambda:now+601)
    assert run.remaining(claim)==0  # the configured attempt timeout remains hard


def test_dependency_pr_branches_are_found_and_merged_into_the_base(tmp_path):
    # 2026-10-24: cycles start from upstream, so 0000a016 never saw its dependencies' open PRs.
    spec=importlib.util.spec_from_file_location('stack_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    origin,clone=tmp_path/"origin",tmp_path/"clone"
    run=lambda *a,cwd=None:subprocess.run(["git",*a],cwd=cwd,check=True,capture_output=True,env={**r.os.environ,**r.identity("t")})
    run("init","-q","-b","main",str(origin))
    (origin/"a").write_text("a");run("add","a",cwd=origin);run("commit","-qm","a",cwd=origin)
    run("checkout","-qb","omc/abcd1234-11111111",cwd=origin)
    (origin/"dep").write_text("dep");run("add","dep",cwd=origin);run("commit","-qm","dep",cwd=origin)
    run("checkout","-q","main",cwd=origin)
    run("clone","-q",str(origin),str(clone))
    r.REPO=clone
    r.api=lambda *a,**k:{"items":[]}
    refs=r.dependency_branches({"depends_on_task_ids":["abcd1234-0000","ffff0000-0000"]})
    assert refs==["origin/omc/abcd1234-11111111"] and not r.contains(refs[0],"HEAD")
    run("merge","-q","--no-edit",refs[0],cwd=clone)
    assert r.contains(refs[0],"HEAD") and (clone/"dep").exists()


def test_split_dependency_stacks_its_child_task_branches(tmp_path):
    # 2026-10-26: a task depended on a task whose plan was split into child tasks; the parent
    # never publishes omc/<parent8>-*, so the dependent started without the children's open PRs.
    spec=importlib.util.spec_from_file_location('split_stack_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    origin,clone=tmp_path/"origin",tmp_path/"clone"
    run=lambda *a,cwd=None:subprocess.run(["git",*a],cwd=cwd,check=True,capture_output=True,env={**r.os.environ,**r.identity("t")})
    run("init","-q","-b","main",str(origin))
    (origin/"a").write_text("a");run("add","a",cwd=origin);run("commit","-qm","a",cwd=origin)
    for kid in ("c1111111","c2222222"):
        run("checkout","-qb",f"omc/{kid}-00000000","main",cwd=origin)
        (origin/kid).write_text(kid);run("add",kid,cwd=origin);run("commit","-qm",kid,cwd=origin)
    run("checkout","-q","main",cwd=origin)
    run("clone","-q",str(origin),str(clone))
    r.REPO=clone
    rows={"done":[{"id":f"other{i:03}-0000","parent_task_id":None} for i in range(200)]
                 +[{"id":"c1111111-0000","parent_task_id":"pppp0000-0000"},
                   {"id":"c2222222-0000","parent_task_id":"c1111111-0000"}],"review":[]}
    def api(method,path,*a):
        q=dict(kv.split("=") for kv in path.split("?")[1].split("&"))
        assert int(q["limit"])<=200  # the API refuses larger pages (422)
        return {"items":rows[q["status"]][int(q["offset"]):int(q["offset"])+int(q["limit"])]}
    r.api=api
    refs=r.dependency_branches({"depends_on_task_ids":["pppp0000-0000"]})
    assert refs==["origin/omc/c1111111-00000000","origin/omc/c2222222-00000000"]
