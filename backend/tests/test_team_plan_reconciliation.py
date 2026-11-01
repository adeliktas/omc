"""A GitHub path is not Git metadata; retain native plans without author replay."""
import json
from uuid import UUID, uuid4

import pytest

from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env as api_env, sidecar  # noqa: F401
from tests.test_team_cycle import RANKING
from tests.test_team_verification import claim, env  # noqa: F401

PLAN = {"workers": [{"key": "main", "role": "developer", "mode": "implement",
    "scope": ["backend/tests/", ".github/workflows/ci.yml"], "instructions": "add original tests"}]}


@pytest.mark.parametrize("path,ok", [
    (".github/workflows/ci.yml", True), ("backend/tests/", True),
    (".git", False), (".git/config", False), (".gitignore", False),
    (".gitattributes", False), (".gitmodules", False), ("backend/.git/config", False),
    (".github/workflows/.git/config", False), (".github/../.git/config", False),
    ("/.github/workflows/ci.yml", False), ("backend\\.git", False),
])
def test_scope_distinguishes_github_from_git_metadata(path, ok):
    assert svc._scope_ok(path) is ok


async def rejected_plan(env, monkeypatch):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    original = svc._scope_ok
    with monkeypatch.context() as m:
        m.setattr(svc, "_scope_ok", lambda p: original(p) and not p.startswith(".github"))
        for _ in range(2):
            pm, token = await claim(s, cycle)
            await svc.complete_attempt(s, board, cycle, pm, token, ok=True,
                artifact=json.dumps(PLAN), ranking=RANKING)
    task.status = "blocked"
    task.tokens_used = 321
    await s.commit()
    return s, board, task, cycle, pm


async def test_adopt_complete_native_plan_preserves_failed_authors_caps_and_deadline(env, monkeypatch):
    s, board, task, cycle, pm = await rejected_plan(env, monkeypatch)
    old = {a.id: a.model_dump() for a in await svc._attempts(s, cycle.id)}
    deadline, rounds, version = cycle.deadline_at, (cycle.round, cycle.max_rounds), cycle.version
    adopted = await svc.resume_work(s, cycle, version, "reconciled_plan", pm.id, ranking=RANKING)
    assert adopted.id == cycle.id and cycle.state == "developing" and cycle.block_reason is None
    assert cycle.deadline_at == deadline and (cycle.round, cycle.max_rounds) == rounds
    assert all(a.model_dump() == old[a.id] for a in await svc._attempts(s, cycle.id) if a.id in old)
    assert task.tokens_used == 321 and task.budget_exhausted_at is None
    assert json.loads(cycle.plan) == svc.parse_plan(pm.artifact)
    await s.commit()
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, cycle, version, "reconciled_plan", pm.id, ranking=RANKING)
    assert len(await svc._attempts(s, cycle.id)) == 3


@pytest.mark.parametrize("change", ["version", "attempt", "expired", "inputs", "paused", "published", "reservation", "partial", "wrong_error", "manual_hold", "unsafe", "writer", "live", "parent", "delegated_cap", "array_assignment"])
async def test_plan_adoption_refuses_stale_incomplete_or_new_authority(env, monkeypatch, change):
    s, board, task, cycle, pm = await rejected_plan(env, monkeypatch)
    version, aid = cycle.version, pm.id
    if change == "version": version -= 1
    elif change == "attempt": aid = uuid4()
    elif change == "expired": cycle.deadline_at = svc.utcnow()
    elif change == "inputs": task.title += " edited"
    elif change == "paused": task.status = "paused"
    elif change == "published": cycle.published_sha = "a" * 40
    elif change == "reservation": cycle.reviewed_sha = "a" * 40
    elif change == "partial": pm.assignment = json.dumps({"artifact_complete": False})
    elif change == "wrong_error": pm.error = "quota unavailable"
    elif change == "manual_hold": cycle.block_reason = "owner hold; do not execute"
    elif change == "unsafe": pm.artifact = json.dumps({"workers": [{**PLAN["workers"][0], "scope": [".git/config"]}]})
    elif change == "writer": pm.role_kind = "developer"
    elif change == "live": pm.status = "running"
    elif change == "array_assignment": pm.assignment = "[]"
    elif change == "parent":
        parent = svc.Task(board_id=board.id, title="stopped", status="paused")
        s.add(parent)
        await s.flush()
        task.parent_task_id = parent.id
    else:
        from app.api.review import _AUTO_REVIEW_TAG
        from app.models.activity_events import ActivityEvent
        for _ in range(3):
            s.add(ActivityEvent(board_id=board.id, task_id=task.id, event_type="task.comment",
                message=f"{_AUTO_REVIEW_TAG} action=revise"))
    await s.flush()
    snapshot = cycle.model_dump()
    count = len(await svc._attempts(s, cycle.id))
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, cycle, version, "reconciled_plan", aid, ranking=RANKING)
    assert cycle.model_dump() == snapshot and len(await svc._attempts(s, cycle.id)) == count


