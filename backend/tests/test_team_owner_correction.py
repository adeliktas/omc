"""Owner-only bounded correction resumes retained code, never its successful history."""
import json
from uuid import uuid4

import pytest

from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env as api_env, sidecar
from tests.test_team_verification import (
    BASE, DEV_SHA, IMPLEMENT_PLAN, INTEGRATED, _planned, claim, env,
)


async def failed_recovery(env):
    session, board, task = env
    cycle = await _planned(session, board, task, IMPLEMENT_PLAN)
    dev, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, dev, token, ok=True,
        artifact="ran original checks", head_sha=DEV_SHA, changed_files=["backend/app/x.py"])
    await svc.integrate(session, cycle, INTEGRATED)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
        artifact="CORRECTION_KIND: EVIDENCE\nMissing independent probe", head_sha=INTEGRATED, verdict="fail")
    await svc.retry_qa(session, cycle, cycle.version)
    task.status = "in_progress"  # endpoint lifecycle transition
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
        artifact="CORRECTION_KIND: CODE\nReproduced cross-board association201; require denial", head_sha=INTEGRATED, verdict="fail")
    task.status = "blocked"  # runner's terminal-state projection
    await session.commit()
    return session, board, task, cycle, qa


async def test_explicit_owner_grant_queues_only_retained_correction_then_real_qa(env):
    s, board, task, cycle, failed = await failed_recovery(env)
    history = {a.id: a.model_dump() for a in await svc._attempts(s, cycle.id)}
    task.tokens_used = 111
    await svc.resume_correction(s, cycle, cycle.version, failed.id)
    attempts = await svc._attempts(s, cycle.id)
    assert all(a.model_dump() == history[a.id] for a in attempts if a.id in history)
    new = [a for a in attempts if a.id not in history]
    assert len(new) == 1 and new[0].stage_key == "dev-api"
    assignment = json.loads(new[0].assignment)
    assert assignment["base_sha"] == INTEGRATED and assignment["scope"] == ["backend/app"]
    assert assignment["feedback"] == failed.artifact and assignment["tool_counts"] is None
    assert cycle.round == 2 and cycle.state == "developing" and task.tokens_used == 111
    assert cycle.base_sha == BASE and cycle.published_sha is None
    assert 1190 < (cycle.deadline_at - svc.utcnow()).total_seconds() <= 1200
    task.status = "in_progress"  # endpoint lifecycle transition
    worker, token = await claim(s, cycle)
    assert worker.base_sha == INTEGRATED
    fixed = "e" * 40
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact="fixed only reproduced defect; regression passed", head_sha=fixed,
        changed_files=["backend/app/x.py"])
    await svc.integrate(s, cycle, fixed)
    qa, _ = await claim(s, cycle)
    member = await s.get(svc.TeamMember, qa.member_id)
    assert failed.artifact in svc.brief(task, cycle, qa, member, None, history=await svc._attempts(s, cycle.id))
    assert qa.role_kind == "qa" and qa.id != failed.id and cycle.round == 2


@pytest.mark.parametrize("change", ["version", "attempt", "inputs", "cancel", "published", "reservation", "cap", "partial", "evidence", "verify", "multi"])
async def test_owner_correction_refuses_without_replay(env, change):
    s, board, task, cycle, qa = await failed_recovery(env)
    version, aid = cycle.version, qa.id
    if change == "version": version -= 1
    elif change == "attempt": aid = uuid4()
    elif change == "inputs": task.title += " changed"
    elif change == "cancel": task.status = "cancelled"
    elif change == "published": cycle.published_sha = INTEGRATED
    elif change == "reservation": cycle.reviewed_sha = INTEGRATED
    elif change == "cap": cycle.max_rounds = 1
    elif change == "partial": qa.assignment = json.dumps({**json.loads(qa.assignment), "artifact_complete": False})
    elif change == "evidence": qa.artifact = "CORRECTION_KIND: EVIDENCE\nNo proof"
    else:
        plan = json.loads(cycle.plan)
        if change == "verify": plan["workers"][0]["mode"] = "verify"
        else: plan["workers"].append({**plan["workers"][0], "key": "other", "scope": ["frontend"]})
        cycle.plan = json.dumps(plan)
    before = len(await svc._attempts(s, cycle.id))
    with pytest.raises(svc.TeamError):
        await svc.resume_correction(s, cycle, version, aid)
    assert len(await svc._attempts(s, cycle.id)) == before and cycle.round == 1


