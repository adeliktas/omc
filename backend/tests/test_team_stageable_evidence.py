"""Backend admission and the runner share one stageable packet policy."""
# ruff: noqa: F811 - imported pytest fixtures
import json

import pytest
from sqlmodel import select

from app.models.tasks import Task
from app.models.team import TeamCycle, TeamMember
from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env as api_env, sidecar  # noqa: F401
from tests.test_team_cycle import RANKING
from tests.test_team_qa_recovery import published
from tests.test_team_verification import BASE, claim, env  # noqa: F401


@pytest.mark.parametrize("files,valid", [
    ({"a/./proof.md": "whole original\n"}, False),
    ({"mc-team-attempt.fake/proof.md": "whole original\n"}, False),
    ({"proof": "whole original\n", "proof/child.md": "whole child\n"}, False),
    ({"reports/proof.md": "whole original\r\n\n"}, True),
])
async def test_owner_api_refuses_unstageable_packet_before_effects(api_env, monkeypatch, files, valid):
    from app.api import team
    api_env["app"].include_router(team.router, prefix="/api/v1")
    task = await _seed_review_task(api_env)
    async with api_env["maker"]() as session:
        t = await session.get(Task, task.id)
        t.execution_mode, t.tokens_used = "team", 123456
        cycle = (await session.exec(select(TeamCycle).where(TeamCycle.task_id == t.id))).one()
        cycle.task_fingerprint = svc.task_fingerprint(t)
        cycle.plan = json.dumps({"workers": [{"key": "main", "role": "developer",
            "scope": ["backend/tests/"], "mode": "verify"}]})
        session.add(TeamMember(board_id=t.board_id, name="QA", role_kind="qa",
            provider="claude-code", model="sonnet", created_by="admin"))
        await session.commit()
        before = (cycle.version, cycle.deadline_at, cycle.reviewed_sha, cycle.published_sha)
        ident, version = cycle.id, cycle.version
    async def ranking(_): return RANKING
    monkeypatch.setattr(team, "_ranking", ranking)
    async with _client(api_env) as client:
        response = await client.post(
            f"/api/v1/boards/{task.board_id}/tasks/{task.id}/team/cycles/{ident}/retry-qa",
            headers=AUTH, json={"version": version, "evidence_files": files})
    assert response.status_code == (200 if valid else 409), response.text
    async with api_env["maker"]() as session:
        t = await session.get(Task, task.id)
        cycle = await session.get(TeamCycle, ident)
        attempts = await svc._attempts(session, ident)
        assert t.tokens_used == 123456
        assert t.status == ("in_progress" if valid else "review")
        assert cycle.state == ("qa" if valid else "published")
        assert len(attempts) == (3 if valid else 2)
        if not valid:
            assert (cycle.version, cycle.deadline_at, cycle.reviewed_sha, cycle.published_sha) == before
        else:
            assert svc.qa_evidence_files(cycle, attempts, attempts[-1]) == files


@pytest.mark.parametrize("error,blocked", [
    ("runner error: invalid structured evidence path/content", True),
    ("runner error: whole structured evidence exceeds bounds or conflicts", True),
    ("runner error: structured evidence destination collision", True),
    ("503 upstream echoed: runner error: invalid structured evidence path/content", False),
])
async def test_deterministic_staging_error_never_retries_unchanged_packet(env, error, blocked):
    session, board, task, cycle = await published(env)
    await svc.retry_qa(session, cycle, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=False, error=error)
    attempts = await svc._attempts(session, cycle.id)
    assert cycle.state == ("blocked" if blocked else "qa")
    assert len(attempts) == (4 if blocked else 5)
    assert qa.status == "failed" and qa.error == error
    assert cycle.published_sha == BASE
    assert sum(a.role_kind == "developer" for a in attempts) == 1
    assert any(a.status == "queued" for a in attempts) is not blocked