@pytest.mark.parametrize("gate", ["ok", "approval", "dependency", "nonadmin", "otherorg", "unauthenticated"])
async def test_plan_api_gates_and_duplicate_does_not_cancel_claimed_worker(api_env, monkeypatch, gate):
    from app.api import team
    from app.models.approvals import Approval
    from app.models.organization_members import OrganizationMember
    from app.models.task_dependencies import TaskDependency
    from sqlmodel import select
    async def ranking(_): return RANKING
    monkeypatch.setattr(team, "_ranking", ranking)
    api_env["app"].include_router(team.router, prefix="/api/v1")
    seed = await _seed_review_task(api_env, with_cycle=False, status="in_progress")
    async with api_env["maker"]() as s:
        task = await s.get(svc.Task, seed.id)
        task.execution_mode, task.execution_lane = "team", "harness"
        board = await s.get(svc.Board, task.board_id)
        _, _, _, cycle, pm = await rejected_plan((s, board, task), monkeypatch)
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
                other = Organization(name="other")
                s.add(other)
                await s.flush()
                s.add(OrganizationMember(organization_id=other.id, user_id=api_env["user_id"], role="owner", all_boards_read=True, all_boards_write=True))
                (await s.get(User, api_env["user_id"])).active_organization_id = other.id
        await s.commit()
        snapshot = cycle.model_dump()
        history = [a.model_dump() for a in await svc._attempts(s, cycle.id)]
        cid, version, aid = cycle.id, cycle.version, pm.id
    path = f"/api/v1/boards/{board.id}/tasks/{task.id}/team/cycles/{cid}/resume-work"
    payload = {"version": version, "cause": "reconciled_plan", "failed_attempt_id": str(aid)}
    async with _client(api_env) as client:
        response = await client.post(path, headers={} if gate == "unauthenticated" else AUTH, json=payload)
        expected = 200 if gate == "ok" else 401 if gate == "unauthenticated" else 403 if gate in ("nonadmin", "otherorg") else 409
        assert response.status_code == expected, response.text
        if gate == "ok":
            assert UUID(response.json()["id"]) == cid
            async with api_env["maker"]() as s:
                current = await s.get(svc.TeamCycle, cid)
                worker, _ = await claim(s, current)
                claimed = worker.model_dump()
                after = current.model_dump()
            for stamp in (version, version + 1):
                assert (await client.post(path, headers=AUTH, json={**payload, "version": stamp})).status_code == 409
            async with api_env["maker"]() as s:
                assert (await s.get(svc.TeamAttempt, worker.id)).model_dump() == claimed
                assert (await s.get(svc.TeamCycle, cid)).model_dump() == after
                assert [a.model_dump() for a in await svc._attempts(s, cid) if a.id != worker.id] == history
        else:
            async with api_env["maker"]() as s:
                assert (await s.get(svc.TeamCycle, cid)).model_dump() == snapshot
                assert [a.model_dump() for a in await svc._attempts(s, cid)] == history
    async with api_env["maker"]() as s:
        current_task = await s.get(svc.Task, task.id)
        assert current_task.status == ("in_progress" if gate == "ok" else "blocked")
        assert current_task.tokens_used == 321 and current_task.budget_exhausted_at is None
