"""Native telemetry is board-authorized metadata, not captured HTTP/body evidence."""
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.models.team import TeamAttempt, TeamMember
from app.schemas.team import TeamCompleteRequest
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env  # noqa: F401
from tests.test_autonomous_review import sidecar  # noqa: F401


async def test_native_metadata_is_authorized_and_never_exposes_reports(env):
    from app.api import traffic

    env["app"].include_router(traffic.router, prefix="/api/v1")
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        attempts = (await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))).all()
        for attempt in attempts:
            session.add(TeamMember(id=attempt.member_id, board_id=task.board_id,
                name=attempt.role_kind, role_kind=attempt.role_kind,
                provider=attempt.provider, model=attempt.model, created_by="admin"))
            attempt.assignment = json.dumps({"tool_counts": {"read": 2}, "private": "not for telemetry"})
        await session.commit()
    async with _client(env) as client:
        response = await client.get(f"/api/v1/traffic/native-attempts?board_id={task.board_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert len(rows) == 2 and {row["attempt_id"] for row in rows} == {str(a.id) for a in attempts}
        assert all(row["tool_counts"] == {"read": 2} and row["task_id"] == str(task.id) for row in rows)
        assert not any(key in row for row in rows for key in ("artifact", "assignment", "error", "claim_hash"))
        assert "not for telemetry" not in response.text
        assert (await client.get(f"/api/v1/traffic/native-attempts?board_id={uuid4()}", headers=AUTH)).status_code == 403
        assert (await client.get("/api/v1/traffic/native-attempts?limit=101", headers=AUTH)).status_code == 422
        assert (await client.get("/api/v1/traffic/native-attempts")).status_code == 401
    async with env["maker"]() as session:
        attempt = await session.get(TeamAttempt, attempts[0].id)
        attempt.board_id = uuid4()  # A corrupt/cross-board attempt is never attributed to this task.
        await session.commit()
    async with _client(env) as client:
        rows = (await client.get("/api/v1/traffic/native-attempts", headers=AUTH)).json()
        assert len(rows) == 1 and rows[0]["attempt_id"] != str(attempts[0].id)


async def test_native_window_includes_new_completion_not_only_old_claim(env):
    from datetime import timedelta
    from app.api import traffic
    from app.core.time import utcnow

    env["app"].include_router(traffic.router, prefix="/api/v1")
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        attempt = (await session.exec(select(TeamAttempt).where(
            TeamAttempt.task_id == task.id, TeamAttempt.role_kind == "qa"
        ))).one()
        session.add(TeamMember(id=attempt.member_id, board_id=task.board_id,
            name="QA", role_kind="qa", provider=attempt.provider, model=attempt.model, created_by="admin"))
        attempt.updated_at = utcnow() - timedelta(hours=2)
        attempt.finished_at = utcnow()
        await session.commit()
    since = (utcnow() - timedelta(minutes=1)).isoformat()
    async with _client(env) as client:
        response = await client.get("/api/v1/traffic/native-attempts", headers=AUTH, params={"since": since})
        assert response.status_code == 200
        assert [row["attempt_id"] for row in response.json()] == [str(attempt.id)]


@pytest.mark.parametrize("value", ["yes", "1", 1, "on"])
def test_expired_recovery_authority_is_a_strict_boolean(value):
    from app.schemas.team import TeamRetryQA

    with pytest.raises(ValidationError):
        TeamRetryQA(version=1, recover_expired=value)
    assert TeamRetryQA(version=1, recover_expired=True).recover_expired is True


@pytest.mark.parametrize("counts", [{"read": True}, {"read": -1}, {"read": 100001}, {"credential": 1}])
def test_tool_counts_reject_non_counts_or_unknown_fields(counts):
    with pytest.raises(ValidationError):
        TeamCompleteRequest(claim_token="test", ok=True, tool_counts=counts)
