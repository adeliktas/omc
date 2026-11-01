"""A supplemental owner grant is new bounded work, never rewritten history."""
# ruff: noqa: F401, F811 - imported pytest fixtures are injected as parameters
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.services import team_cycle as svc
from tests.test_team_owner_correction import failed_recovery
from tests.test_team_verification import DEV_SHA, IMPLEMENT_PLAN, INTEGRATED, _planned, claim, env
from tests.test_team_cycle import RANKING
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env as api_env, sidecar


async def source_work(env, cause):
    s, board, task = env
    if cause == "report_evidence":
        from tests.test_team_report_recovery import source_report
        s, board, task, cycle, worker, failed = await source_report(env)
        return s, board, task, cycle, failed
    if cause == "expired_unstarted":
        cycle = await _planned(s, board, task, IMPLEMENT_PLAN)
        await svc._block(s, cycle, "cycle deadline exceeded")
        cycle.deadline_at = svc.utcnow() - timedelta(seconds=1)
        failed = None
    else:
        s, board, task, cycle, failed = await failed_recovery(env)
        await svc.resume_correction(s, cycle, cycle.version, failed.id)
        task.status = "in_progress"
        worker, token = await claim(s, cycle)
        await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
            artifact="original correction complete", head_sha="e" * 40,
            changed_files=["backend/app/x.py"])
        await svc.integrate(s, cycle, "e" * 40)
        failed, token = await claim(s, cycle)
        failed.provider, failed.model = "omniroute-codex", "gpt-6.1-sol"
        await svc.complete_attempt(s, board, cycle, failed, token, ok=True,
            artifact="CORRECTION_KIND: CODE\nConcurrent duplicate acknowledgement reproduced\nVERDICT: FAIL",
            head_sha="e" * 40, verdict="fail")
    task.status = "blocked"
    await s.commit()
    return s, board, task, cycle, failed


@pytest.mark.parametrize("cause", ["expired_unstarted", "retained_code_failure"])
async def test_new_one_round_grant_preserves_source_and_fails_closed_on_repeat(env, cause):
    s, board, task, old, failed = await source_work(env, cause)
    previous = old.model_dump()
    history = {a.id: a.model_dump() for a in await svc._attempts(s, old.id)}
    task.tokens_used = 123
    new = await svc.resume_work(s, old, old.version, cause,
        failed.id if failed else None, ranking=RANKING)
    assert old.model_dump() == previous
    assert {a.id: a.model_dump() for a in await svc._attempts(s, old.id)} == history
    assert new.id != old.id and new.round == new.max_rounds == 1
    assert new.plan == old.plan and new.task_fingerprint == old.task_fingerprint
    assert new.state == "developing" and task.tokens_used == 123
    from app.api.review import _count_delegated_revisions
    assert await _count_delegated_revisions(s, task.id) == int(failed is not None)
    assert 1190 < (new.deadline_at - svc.utcnow()).total_seconds() <= 1200
    attempts = await svc._attempts(s, new.id)
    assert len(attempts) == 1 and attempts[0].role_kind == "developer"
    assignment = json.loads(attempts[0].assignment)
    assert assignment["base_sha"] == (old.integration_sha if failed else None)
    assert assignment["owner_work_recovery"]["source_cycle_id"] == str(old.id)
    if failed:
        assert failed.artifact in [r["artifact"] for r in json.loads(new.qa_feedback)]
        assert new.base_sha == old.base_sha
    await s.commit()
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, old, old.version, cause,
            failed.id if failed else None, ranking=RANKING)
    assert len(await svc._attempts(s, new.id)) == 1
    task.status = "in_progress"
    worker, token = await claim(s, new)
    await svc.complete_attempt(s, board, new, worker, token, ok=True,
        artifact="new original-scope work", head_sha="f" * 40,
        changed_files=["backend/app/x.py"])
    await svc.integrate(s, new, "f" * 40)
    qa, token = await claim(s, new)
    member = await s.get(svc.TeamMember, qa.member_id)
    brief = svc.brief(task, new, qa, member, None, history=await svc._attempts(s, new.id))
    if failed:
        assert json.dumps(failed.artifact) in brief
    await svc.complete_attempt(s, board, new, qa, token, ok=True,
        artifact="CORRECTION_KIND: CODE\nStill broken\nVERDICT: FAIL", head_sha="f" * 40, verdict="fail")
    assert new.state == "blocked" and new.round == new.max_rounds == 1
    task.status = "blocked"
    with pytest.raises(svc.TeamError, match="already used"):
        await svc.resume_work(s, new, new.version, "retained_code_failure", qa.id, ranking=RANKING)