async def test_first_verdict_after_unstarted_qa_resumes_every_leaf(env):
    s, board, task, cycle, qa = await failed_recovery(env)
    plan = json.loads(cycle.plan)
    plan["workers"].append({**plan["workers"][0], "key": "other", "scope": ["frontend"]})
    cycle.plan = json.dumps(plan)
    qa.assignment = json.dumps({**json.loads(qa.assignment), "recovered_block_reason":
        "stage qa failed after 2 attempts (harness runtime setup failed (OSError))"})
    before = {a.id for a in await svc._attempts(s, cycle.id)}
    await svc.resume_correction(s, cycle, cycle.version, qa.id)
    new = [a for a in await svc._attempts(s, cycle.id) if a.id not in before]
    assert sorted(a.stage_key for a in new) == ["dev-api", "dev-other"] and cycle.round == 2
    assert all(json.loads(a.assignment)["base_sha"] == INTEGRATED for a in new)


@pytest.mark.parametrize("gate", ["ok", "approval", "dependency", "nonadmin", "otherorg", "unauthenticated"])
async def test_correction_api_authority_lifecycle_and_atomic_rollback(api_env, gate, monkeypatch):
    from sqlmodel import select
    from app.api import team
    from app.models.boards import Board
    from app.models.tasks import Task
    from app.models.task_dependencies import TaskDependency
    from app.models.team import TeamCycle, TeamAttempt
    from app.models.approvals import Approval
    from app.models.organization_members import OrganizationMember
    from tests.test_team_cycle import RANKING

    async def ranking(_): return RANKING
    monkeypatch.setattr(team, "_ranking", ranking)
    api_env["app"].include_router(team.router, prefix="/api/v1")
    task = await _seed_review_task(api_env, with_cycle=False, status="in_progress")
    async with api_env["maker"]() as s:
        task = await s.get(Task, task.id)
        task.execution_mode, task.execution_lane = "team", "harness"
        board = await s.get(Board, task.board_id)
        _, _, task, cycle, qa = await failed_recovery((s, board, task))
        task.tokens_used, task.budget_exhausted_at = 123, svc.utcnow()
        used_at = task.budget_exhausted_at
        if gate == "approval":
            board.block_status_changes_with_pending_approval = True
            s.add(Approval(board_id=board.id, task_id=task.id, action_type="test", confidence=100, status="pending"))
        if gate == "dependency":
            dep = Task(board_id=board.id, title="unfinished", status="inbox")
            s.add(dep)
            await s.flush()
            s.add(TaskDependency(board_id=board.id, task_id=task.id, depends_on_task_id=dep.id))
        if gate in ("nonadmin", "otherorg"):
            member = (await s.exec(select(OrganizationMember))).one()
            member.role = "member"
        if gate == "otherorg":
            from app.models.organizations import Organization
            from app.models.users import User
            other = Organization(name="other active org")
            s.add(other)
            await s.flush()
            s.add(OrganizationMember(organization_id=other.id, user_id=api_env["user_id"],
                role="owner", all_boards_read=True, all_boards_write=True))
            user = await s.get(User, api_env["user_id"])
            user.active_organization_id = other.id
        await s.commit()
        cid, qid, version = cycle.id, qa.id, cycle.version
        count = len(await svc._attempts(s, cid))
    async with _client(api_env) as client:
        path = f"/api/v1/boards/{board.id}/tasks/{task.id}/team/cycles/{cid}/resume-correction"
        response = await client.post(path, headers={} if gate == "unauthenticated" else AUTH,
            json={"version": version, "failed_attempt_id": str(qid)})
        assert response.status_code == (200 if gate == "ok" else 401 if gate == "unauthenticated" else 403 if gate in ("nonadmin", "otherorg") else 409), response.text
        if gate == "otherorg":
            denied_qa = await client.post(path.replace("resume-correction", "retry-qa"),
                headers=AUTH, json={"version": version})
            assert denied_qa.status_code == 403
        if gate == "ok":
            async with api_env["maker"]() as s:
                granted = (await s.get(TeamCycle, cid)).model_dump()
                granted_attempts = [a.model_dump() for a in await svc._attempts(s, cid)]
            for retry_version in (version, version + 1):
                repeat = await client.post(path, headers=AUTH, json={"version": retry_version, "failed_attempt_id": str(qid)})
                assert repeat.status_code == 409
                async with api_env["maker"]() as s:
                    assert (await s.get(TeamCycle, cid)).model_dump() == granted
                    assert [a.model_dump() for a in await svc._attempts(s, cid)] == granted_attempts
    async with api_env["maker"]() as s:
        current, current_task = await s.get(TeamCycle, cid), await s.get(Task, task.id)
        attempts = await svc._attempts(s, cid)
        assert len(attempts) == count + (gate == "ok")
        assert current.round == (2 if gate == "ok" else 1)
        assert current.state == ("developing" if gate == "ok" else "blocked")
        assert current_task.status == ("in_progress" if gate == "ok" else "blocked")
        assert current_task.tokens_used == 123 and current_task.budget_exhausted_at == used_at