@pytest.mark.parametrize("change", ["version", "inputs", "paused", "published", "reservation", "exhausted", "live", "not_expired", "successful_writer", "scope", "parent", "delegated_cap", "array_assignment", "invalid_assignment", "null_assignment"])
async def test_expired_grant_refuses_wrong_cause_or_authority(env, change):
    s, board, task, old, failed = await source_work(env, "expired_unstarted")
    version = old.version
    if change == "version": version -= 1
    elif change == "inputs": task.title += " edited"
    elif change == "paused": task.status = "paused"
    elif change == "published": old.published_sha = INTEGRATED
    elif change == "reservation": old.reviewed_sha = INTEGRATED
    elif change == "exhausted": task.budget_exhausted_at = svc.utcnow()
    elif change == "live":
        a = next(a for a in await svc._attempts(s, old.id) if a.role_kind == "developer")
        a.status = "running"
    elif change == "not_expired": old.deadline_at = svc.utcnow() + timedelta(seconds=60)
    elif change == "successful_writer":
        a = next(a for a in await svc._attempts(s, old.id) if a.role_kind == "developer")
        a.status, a.head_sha = "succeeded", DEV_SHA
    elif change == "scope": old.plan = "{}"
    elif change == "parent":
        parent = svc.Task(board_id=board.id, title="stopped parent", status="paused")
        s.add(parent)
        await s.flush()
        task.parent_task_id = parent.id
    elif change.endswith("assignment"):
        a = (await svc._attempts(s, old.id))[0]
        a.assignment = {"array_assignment": "[1]", "invalid_assignment": "{", "null_assignment": "null"}[change]
    else:
        from app.api.review import _AUTO_REVIEW_TAG
        from app.models.activity_events import ActivityEvent
        for _ in range(3):
            s.add(ActivityEvent(board_id=board.id, task_id=task.id, event_type="task.comment",
                message=f"{_AUTO_REVIEW_TAG} action=revise"))
    await s.flush()
    count = len((await s.exec(select(svc.TeamCycle))).all())
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, old, version, "expired_unstarted", ranking=RANKING)
    assert len((await s.exec(select(svc.TeamCycle))).all()) == count


@pytest.mark.parametrize("change", ["wrong_attempt", "partial", "evidence", "verify", "wrong_head", "unqualified", "previous_qa_missing"])
async def test_residual_code_grant_requires_complete_real_failure(env, change):
    s, board, task, old, failed = await source_work(env, "retained_code_failure")
    aid = failed.id
    if change == "wrong_attempt": aid = uuid4()
    elif change == "partial": failed.assignment = json.dumps({"artifact_complete": False})
    elif change == "evidence": failed.artifact = "CORRECTION_KIND: EVIDENCE\nUnavailable"
    elif change == "verify":
        plan = json.loads(old.plan)
        plan["workers"][0]["mode"] = "verify"
        old.plan = json.dumps(plan)
    elif change == "wrong_head": failed.head_sha = "d" * 40
    elif change == "unqualified": failed.provider, failed.model = "vllm", "qwen"
    else:
        earlier = next(a for a in await svc._attempts(s, old.id) if a.role_kind == "qa" and a.id != failed.id)
        earlier.artifact = None
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, old, old.version, "retained_code_failure", aid, ranking=RANKING)


@pytest.mark.parametrize("cause", ["expired_unstarted", "report_evidence"])
@pytest.mark.parametrize("gate", ["ok", "approval", "dependency", "nonadmin", "otherorg", "unauthenticated"])
async def test_owner_work_api_atomic_gates_and_duplicate_post(api_env, monkeypatch, gate, cause):
    from app.api import team
    from app.models.approvals import Approval
    from app.models.organization_members import OrganizationMember
    from app.models.task_dependencies import TaskDependency
    async def ranking(_): return RANKING
    monkeypatch.setattr(team, "_ranking", ranking)
    api_env["app"].include_router(team.router, prefix="/api/v1")
    seed = await _seed_review_task(api_env, with_cycle=False, status="in_progress")
    async with api_env["maker"]() as s:
        task = await s.get(svc.Task, seed.id)
        task.execution_mode, task.execution_lane = "team", "harness"
        board = await s.get(svc.Board, task.board_id)
        _, _, task, old, failed = await source_work((s, board, task), cause)
        task.tokens_used = 456
        if gate == "approval":
            board.block_status_changes_with_pending_approval = True
            s.add(Approval(board_id=board.id, task_id=task.id, action_type="test", confidence=100, status="pending"))
        elif gate == "dependency":
            dep = svc.Task(board_id=board.id, title="unfinished", status="inbox")
            s.add(dep)
            await s.flush()
            s.add(TaskDependency(board_id=board.id, task_id=task.id, depends_on_task_id=dep.id))
        elif gate in ("nonadmin", "otherorg"):
            (await s.exec(select(OrganizationMember))).one().role = "member"
            if gate == "otherorg":
                from app.models.organizations import Organization
                from app.models.users import User
                other = Organization(name="other active org")
                s.add(other)
                await s.flush()
                s.add(OrganizationMember(organization_id=other.id, user_id=api_env["user_id"],
                    role="owner", all_boards_read=True, all_boards_write=True))
                (await s.get(User, api_env["user_id"])).active_organization_id = other.id
        await s.commit()
        old_snapshot = old.model_dump()
        cid, version = old.id, old.version
    path = f"/api/v1/boards/{board.id}/tasks/{task.id}/team/cycles/{cid}/resume-work"
    payload = {"version": version, "cause": cause,
               "failed_attempt_id": str(failed.id) if failed else None}
    async with _client(api_env) as client:
        response = await client.post(path, headers={} if gate == "unauthenticated" else AUTH, json=payload)
        assert response.status_code == (200 if gate == "ok" else 401 if gate == "unauthenticated" else 403 if gate in ("nonadmin", "otherorg") else 409), response.text
        if gate == "ok":
            nid = UUID(response.json()["id"])
            async with api_env["maker"]() as s:
                new_snapshot = (await s.get(svc.TeamCycle, nid)).model_dump()
                attempts = [a.model_dump() for a in await svc._attempts(s, nid)]
            for stamp in (version, version + 1):
                repeat = await client.post(path, headers=AUTH, json={**payload, "version": stamp})
                assert repeat.status_code == 409
            async with api_env["maker"]() as s:
                assert (await s.get(svc.TeamCycle, nid)).model_dump() == new_snapshot
                assert [a.model_dump() for a in await svc._attempts(s, nid)] == attempts
    async with api_env["maker"]() as s:
        assert (await s.get(svc.TeamCycle, cid)).model_dump() == old_snapshot
        current = await s.get(svc.Task, task.id)
        assert current.status == ("in_progress" if gate == "ok" else "blocked")
        assert current.tokens_used == 456 and current.budget_exhausted_at is None
        assert len((await s.exec(select(svc.TeamCycle).where(svc.TeamCycle.task_id == task.id))).all()) == 1 + (gate == "ok")
