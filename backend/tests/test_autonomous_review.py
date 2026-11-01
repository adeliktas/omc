"""Owner-authorized autonomous acceptance review (2026-10-05).

Covers the public automation contract, candidate listing, the delegated
decision API (gates, fencing, lifecycle routing, escalation dedup, bounded
corrections), the watchdog/approvals stand-down while autonomy is enabled,
and the standing reviewer script's strict verdict parsing + checkpoint rules.

No model, no network, no production service: SQLite + the real review router.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import review as review_api
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.approvals import Approval
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organization_members import OrganizationMember
from app.models.organizations import Organization
from app.models.owner_reviews import OwnerReview
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle, TeamMember
from app.models.users import User
from app.services import mc_sidecar, team_cycle
from app.services.owner_reviews import (
    AUTOMATION_POLICY,
    MAX_AUTO_CORRECTIONS,
    REVIEWER_MODELS,
    automation_enabled,
    automation_models_status,
    set_automation_enabled,
)

ROOT = Path(__file__).resolve().parents[2]
LOCAL_TOKEN = "test-local-token-0123456789-0123456789-0123456789x"
AUTH = {"Authorization": f"Bearer {LOCAL_TOKEN}"}


# ── shared infrastructure ──────────────────────────────────────────────────


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _build_app(session_maker: async_sessionmaker[AsyncSession]) -> FastAPI:
    from app.db.session import get_session

    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(review_api.router)
    app.include_router(api_v1)

    async def _override_get_session() -> AsyncSession:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    return app


@pytest.fixture
def sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "openclaw.mc.json"
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", path)
    return path


@pytest.fixture
async def env(sidecar: Path):
    """App + session maker + seeded org/user/board/task ids."""
    engine = await _make_engine()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(maker)
    org_id, user_id, gw_id, board_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with maker() as session:
        session.add(Organization(id=org_id, name="org"))
        await session.flush()
        session.add(
            Gateway(
                id=gw_id,
                organization_id=org_id,
                name="g",
                url="ws://fake.invalid/ws",
                workspace_root="/tmp/w",
            )
        )
        await session.flush()
        session.add(
            Board(
                id=board_id,
                organization_id=org_id,
                name="board",
                slug=f"b-{board_id.hex}",
                gateway_id=gw_id,
            )
        )
        session.add(
            User(
                id=user_id,
                clerk_user_id="local-auth-user",
                email="admin@home.local",
                name="Local",
                active_organization_id=org_id,
            )
        )
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=user_id,
                role="owner",
                all_boards_read=True,
                all_boards_write=True,
            )
        )
        await session.commit()
    yield {
        "app": app,
        "maker": maker,
        "org_id": org_id,
        "board_id": board_id,
        "user_id": user_id,
    }
    await engine.dispose()


def _client(env: dict) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=env["app"]), base_url="http://test")


async def _seed_review_task(
    env: dict,
    *,
    title: str = "team task",
    status: str = "review",
    with_cycle: bool = True,
    qa_pass: bool = True,
    published_sha: str | None = "a" * 40,
) -> Task:
    task_id = uuid4()
    async with env["maker"]() as session:
        task = Task(
            id=task_id,
            board_id=env["board_id"],
            title=title,
            description="do the thing",
            status=status,
            priority="medium",
            complexity=3,
        )
        session.add(task)
        if with_cycle:
            cycle = TeamCycle(
                board_id=env["board_id"],
                task_id=task_id,
                state="published" if published_sha else "qa_passed",
                deadline_at=utcnow() + timedelta(hours=1),
                published_sha=published_sha,
                reviewed_sha=published_sha,
                integration_sha=published_sha,
                plan="plan: implement + test",
                task_fingerprint=team_cycle.task_fingerprint(task),
            )
            session.add(cycle)
            await session.flush()
            qa_member = TeamMember(board_id=env["board_id"], name=f"qa-{task_id}",
                role_kind="qa", provider="claude-code", model="opus-5-5")
            writer_member = TeamMember(board_id=env["board_id"], name=f"dev-{task_id}",
                role_kind="developer", provider="deepseek-sub", model="deepseek-flash")
            session.add_all([qa_member, writer_member])
            await session.flush()
            session.add(
                TeamAttempt(
                    cycle_id=cycle.id,
                    board_id=env["board_id"],
                    task_id=task_id,
                    member_id=qa_member.id,
                    role_kind="qa",
                    stage_key="qa",
                    status="succeeded",
                    provider="claude-code",
                    model="opus-5-5",
                    verdict="pass" if qa_pass else "fail",
                    head_sha=published_sha,
                    artifact="Fixture independent QA report.",
                    assignment=json.dumps({"artifact_complete": True}),
                )
            )
            session.add(TeamAttempt(
                cycle_id=cycle.id, board_id=env["board_id"], task_id=task_id,
                member_id=writer_member.id, role_kind="developer", stage_key="dev-main",
                status="succeeded", provider="deepseek-sub", model="deepseek-flash",
                head_sha=published_sha, artifact="Fixture author evidence.",
            ))
        await session.commit()
    return task


async def test_same_model_acceptance_requires_fresh_separate_reviewer_session(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        writer = (await session.exec(select(TeamAttempt).where(
            TeamAttempt.task_id == task.id, TeamAttempt.role_kind == "developer"
        ))).one()
        writer.provider, writer.model = REVIEWER_MODELS[0]
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        response = await client.post(
            "/api/v1/review/decisions", headers=AUTH, json=_decision_body(candidate)
        )
        assert response.status_code == 422 and "authored" in response.text
    assert (await _get_task(env, task.id)).status == "review"
    async with env["maker"]() as session:
        assert not (await session.exec(select(Approval))).all()

    async with _client(env) as client:
        body = _decision_body(candidate)
        body["reviewer_session_id"] = str(writer.id)
        denied = await client.post("/api/v1/review/decisions", headers=AUTH, json=body)
        assert denied.status_code == 422
        body["reviewer_session_id"] = str(uuid4())
        allowed = await client.post("/api/v1/review/decisions", headers=AUTH, json=body)
        assert allowed.status_code == 200, allowed.text
    assert (await _get_task(env, task.id)).status == "done"
    async with env["maker"]() as session:
        approvals = (await session.exec(select(Approval))).all()
        assert len(approvals) == 1 and approvals[0].payload["review_basis"] == "separate_session"


async def test_acceptance_reviewer_alias_cannot_hide_authorship(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        attempts = (await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))).all()
        writer = next(a for a in attempts if a.role_kind == "developer")
        qa = next(a for a in attempts if a.role_kind == "qa")
        writer.provider, writer.model = "claude-code", "claude-opus-5-5"
        qa.provider, qa.model = "pi-neuralwatt", "glm-5.3"
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        body = _decision_body(candidate)
        body["reviewer_provider"], body["reviewer_model"] = "claude-code", "opus-5-5"
        response = await client.post("/api/v1/review/decisions", headers=AUTH, json=body)
        assert response.status_code == 422 and "authored" in response.text
    assert (await _get_task(env, task.id)).status == "review"
    async with env["maker"]() as session:
        assert not (await session.exec(select(Approval))).all()


@pytest.mark.parametrize("scope", ["task", "board", "both"])
async def test_foreign_attempt_rows_cannot_approve_the_locked_target_task(env, scope):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        foreign_org = Organization(name="foreign fixture tenant")
        session.add(foreign_org)
        await session.flush()
        foreign_board = Board(organization_id=foreign_org.id, name="foreign", slug=str(uuid4()))
        session.add(foreign_board)
        await session.flush()
        foreign_task = Task(board_id=foreign_board.id, title="different work", status="review")
        session.add(foreign_task)
        await session.flush()
        rows = (await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id))).all()
        for attempt in rows:
            if scope in ("task", "both"):
                attempt.task_id = foreign_task.id
            if scope in ("board", "both"):
                attempt.board_id = foreign_board.id
            member = await session.get(TeamMember, attempt.member_id)
            member.board_id = attempt.board_id
            session.add(member)
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        response = await client.post(
            "/api/v1/review/decisions", headers=AUTH, json=_decision_body(candidate)
        )
        assert response.status_code == 422 and "QA PASS" in response.text
    assert (await _get_task(env, task.id)).status == "review"
    async with env["maker"]() as session:
        assert not (await session.exec(select(Approval))).all()


async def _get_task(env: dict, task_id: Any) -> Task:
    async with env["maker"]() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task


async def _first_candidate(env: dict, client: AsyncClient, kind: str, candidate_id: str) -> dict:
    offset = 0
    while True:
        resp = await client.get(
            f"/api/v1/review/candidates?limit=200&offset={offset}", headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for item in body["items"]:
            if item["kind"] == kind and item["id"] == str(candidate_id):
                return item
        offset += 200
        if offset >= body["total"]:
            break
    raise AssertionError(f"no {kind} candidate {candidate_id}")


def _decision_body(candidate: dict, **overrides: Any) -> dict:
    body = {
        "kind": candidate["kind"],
        "id": candidate["id"],
        "fingerprint": candidate["fingerprint"],
        "expected_status": candidate["status"],
        "action": "accept",
        "impact": "routine",
        "reviewer_provider": REVIEWER_MODELS[0][0],
        "reviewer_model": REVIEWER_MODELS[0][1],
        "evidence": "QA PASS on published SHA; files verified.",
        "published_sha": ((candidate.get("evidence") or {}).get("cycle") or {}).get("published_sha"),
    }
    if overrides.get("action") == "revise":
        body["correction_kind"] = "code"
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


# ── automation contract ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_automation_defaults_disabled_with_ordered_trio(env) -> None:
    assert automation_enabled() is False
    async with _client(env) as client:
        resp = await client.get("/api/v1/review/automation", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
    assert body["enabled"] is False
    assert body["policy"] == AUTOMATION_POLICY
    assert [(m["provider"], m["model"]) for m in body["models"]] == [
        ("openai-codex-2", "gpt-6.1-sol"),
        ("omniroute-codex", "gpt-6.1-sol"),
        ("openai-codex-canonical", "gpt-6.1-sol"),
        ("claude-code", "opus-5-5"),
        ("pi-neuralwatt", "glm-5.3"),
    ]
    for model in body["models"]:
        if model["provider"] == "openai-codex-canonical":
            assert model["eligible"] is False and model["block_reason"] == "canonical_pi_unselected"
        else:
            assert model["eligible"] and model["block_reason"] is None


@pytest.mark.asyncio
async def test_automation_put_persists_and_requires_auth(env, sidecar) -> None:
    async with _client(env) as client:
        unauth = await client.put("/api/v1/review/automation", json={"enabled": True})
        assert unauth.status_code == 401

        resp = await client.put("/api/v1/review/automation", json={"enabled": True}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        # persisted via the sidecar namespace (no level → not on ranking board)
        meta = mc_sidecar.get_model_meta("_mc_automation", f"owner_review:{env['org_id']}")
        assert meta["enabled"] is True and "level" not in meta
        assert automation_enabled() is True

        body = (await client.get("/api/v1/review/automation", headers=AUTH)).json()
        assert body["enabled"] is True

        await client.put("/api/v1/review/automation", json={"enabled": False}, headers=AUTH)
    assert automation_enabled() is False


def test_automation_models_respect_manual_disable_and_holds(sidecar) -> None:
    mc_sidecar.set_model_meta(
        "claude-code",
        "opus-5-5",
        disabled=True,
        disabled_reason="owner paused",
    )
    mc_sidecar.set_model_meta(
        "openai-codex-2",
        "gpt-6.1-sol",
        unavailable_until="2999-01-01T00:00:00+00:00",
        availability_reason="usage_limit",
    )
    status = automation_models_status()
    by_model = {(m["provider"], m["model"]): m for m in status}
    assert by_model[("claude-code", "opus-5-5")]["eligible"] is False
    assert "owner paused" in by_model[("claude-code", "opus-5-5")]["block_reason"]
    assert by_model[("openai-codex-2", "gpt-6.1-sol")]["eligible"] is False
    assert by_model[("openai-codex-2", "gpt-6.1-sol")]["block_reason"] == "usage_limit"
    # No spending/token-budget gate: glm-5.3 stays eligible without a budget.
    assert by_model[("pi-neuralwatt", "glm-5.3")]["eligible"] is True


# ── candidates ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_candidates_require_org_admin(env) -> None:
    async with _client(env) as client:
        resp = await client.get("/api/v1/review/candidates")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_candidates_list_org_scoped_tasks_and_items(env) -> None:
    task = await _seed_review_task(env)
    item_id = uuid4()
    async with env["maker"]() as session:
        session.add(
            OwnerReview(
                id=item_id,
                title="WATCHDOG: stale task",
                type="other",
                summary="stuck",
                reason="auto",
                proposed_action="x",
                submitted_by="Pipeline Watchdog",
                task_id=task.id,
            )
        )
        # Another org's task + a non-watchdog item: both must be excluded.
        other_org, other_board, other_task = uuid4(), uuid4(), uuid4()
        session.add(Organization(id=other_org, name="other"))
        session.add(
            Board(
                id=other_board,
                organization_id=other_org,
                name="o",
                slug=f"o-{other_board.hex}",
            )
        )
        session.add(
            Task(
                id=other_task,
                board_id=other_board,
                title="other org task",
                status="review",
                priority="medium",
            )
        )
        session.add(
            OwnerReview(
                id=uuid4(),
                title="agent suggestion",
                type="other",
                submitted_by="Some Agent",
            )
        )
        await session.commit()

    async with _client(env) as client:
        resp = await client.get("/api/v1/review/candidates", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
    kinds = [(c["kind"], c["id"]) for c in body["items"]]
    assert ("task", str(task.id)) in kinds
    assert ("item", str(item_id)) in kinds
    assert ("task", str(other_task)) not in kinds
    task_candidate = next(c for c in body["items"] if c["kind"] == "task")
    assert task_candidate["board_id"] == str(env["board_id"])
    assert task_candidate["fingerprint"]
    evidence = task_candidate["evidence"]
    assert evidence["cycle"]["published_sha"] == "a" * 40
    assert evidence["qa_attempts"][0]["verdict"] == "pass"
    assert evidence["board"]["rules"]["require_approval_for_done"] is True
    item_candidate = next(c for c in body["items"] if c["id"] == str(item_id))
    assert item_candidate["board_id"] is None
    assert item_candidate["evidence"]["linked_task"]["status"] == "review"


@pytest.mark.asyncio
async def test_candidates_never_silently_skip_pages(env) -> None:
    # 5 tasks → more than one page at limit=2.
    for i in range(5):
        await _seed_review_task(env, title=f"t{i}")
    async with _client(env) as client:
        seen: list[str] = []
        offset = 0
        while True:
            resp = await client.get(
                f"/api/v1/review/candidates?limit=2&offset={offset}", headers=AUTH
            )
            body = resp.json()
            seen.extend(c["id"] for c in body["items"])
            offset += len(body["items"])
            if offset >= body["total"] or not body["items"]:
                break
    assert len(seen) == 5 and len(set(seen)) == 5


# ── decisions: gates ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_requires_autonomy_enabled(env) -> None:
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        resp = await client.post(
            "/api/v1/review/decisions", json=_decision_body(candidate), headers=AUTH
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_decision_rejects_unapproved_reviewer_and_empty_evidence(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        bad_engine = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(
                candidate,
                reviewer_provider="pi-neuralwatt",
                reviewer_model="glm-5.3-flash",
            ),
            headers=AUTH,
        )
        assert bad_engine.status_code == 422
        no_evidence = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, evidence="   "),
            headers=AUTH,
        )
        assert no_evidence.status_code == 422


@pytest.mark.asyncio
async def test_decision_stale_fingerprint_is_409(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        body = _decision_body(candidate, fingerprint="0" * 64)
        resp = await client.post("/api/v1/review/decisions", json=body, headers=AUTH)
        assert resp.status_code == 409
        # changed task → changed fingerprint → 409, never overridden
        async with env["maker"]() as session:
            db_task = await session.get(Task, task.id)
            db_task.description = "changed"
            await session.commit()
        resp = await client.post(
            "/api/v1/review/decisions", json=_decision_body(candidate), headers=AUTH
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_accept_requires_qa_pass_and_sha_binding(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env, qa_pass=False)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        resp = await client.post(
            "/api/v1/review/decisions", json=_decision_body(candidate), headers=AUTH
        )
        assert resp.status_code == 422
        assert "QA PASS" in resp.json()["detail"]

        task2 = await _seed_review_task(env, title="sha task")
        candidate2 = await _first_candidate(env, client, "task", str(task2.id))
        wrong_sha = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate2, published_sha="b" * 40),
            headers=AUTH,
        )
        assert wrong_sha.status_code == 422
        assert "published SHA" in wrong_sha.json()["detail"]


@pytest.mark.asyncio
async def test_accept_only_from_review(env) -> None:
    set_automation_enabled(True)
    # Non-review tasks are never acceptance candidates — the decision API
    # additionally 409s any accept whose task left Review.
    task = await _seed_review_task(env, status="in_progress")
    async with _client(env) as client:
        resp = await client.get("/api/v1/review/candidates", headers=AUTH)
        body = resp.json()
    assert str(task.id) not in [c["id"] for c in body["items"]]


# ── decisions: accept happy path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_creates_delegated_approval_and_goes_through_lifecycle(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        resp = await client.post(
            "/api/v1/review/decisions", json=_decision_body(candidate), headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True and body["action"] == "accept"
        assert body["task_id"] == str(task.id)
        assert body["updated_at"]

    task = await _get_task(env, task.id)
    assert task.status == "done"
    async with env["maker"]() as session:
        approvals = list(
            (await session.exec(select(Approval).where(col(Approval.task_id) == task.id))).all()
        )
        assert len(approvals) == 1
        payload = approvals[0].payload
        assert payload["source"] == "autonomous_review"
        assert payload["delegated"] is True
        assert payload["reviewer_model"] == "gpt-6.1-sol"
        assert payload["snapshot_fingerprint"] == candidate["fingerprint"]
        assert approvals[0].status == "approved"
        comments = list(
            (
                await session.exec(
                    select(ActivityEvent)
                    .where(
                        col(ActivityEvent.task_id) == task.id,
                        col(ActivityEvent.event_type) == "task.comment",
                    )
                    .order_by(col(ActivityEvent.created_at).desc())
                )
            ).all()
        )
    assert any("[auto-review] action=accept" in (c.message or "") for c in comments), [
        c.message for c in comments
    ]


@pytest.mark.asyncio
async def test_accept_major_requires_owner_approval_linked_to_snapshot(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env, title="major task")
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        refused = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, impact="major"),
            headers=AUTH,
        )
        assert refused.status_code == 422
        assert "owner approval" in refused.json()["detail"]

        # The model cannot invent the approval: the backend must be able to
        # verify one linked to the exact same snapshot fingerprint.
        async with env["maker"]() as session:
            session.add(
                Approval(
                    board_id=env["board_id"],
                    task_id=task.id,
                    action_type="owner_decision",
                    payload={
                        "source": "owner",
                        "goal_fingerprint": candidate["evidence"]["goal_fingerprint"],
                    },
                    confidence=100.0,
                    status="approved",
                )
            )
            await session.commit()
        candidate = await _first_candidate(env, client, "task", str(task.id))
        accepted = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, impact="major"),
            headers=AUTH,
        )
        assert accepted.status_code == 200
    assert (await _get_task(env, task.id)).status == "done"


# ── decisions: revise / escalate ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_revise_queues_bounded_correction_under_original_scope(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env, title="revise me")
    original_description = task.description
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        no_feedback = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, action="revise", feedback=None),
            headers=AUTH,
        )
        assert no_feedback.status_code == 422

        resp = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(
                candidate,
                action="revise",
                feedback="fix the failing check in backend/x.py",
            ),
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "revise"

    task = await _get_task(env, task.id)
    assert task.status == "inbox"  # requeued correction, not labelled done
    assert task.description.startswith(original_description)  # original scope preserved
    from app.models.team import TeamMember
    from app.services.team_cycle import brief

    assert "fix the failing check" in brief(
        task, TeamCycle(), TeamAttempt(role_kind="pm", stage_key="pm"), TeamMember(name="PM"), None
    )
    async with env["maker"]() as session:
        comments = list(
            (
                await session.exec(
                    select(ActivityEvent)
                    .where(col(ActivityEvent.task_id) == task.id)
                    .order_by(col(ActivityEvent.created_at).desc())
                )
            ).all()
        )
    assert any(
        "[auto-review] action=revise" in (c.message or "")
        and "fix the failing check" in (c.message or "")
        for c in comments
    )
    # NOT labelled done
    assert task.status != "done"


@pytest.mark.asyncio
async def test_revise_bound_reached_escalates(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env, title="bound me")
    async with env["maker"]() as session:
        for _ in range(MAX_AUTO_CORRECTIONS):
            session.add(
                ActivityEvent(
                    event_type="task.comment",
                    message="[auto-review] action=revise correction",
                    task_id=task.id,
                    board_id=env["board_id"],
                )
            )
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        resp = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, action="revise", feedback="another round"),
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "escalate"
        assert resp.json()["escalation_item_id"]
    async with env["maker"]() as session:
        escalations = list(
            (
                await session.exec(
                    select(OwnerReview).where(
                        col(OwnerReview.task_id) == task.id,
                        col(OwnerReview.submitted_by) == "Autonomous Review (delegated)",
                    )
                )
            ).all()
        )
    assert len(escalations) == 1
    assert escalations[0].status == "pending"
    assert (await _get_task(env, task.id)).status == "review"  # native pool cannot claim


@pytest.mark.asyncio
async def test_escalate_is_deduplicated_and_never_executes(env) -> None:
    set_automation_enabled(True)
    task = await _seed_review_task(env, title="escalate me")
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        for evidence_text in ("first look", "second look"):
            resp = await client.post(
                "/api/v1/review/decisions",
                json=_decision_body(
                    candidate,
                    action="escalate",
                    impact="major",
                    evidence=evidence_text,
                ),
                headers=AUTH,
            )
            assert resp.status_code == 200
            assert resp.json()["action"] == "escalate"
    async with env["maker"]() as session:
        rows = list(
            (
                await session.exec(
                    select(OwnerReview).where(
                        col(OwnerReview.task_id) == task.id,
                        col(OwnerReview.submitted_by) == "Autonomous Review (delegated)",
                    )
                )
            ).all()
        )
    assert len(rows) == 1
    assert "second look" in (rows[0].summary or "")
    # Task untouched: escalating never executes the proposed change.
    assert (await _get_task(env, task.id)).status == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["request_revision", "defer"])
async def test_reescalation_preserves_open_owner_decision(env, monkeypatch, action) -> None:
    async with env["maker"]() as session:
        if session.get_bind().dialect.name == "sqlite":
            from sqlalchemy import text
            await session.execute(text("ALTER TABLE owner_reviews ADD COLUMN snooze_until TIMESTAMP"))
            await session.commit()
    monkeypatch.setattr(review_api, "_SNOOZE_COLUMN", None)
    set_automation_enabled(True)
    task = await _seed_review_task(env, title="owner hold")
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", str(task.id))
        first = await client.post(
            "/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, action="escalate", impact="major", evidence="first"),
        )
        assert first.status_code == 200
        item_id = first.json()["escalation_item_id"]
        held = await client.patch(f"/api/v1/review/items/{item_id}", headers=AUTH, json={
            "action": action, "reviewer_note": "Keep original bounds and findings",
            "expected_status": "pending", "snooze_hours": 24,
        })
        assert held.status_code == 200
        before = (await client.get("/api/v1/review/items?include_snoozed=true", headers=AUTH)).json()[0]
        # The owner comment changes the acceptance fingerprint, never fake freshness.
        candidate = await _first_candidate(env, client, "task", str(task.id))
        again = await client.post(
            "/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, action="escalate", impact="major", evidence="second"),
        )
        assert again.status_code == 200
        assert again.json()["escalation_item_id"] == item_id
        items = (await client.get("/api/v1/review/items?include_snoozed=true", headers=AUTH)).json()
        assert len(items) == 1 and items[0] == before
        if action == "defer":
            assert before["snooze_until"] is not None
    assert (await _get_task(env, task.id)).status == "review"


# ── decisions: owner-queue items ────────────────────────────────────────────


async def _seed_watchdog_item(env, *, task_id=None, status="pending") -> OwnerReview:
    item_id = uuid4()
    async with env["maker"]() as session:
        session.add(
            OwnerReview(
                id=item_id,
                title="WATCHDOG: stale task",
                type="other",
                summary="stuck",
                reason="auto",
                proposed_action="x",
                submitted_by="Pipeline Watchdog",
                task_id=task_id,
                status=status,
            )
        )
        await session.commit()
    async with env["maker"]() as session:
        item = await session.get(OwnerReview, item_id)
        assert item is not None
        return item


@pytest.mark.asyncio
async def test_item_accept_refused(env) -> None:
    set_automation_enabled(True)
    item = await _seed_watchdog_item(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "item", str(item.id))
        resp = await client.post(
            "/api/v1/review/decisions", json=_decision_body(candidate), headers=AUTH
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_item_resolve_routine_and_confirmed_obsolete(env) -> None:
    set_automation_enabled(True)
    # Confirmed obsolete: the linked task no longer exists.
    item = await _seed_watchdog_item(env, task_id=uuid4())
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "item", str(item.id))
        major = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, action="resolve", impact="major"),
            headers=AUTH,
        )
        assert major.status_code == 422  # meaningful decisions stay human-visible
        resp = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, action="resolve"),
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "resolve"
        assert resp.json()["task_id"] == str(item.task_id)
        assert resp.json()["updated_at"]
    async with env["maker"]() as session:
        row = await session.get(OwnerReview, item.id)
        assert row is not None
        assert row.status == "resolved"  # never 'approved', never deleted
        assert "Delegated resolution" in (row.reviewer_note or "")


@pytest.mark.asyncio
async def test_item_stale_fingerprint_409_and_escalate_keeps_pending(env) -> None:
    set_automation_enabled(True)
    item = await _seed_watchdog_item(env, status="revision_requested")
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "item", str(item.id))
        stale = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, fingerprint="0" * 64, action="resolve"),
            headers=AUTH,
        )
        assert stale.status_code == 409
        resp = await client.post(
            "/api/v1/review/decisions",
            json=_decision_body(candidate, action="escalate", impact="major"),
            headers=AUTH,
        )
        assert resp.status_code == 200
    async with env["maker"]() as session:
        row = await session.get(OwnerReview, item.id)
        assert row is not None
        # stays human-visible
        assert row.status == "pending"
        assert "owner decision" in (row.reviewer_note or "")


# ── manual review item endpoints: gating + fencing ──────────────────────────


@pytest.mark.asyncio
async def test_patch_item_requires_admin_fences_and_resolve(env) -> None:
    task = await _seed_review_task(env)
    item = await _seed_watchdog_item(env, task_id=task.id)
    async with _client(env) as client:
        unauth = await client.patch(f"/api/v1/review/items/{item.id}", json={"action": "approve"})
        assert unauth.status_code == 401

        fenced = await client.patch(
            f"/api/v1/review/items/{item.id}",
            json={"action": "approve", "expected_status": "approved"},
            headers=AUTH,
        )
        assert fenced.status_code == 409

        override = await client.patch(
            f"/api/v1/review/items/{item.id}",
            json={"action": "approve", "status": "weird"},
            headers=AUTH,
        )
        assert override.status_code == 400

        resolved = await client.patch(
            f"/api/v1/review/items/{item.id}",
            json={"action": "resolve", "reviewer_note": "obsolete"},
            headers=AUTH,
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["task_id"] == str(task.id)
        assert resolved.json()["updated_at"]

    # resolve creates NO task approval (approve on watchdog items would)
    async with env["maker"]() as session:
        approvals = list(
            (await session.exec(select(Approval).where(col(Approval.task_id) == task.id))).all()
        )
    assert approvals == []


@pytest.mark.asyncio
async def test_manual_approve_still_creates_watchdog_approval(env) -> None:
    task = await _seed_review_task(env)
    item = await _seed_watchdog_item(env, task_id=task.id)
    async with _client(env) as client:
        resp = await client.patch(
            f"/api/v1/review/items/{item.id}", json={"action": "approve"}, headers=AUTH
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
    async with env["maker"]() as session:
        approvals = list(
            (await session.exec(select(Approval).where(col(Approval.task_id) == task.id))).all()
        )
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_delete_item_requires_admin(env) -> None:
    item = await _seed_watchdog_item(env)
    async with _client(env) as client:
        unauth = await client.delete(f"/api/v1/review/items/{item.id}")
        assert unauth.status_code == 401
        resp = await client.delete(f"/api/v1/review/items/{item.id}", headers=AUTH)
        assert resp.status_code == 204


# ── watchdog / approvals stand-down while autonomy is enabled ───────────────


def _source(path: str) -> str:
    return (ROOT / path).read_text()


def test_watchdog_confidence_paths_stand_down_when_automation_enabled() -> None:
    src = _source("backend/app/services/watchdog_loop.py")
    for fn in ("_auto_approve_high_confidence_approvals", "_auto_close_review_tasks"):
        body = src[src.index(f"async def {fn}(") :]
        body = body[: body.index("\nasync def ", 1)]
        assert "from app.services.owner_reviews import automation_enabled" in body
        assert "automation_enabled(org_id)" in body or "automation_enabled(row[4])" in body


def test_watchdog_stale_escalation_skips_live_team_cycles_and_published_review() -> None:
    src = _source("backend/app/services/watchdog_loop.py")
    body = src[src.index("async def _stale_tasks(") :]
    body = body[: body.index("\nasync def ", 1)]
    assert "team_cycles" in body
    assert "c.state = 'published' AND t.status = 'review'" in body
    # healthy = active state AND deadline not passed
    assert "c.deadline_at > NOW()" in body


def test_approvals_confidence_autoclose_gated_and_no_raw_update_fallback() -> None:
    src = _source("backend/app/api/approvals.py")
    assert "automation_enabled" in src
    assert "Fallback: raw UPDATE" not in src
    assert "UPDATE tasks SET status='done'" not in src


@pytest.mark.asyncio
async def test_auto_approve_high_confidence_stands_down_when_enabled(env, sidecar) -> None:
    from app.services import watchdog_loop as wl

    task = await _seed_review_task(env, status="review")
    async with env["maker"]() as session:
        session.add(
            Approval(
                board_id=env["board_id"],
                task_id=task.id,
                action_type="close_task",
                confidence=95.0,
                status="pending",
            )
        )
        await session.commit()
    set_automation_enabled(True)
    async with env["maker"]() as session:
        approved = await wl._auto_approve_high_confidence_approvals(session)
    assert approved == 0
    async with env["maker"]() as session:
        rows = list(
            (await session.exec(select(Approval).where(col(Approval.task_id) == task.id))).all()
        )
    assert rows[0].status == "pending"


def test_decisions_use_lifecycle_not_raw_update() -> None:
    src = _source("backend/app/api/review.py")
    assert "_apply_admin_task_rules" in src and "_finalize_updated_task" in src
    assert "UPDATE tasks" not in src


# ── standing reviewer script ────────────────────────────────────────────────


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "mc_review_agent", str(ROOT / "scripts/mc-review-agent.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mc_review_agent"] = module
    spec.loader.exec_module(module)
    return module


def test_parse_verdict_strict_json() -> None:
    script = _load_script()
    good = json.dumps(
        {
            "action": "accept",
            "impact": "routine",
            "evidence": "QA PASS on sha abc",
            "feedback": None,
        }
    )
    assert script.parse_verdict(f"thinking...\n```json\n{good}\n```") is None
    for invalid in (
        good + good,
        "Example only: " + good + " FAIL; do not accept.",
        json.dumps({"wrapper": json.loads(good)}),
        '{"action":"escalate","action":"accept","impact":"routine","evidence":"x"}',
        good[:-1] + ',"unknown":1}',
    ):
        assert script.parse_verdict(invalid) is None
    assert script.parse_verdict(good)["action"] == "accept"


def test_parse_verdict_rejects_thinking_only_empty_and_malformed() -> None:
    script = _load_script()
    assert script.parse_verdict("") is None
    assert script.parse_verdict("I think the task looks good.") is None
    assert script.parse_verdict('{"action": "accept"}') is None  # missing fields
    assert (
        script.parse_verdict('{"action": "destroy", "impact": "routine", "evidence": "x"}') is None
    )
    # revise without feedback is not a valid verdict
    assert (
        script.parse_verdict('{"action": "revise", "impact": "routine", "evidence": "x"}') is None
    )


def test_script_state_checkpoints_only_after_apply(tmp_path, monkeypatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "STATE_FILE", tmp_path / "state.json")
    state = script._load_state()
    script._mark_failed(state, "fp1", "api error")
    script._save_state(state)
    assert script._failure_pending(script._load_state(), "fp1") is True
    # a checkpoint clears the failure and records the fingerprint as
    # processed — only reached after a successful server apply in the script
    state = script._load_state()
    script._mark_processed(state, "fp1")
    script._save_state(state)
    assert script._failure_pending(script._load_state(), "fp1") is False
    assert "fp1" in script._load_state()["processed"]


def test_script_guards_present() -> None:
    src = _source("scripts/mc-review-agent.py")
    assert 'os.environ.get("RUNNER_APPLY", "0") != "1"' in src
    assert "fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" in src
    assert "--once" in src
    # checkpoints only after a successful server apply or a definitive 409
    assert "Checkpoint ONLY after a successful server apply" in src


def test_team_runner_has_canonical_opus_5_5_alias() -> None:
    src = _source("scripts/mc-team-runner.py")
    assert '"opus-5-5": "claude-opus-5-5"' in src


# ── standing reviewer script, end to end against a fake MC ───────────────────

_REVIEWER_CLI = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
Path(os.environ["FIXTURE_LOG"]).open("a").write(
    f"claude cwd={os.getcwd()} model={args[args.index('--model') + 1]}\\n"
)
assert "INDEPENDENT ACCEPTANCE REVIEWER" in prompt
assert '"kind": "task"' in prompt or '"kind"' in prompt
verdict = {
    "action": "accept",
    "impact": "routine",
    "evidence": "QA PASS verified on the published artifact; criteria met.",
}
print(json.dumps({"result": json.dumps(verdict), "usage": {"input_tokens": 10, "output_tokens": 5}}))
"""


@pytest.mark.asyncio
async def test_review_agent_once_applies_validated_verdict(tmp_path, monkeypatch) -> None:
    import socket
    import threading

    import uvicorn
    from fastapi import Body

    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap not installed")

    # One candidate: a review task without a published SHA (read-only repo
    # path), so the script reviews the current repo read-only.
    engine = await _make_engine()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(maker)
    org_id, gw_id, board_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with maker() as session:
        session.add(Organization(id=org_id, name="o"))
        await session.flush()
        session.add(
            Gateway(
                id=gw_id,
                organization_id=org_id,
                name="g",
                url="ws://x",
                workspace_root="/tmp/w",
            )
        )
        await session.flush()
        session.add(
            Board(
                id=board_id, organization_id=org_id, name="b", slug=f"b-{board_id.hex}"
            )
        )
        session.add(
            User(
                id=uuid4(),
                clerk_user_id="local-auth-user",
                email="admin@home.local",
                active_organization_id=org_id,
            )
        )
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=(await session.exec(select(User))).first().id,  # type: ignore[union-attr]
                role="owner",
            )
        )
        session.add(
            Task(
                id=task_id,
                board_id=board_id,
                title="script task",
                description="criteria",
                status="review",
                priority="medium",
            )
        )
        await session.commit()

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "sc.json")
    set_automation_enabled(True)

    applied: list[dict] = []
    usage_reports: list[dict] = []

    fake = FastAPI()

    @fake.get("/api/v1/review/automation")
    async def automation():
        return {
            "enabled": True,
            "models": [
                {
                    "provider": "claude-code",
                    "model": "opus-5-5",
                    "eligible": True,
                    "block_reason": None,
                }
            ],
            "policy": "p",
        }

    @fake.get("/api/v1/review/candidates")
    async def candidates(limit: int = 50, offset: int = 0):
        # Serve the REAL router's candidate shape by querying it once.
        if not hasattr(candidates, "_items"):  # type: ignore[attr-defined]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as client:
                resp = await client.get(
                    "/api/v1/review/candidates?limit=200", headers=AUTH
                )
                candidates._items = resp.json()["items"]  # type: ignore[attr-defined]
        items = candidates._items  # type: ignore[attr-defined]
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "limit": limit,
            "offset": offset,
        }

    @fake.post("/api/v1/review/tasks/{task_id}/review-lease")
    async def lease(task_id: str, body: dict = Body(...)):
        from fastapi.responses import JSONResponse
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/api/v1/review/tasks/{task_id}/review-lease", headers=AUTH, json=body)
            return JSONResponse(response.json(), status_code=response.status_code)

    @fake.post("/api/v1/review/tasks/{task_id}/review-lease/update")
    async def lease_update(task_id: str, body: dict = Body(...)):
        from fastapi.responses import JSONResponse
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/api/v1/review/tasks/{task_id}/review-lease/update", headers=AUTH, json=body)
            return JSONResponse(response.json(), status_code=response.status_code)

    @fake.post("/api/v1/ranking/usage")
    async def usage(body: dict = Body(...)):
        usage_reports.append(body)
        return {"ok": True}

    @fake.post("/api/v1/review/decisions")
    async def decisions(body: dict = Body(...)):
        applied.append(body)
        return {"ok": True, "kind": body["kind"], "id": body["id"], "action": body["action"]}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started

    binaries = tmp_path / "bin"
    binaries.mkdir()
    cli = binaries / "claude"
    cli.write_text(_REVIEWER_CLI)
    cli.chmod(0o755)

    state_file = tmp_path / "state.json"
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "MC_URL": f"http://127.0.0.1:{port}",
        "MC_TOKEN": "fake-token",
        "RUNNER_APPLY": "1",
        "RUNNER_LOG": str(tmp_path / "log"),
        "MC_REVIEW_STATE": str(state_file),
        "MC_REVIEW_LOCK": str(tmp_path / "agent.lock"),
        "FIXTURE_LOG": str(tmp_path / "fixture.log"),
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/mc-review-agent.py"),
            "--once",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The validated verdict was applied exactly once, through the decisions API.
    assert len(applied) == 1
    decision = applied[0]
    assert decision["action"] == "accept"
    assert decision["reviewer_provider"] == "claude-code"
    assert decision["reviewer_model"] == "opus-5-5"
    assert decision["evidence"]
    # Aggregate usage was reported (ledger accounting).
    assert usage_reports and usage_reports[0]["provider"] == "claude-code"
    # Checkpoint written only after the successful apply.
    state = json.loads(state_file.read_text())
    assert decision["fingerprint"] in state["processed"]
    # The model ran in its own session, separate from implementers.
    log_text = (tmp_path / "fixture.log").read_text()
    assert "cwd=" in log_text and "model=claude-opus-5-5" in log_text

    # A second --once pass does not re-review the unchanged candidate.
    proc2 = subprocess.run(
        [sys.executable, str(ROOT / "scripts/mc-review-agent.py"), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert len(applied) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_review_agent_disarmed_without_runner_apply(tmp_path) -> None:
    env = {
        **os.environ,
        "RUNNER_APPLY": "0",
        "RUNNER_LOG": str(tmp_path / "log"),
        "MC_REVIEW_STATE": str(tmp_path / "state.json"),
        "MC_REVIEW_LOCK": str(tmp_path / "agent.lock"),
    }
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/mc-review-agent.py"), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    log_text = (tmp_path / "log").read_text()
    assert "disarmed" in log_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["dependency", "pending", "audit", "disabled", "failed_qa", "wrong_qa_sha", "unpublished"],
)
async def test_acceptance_gates_are_atomic(env, monkeypatch, failure):
    """Independent QA repros: never claim accept or partially persist it."""
    from app.api import tasks as tasks_api
    from app.models.task_dependencies import TaskDependency

    set_automation_enabled(True)
    task = await _seed_review_task(
        env, published_sha=None if failure == "unpublished" else "a" * 40
    )
    async with env["maker"]() as session:
        if failure == "dependency":
            dep = Task(board_id=task.board_id, title="Unfinished", status="in_progress")
            session.add(dep)
            await session.flush()
            session.add(
                TaskDependency(board_id=task.board_id, task_id=task.id, depends_on_task_id=dep.id)
            )
        elif failure == "pending":
            board = await session.get(Board, task.board_id)
            board.block_status_changes_with_pending_approval = True
            session.add(
                Approval(
                    board_id=task.board_id,
                    task_id=task.id,
                    action_type="human_required",
                    confidence=90.0,
                    status="pending",
                )
            )
        elif failure in ("failed_qa", "wrong_qa_sha"):
            qa = (
                await session.exec(select(TeamAttempt).where(col(TeamAttempt.task_id) == task.id))
            ).first()
            if failure == "failed_qa":
                qa.status = "failed"
            else:
                qa.head_sha = "b" * 40
        await session.commit()

    async def fail(*args, **kwargs):
        raise RuntimeError("audit persistence failed")

    if failure == "audit":
        monkeypatch.setattr(tasks_api, "_record_task_comment_from_update", fail)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        if failure == "disabled":
            mc_sidecar.set_model_meta(*REVIEWER_MODELS[0], disabled=True)
        if failure == "audit":
            with pytest.raises(RuntimeError, match="audit persistence failed"):
                await client.post(
                    "/api/v1/review/decisions", headers=AUTH, json=_decision_body(candidate)
                )
        else:
            response = await client.post(
                "/api/v1/review/decisions", headers=AUTH, json=_decision_body(candidate)
            )
            assert response.status_code in (403, 409, 422), response.text
    assert (await _get_task(env, task.id)).status == "review"
    async with env["maker"]() as session:
        approvals = (
            await session.exec(select(Approval).where(col(Approval.task_id) == task.id))
        ).all()
        assert all(a.status != "approved" for a in approvals)


@pytest.mark.asyncio
async def test_human_evidence_and_authorized_goal_fencing(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        # A human may use our audit tag; it must still invalidate the verdict.
        async with env["maker"]() as session:
            session.add(
                ActivityEvent(
                    event_type="task.comment",
                    task_id=task.id,
                    message="[auto-review] OWNER: stop; regression found.",
                )
            )
            await session.commit()
        stale = await client.post(
            "/api/v1/review/decisions", headers=AUTH, json=_decision_body(candidate)
        )
        assert stale.status_code == 409
        candidate = await _first_candidate(env, client, "task", task.id)
        major_revision = await client.post(
            "/api/v1/review/decisions",
            headers=AUTH,
            json=_decision_body(
                candidate, action="revise", impact="major", feedback="New architecture"
            ),
        )
        assert major_revision.json()["action"] == "escalate"
        assert (await _get_task(env, task.id)).status == "review"
        item_id = major_revision.json()["escalation_item_id"]
        approved = await client.patch(
            f"/api/v1/review/items/{item_id}",
            headers=AUTH,
            json={"action": "approve", "reviewer_note": "Original goal is authorized."},
        )
        assert approved.status_code == 200
        fresh = await _first_candidate(env, client, "task", task.id)
        assert fresh["fingerprint"] != candidate["fingerprint"]
        # Delegation and a previous goal authorization never authorize a NEW goal.
        async with env["maker"]() as session:
            changed = await session.get(Task, task.id)
            changed.description += " New security authority."
            await session.commit()
        changed = await _first_candidate(env, client, "task", task.id)
        denied = await client.post(
            "/api/v1/review/decisions", headers=AUTH, json=_decision_body(changed, impact="major")
        )
        assert denied.status_code == 422
        async with env["maker"]() as session:
            original = await session.get(Task, task.id)
            original.description = task.description
            original.updated_at = utcnow()  # implementation time does not change goals
            await session.commit()
        settled = await _first_candidate(env, client, "task", task.id)
        accepted = await client.post(
            "/api/v1/review/decisions", headers=AUTH, json=_decision_body(settled, impact="major")
        )
        assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_manual_item_timestamp_and_self_checkpoint(env):
    set_automation_enabled(True)
    item = await _seed_watchdog_item(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "item", item.id)
        stale = await client.patch(
            f"/api/v1/review/items/{item.id}",
            headers=AUTH,
            json={"action": "approve", "expected_updated_at": "1900-01-01T00:00:00"},
        )
        assert stale.status_code == 409
        escalated = await client.post(
            "/api/v1/review/decisions",
            headers=AUTH,
            json=_decision_body(candidate, action="escalate", impact="major"),
        )
        assert escalated.status_code == 200
        fresh = await _first_candidate(env, client, "item", item.id)
        assert fresh["fingerprint"] == candidate["fingerprint"]
        human = await client.patch(
            f"/api/v1/review/items/{item.id}",
            headers=AUTH,
            json={
                "action": "defer",
                "reviewer_note": "Human evidence change",
                "expected_updated_at": fresh["evidence"]["updated_at"],
            },
        )
        assert human.status_code == 200
        assert (await _first_candidate(env, client, "item", item.id))["fingerprint"] != fresh[
            "fingerprint"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_queue_mutations_are_org_scoped(env, method):
    async with env["maker"]() as session:
        org = Organization(name="Other")
        session.add(org)
        await session.flush()
        board = Board(organization_id=org.id, name="Other", slug=str(uuid4()))
        session.add(board)
        await session.flush()
        task = Task(board_id=board.id, title="Other tenant", status="review")
        session.add(task)
        await session.commit()
    item = await _seed_watchdog_item(env, task_id=task.id)
    async with _client(env) as client:
        kwargs = {"json": {"action": "approve"}} if method == "patch" else {}
        response = await getattr(client, method)(
            f"/api/v1/review/items/{item.id}", headers=AUTH, **kwargs
        )
        assert response.status_code == 404
    async with env["maker"]() as session:
        assert (await session.get(OwnerReview, item.id)).status == "pending"


@pytest.mark.asyncio
async def test_escalation_approval_fences_changed_goal_and_revocation(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        escalated = await client.post("/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, action="escalate", impact="major"))
        item_id = escalated.json()["escalation_item_id"]
        async with env["maker"]() as session:
            changed = await session.get(Task, task.id)
            changed.description += " New goal."
            await session.commit()
        denied = await client.patch(f"/api/v1/review/items/{item_id}", headers=AUTH,
                                    json={"action": "approve"})
        assert denied.status_code == 409
        fresh = await _first_candidate(env, client, "task", task.id)
        repeated = await client.post("/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(fresh, action="escalate", impact="major"))
        assert repeated.json()["escalation_item_id"] == item_id  # deduplicated fresh goal
        assert (await client.patch(f"/api/v1/review/items/{item_id}", headers=AUTH,
                                  json={"action": "approve"})).status_code == 200
        assert (await client.patch(f"/api/v1/review/items/{item_id}", headers=AUTH,
                                  json={"action": "reject"})).status_code == 200
        reconsider = await _first_candidate(env, client, "task", task.id)
        revoked = await client.post("/api/v1/review/decisions", headers=AUTH,
                                    json=_decision_body(reconsider, impact="major"))
        assert revoked.status_code == 422


def test_new_reviewer_model_cannot_bypass_account_hold(sidecar):
    from app.services.model_ranking import seed_default_ranking

    mc_sidecar.set_model_meta(
        "openai-codex-2",
        "gpt-5.6-sol",
        unavailable_until="2999-01-01T00:00:00+00:00",
        availability_reason="quota_exhausted",
    )
    seed_default_ranking()
    assert not automation_models_status()[0]["eligible"]
    assert automation_models_status()[1]["eligible"]  # distinct account unaffected


@pytest.mark.parametrize("failure", ["harness", "malformed", "lifecycle", "stale"])
def test_same_candidate_fallback_and_409_checkpoints(tmp_path, monkeypatch, failure):
    script = _load_script()
    models = [{"provider": p, "model": m, "eligible": True} for p, m in REVIEWER_MODELS]
    calls = []
    commands = []
    cleanup = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(ROOT),
        "worktree",
        "remove",
        "--force",
        str(tmp_path / "wt"),
    ]
    monkeypatch.setattr(script, "_review_workspace", lambda *a: (ROOT, cleanup))
    monkeypatch.setattr(script.subprocess, "run", lambda cmd, **kw: commands.append(cmd))

    def harness(claim, *args):
        calls.append((claim["attempt"]["provider"], claim["attempt"]["model"]))
        if len(calls) == 1 and failure == "harness":
            return {"success": False, "text": "primary quota exhausted"}
        if len(calls) == 1 and failure == "malformed":
            return {"success": True, "text": "Thinking only"}
        return {
            "success": True,
            "text": json.dumps({"action": "escalate", "impact": "major", "evidence": "verified"}),
        }

    def api(method, path, *args):
        if method == "get":
            return {"enabled": True, "models": models}
        if path.endswith("/review-lease"):
            return {"lease_id": str(uuid4())}
        if path.endswith("/update"):
            return {"ok": True}
        if failure in ("lifecycle", "stale"):
            message = (
                "Pending approval blocks status change"
                if failure == "lifecycle"
                else "Candidate changed since the review"
            )
            raise script.team.ApiError(409, message)
        return {"ok": True, "action": "escalate"}

    monkeypatch.setattr(script.team, "run_harness", harness)
    monkeypatch.setattr(script.team, "api", api)
    state = {"processed": {}, "failed": {}}
    candidate = {
        "id": "a" * 32,
        "kind": "task",
        "status": "review",
        "fingerprint": "snapshot",
        "evidence": {},
    }
    result = script.review_candidate(candidate, models, set(), state)
    if failure == "lifecycle":
        assert result is None and not state["processed"]
    else:
        assert result == ("stale" if failure == "stale" else "escalate")
        assert "snapshot" in state["processed"]
    if failure in ("harness", "malformed"):
        assert calls == [REVIEWER_MODELS[0], REVIEWER_MODELS[1]]
    assert commands and all(cmd == cleanup for cmd in commands)  # one argv, not each argument


def test_published_workspace_uses_safe_git(tmp_path, monkeypatch):
    script = _load_script()
    commands = []
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda cmd, **kw: commands.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    candidate = {"evidence": {"cycle": {"published_sha": "a" * 40}}}
    workspace, cleanup = script._review_workspace(candidate, tmp_path)
    try:
        assert "a" * 40 in commands[0]
        assert all(
            "core.hooksPath=/dev/null" in cmd and "core.fsmonitor=false" in cmd
            for cmd in [commands[0], cleanup]
        )
    finally:
        shutil.rmtree(workspace)


@pytest.mark.parametrize("control", ["disabled", "signal"])
def test_active_review_cancellation_fences_result(tmp_path, monkeypatch, control):
    import signal

    script = _load_script()
    models = [{"provider": "claude-code", "model": "opus-5-5", "eligible": True}]
    monkeypatch.setattr(script, "_review_workspace", lambda *a: (ROOT, []))
    monkeypatch.setattr(script, "LOCK_FILE", str(tmp_path / "lock"))
    monkeypatch.setattr(script, "STATE_FILE", tmp_path / "state")
    monkeypatch.setattr(script, "LOG", str(tmp_path / "log"))
    monkeypatch.setattr(script.team, "git", lambda *a: "ok")
    monkeypatch.setenv("RUNNER_APPLY", "1")
    monkeypatch.setattr(sys, "argv", ["review", "--once"])
    posted = []
    state = {"processed": {}, "failed": {}}

    def api(method, path, *args):
        if method == "post":
            posted.append((path, *args))
            return {"lease_id": str(uuid4()), "ok": True}
        return {"enabled": True, "models": models}

    def harness(claim, prompt, cwd, tmp, timeout, cancelled):
        if control == "signal":
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        else:
            models[0]["eligible"] = False
        assert cancelled.wait(3), "active harness was not cancelled"
        return {
            "success": True,
            "text": '{"action":"accept","impact":"routine","evidence":"late result"}',
        }

    monkeypatch.setattr(script.team, "api", api)
    monkeypatch.setattr(script.team, "run_harness", harness)
    monkeypatch.setattr(
        script,
        "one_pass",
        lambda _: script.review_candidate(
            {
                "id": "a" * 32,
                "fingerprint": "snapshot",
                "kind": "task",
                "status": "review",
                "evidence": {},
            },
            models,
            set(),
            state,
        )
        or 0,
    )
    old_handlers = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]
    try:
        script.main()
        assert not any(path == "/api/v1/review/decisions" for path, *_ in posted)
        assert not state["processed"]
    finally:
        for sig, handler in zip((signal.SIGTERM, signal.SIGINT), old_handlers):
            signal.signal(sig, handler)
        script.team.STOP.clear()


def test_harness_cancellation_kills_surviving_child(tmp_path, monkeypatch):
    import threading
    import time

    team = _load_script().team
    team.STOP.clear()
    child_pid = tmp_path / "child.pid"
    program = (
        "import os,signal,time,pathlib; pid=os.fork(); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN) if pid==0 else None; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())) if pid==0 else None; "
        "time.sleep(60)"
    )
    monkeypatch.setattr(team, "harness_cmd", lambda *a: ([sys.executable, "-c", program], {}))
    monkeypatch.setattr(team, "sandbox_command", lambda argv, *a, **kw: argv)
    monkeypatch.setattr(team, "api", lambda *a: {"ok": True})
    cancelled = threading.Event()

    def cancel_when_child_ready():
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancelled.set()

    watcher = threading.Thread(target=cancel_when_child_ready)
    watcher.start()
    try:
        team.run_harness(
            {
                "role_kind": "reviewer",
                "attempt": {
                    "id": "test-process-group",
                    "provider": "claude-code",
                    "model": "opus-5-5",
                    "thinking_level": "low",
                },
            },
            "offline test",
            ROOT,
            tmp_path,
            8,
            cancelled,
        )
        assert child_pid.exists()
        pid = int(child_pid.read_text())
        for _ in range(100):
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("child survived process-group cancellation")
    finally:
        watcher.join(timeout=6)


@pytest.mark.asyncio
async def test_obsolete_pev_notice_resolves_without_reopening_task(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env, status="done", with_cycle=False)
    item = await _seed_watchdog_item(env, task_id=task.id, status="revision_requested")
    async with env["maker"]() as session:
        row = await session.get(OwnerReview, item.id)
        row.submitted_by = "PEV Loop"
        session.add(row)
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "item", item.id)
        response = await client.post(
            "/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, action="resolve"),
        )
        assert response.status_code == 200, response.text
    assert (await _get_task(env, task.id)).status == "done"
    async with env["maker"]() as session:
        assert (await session.get(OwnerReview, item.id)).status == "resolved"
        assert not (await session.exec(select(Approval))).all()


@pytest.mark.asyncio
async def test_legacy_unscoped_rows_hidden_and_immutable_across_tenants(env):
    item = await _seed_watchdog_item(env)
    own_task = await _seed_review_task(env)
    own_item = await _seed_watchdog_item(env, task_id=own_task.id)
    async with env["maker"]() as session:
        session.add(Organization(name="another tenant"))
        await session.commit()
    async with _client(env) as client:
        listed = await client.get("/api/v1/review/items?include_snoozed=true", headers=AUTH)
        assert listed.status_code == 200, listed.text
        assert str(item.id) not in [r["id"] for r in listed.json()]
        assert str(own_item.id) in [r["id"] for r in listed.json()]
        candidates = await client.get("/api/v1/review/candidates", headers=AUTH)
        assert str(item.id) not in [r["id"] for r in candidates.json()["items"]]
        for method in ("patch", "delete"):
            kwargs = {"json": {"action": "approve"}} if method == "patch" else {}
            response = await getattr(client, method)(
                f"/api/v1/review/items/{item.id}", headers=AUTH, **kwargs,
            )
            assert response.status_code == 404, response.text


def test_acceptance_prompt_does_not_reopen_authorized_goals():
    prompt = _load_script().PROMPT_HEADER
    assert "not-yet-authorized" in prompt
    assert "already-authorized ORIGINAL goal is routine" in prompt
    assert "untrusted" in prompt


@pytest.mark.asyncio
async def test_automation_control_is_tenant_scoped(env):
    from app.api.deps import OrganizationContext, require_org_member
    async with env["maker"]() as session:
        other = Organization(name="other tenant")
        session.add(other)
        await session.flush()
        member = OrganizationMember(organization_id=other.id, user_id=env["user_id"], role="owner")
        session.add(member)
        await session.commit()
        other_ctx = OrganizationContext(organization=other, member=member)
    async with _client(env) as client:
        enabled = await client.put("/api/v1/review/automation", headers=AUTH, json={"enabled": True})
        assert enabled.status_code == 200
        assert automation_enabled(env["org_id"])
        assert not automation_enabled(other.id)
        env["app"].dependency_overrides[require_org_member] = lambda: other_ctx
        assert not (await client.get("/api/v1/review/automation", headers=AUTH)).json()["enabled"]


@pytest.mark.asyncio
async def test_rolled_back_revision_never_sends_notification(env, monkeypatch):
    from app.api import tasks as tasks_api
    set_automation_enabled(True)
    task = await _seed_review_task(env, with_cycle=False)
    original = tasks_api._finalize_updated_task
    sent = []
    async def notify(*args, **kwargs):
        sent.append(True)
    async def pause_before_commit(*args, **kwargs):
        response = await original(*args, **kwargs)
        set_automation_enabled(False)
        return response
    monkeypatch.setattr(tasks_api, "_finalize_updated_task", pause_before_commit)
    monkeypatch.setattr(tasks_api, "_notify_task_update_assignment_changes", notify)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        result = await client.post("/api/v1/review/decisions", headers=AUTH,
                                   json=_decision_body(candidate, action="revise", feedback="Fix existing regression"))
    assert result.status_code == 403
    assert (await _get_task(env, task.id)).status == "review"
    assert not sent


@pytest.mark.asyncio
async def test_revision_notification_observes_committed_state(env, monkeypatch):
    from app.api import tasks as tasks_api
    set_automation_enabled(True)
    task = await _seed_review_task(env, with_cycle=False)
    seen = []
    async def notify(*args, **kwargs):
        seen.append((await _get_task(env, task.id)).status)
    monkeypatch.setattr(tasks_api, "_notify_task_update_assignment_changes", notify)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        result = await client.post("/api/v1/review/decisions", headers=AUTH,
                                   json=_decision_body(candidate, action="revise", feedback="Fix existing regression"))
    assert result.status_code == 200, result.text
    assert seen == ["inbox"]


def test_own_quota_report_still_falls_back_same_candidate(tmp_path, monkeypatch):
    import time
    script = _load_script()
    models = [{"provider": p, "model": m, "eligible": True} for p, m in REVIEWER_MODELS]
    monkeypatch.setattr(script, "_review_workspace", lambda *args: (ROOT, []))
    monkeypatch.setattr(script.team, "api", lambda method, *args:
                        {"enabled": True, "models": models} if method == "get" else
                        {"lease_id": str(uuid4()), "ok": True, "action": "escalate"})
    calls = []
    def harness(claim, prompt, cwd, tmp, timeout, cancelled):
        calls.append((claim["attempt"]["provider"], claim["attempt"]["model"]))
        if len(calls) == 1:
            models[0]["eligible"] = False
            time.sleep(1.3)
            return {"success": False, "text": "quota exhausted"}
        return {"success": True, "text": json.dumps({"action": "escalate", "impact": "major", "evidence": "checked"})}
    monkeypatch.setattr(script.team, "run_harness", harness)
    candidate = {"id": "a" * 32, "kind": "task", "status": "review", "fingerprint": "snap", "evidence": {}}
    assert script.review_candidate(candidate, models, set(), {"processed": {}, "failed": {}}) == "escalate"
    assert calls == [REVIEWER_MODELS[0], REVIEWER_MODELS[1]]


def test_acceptance_reviewer_reads_staged_whole_files_and_cleans_them(tmp_path, monkeypatch):
    script = _load_script()
    models = [{'provider': 'omniroute-codex', 'model': 'gpt-6.1-sol', 'eligible': True}]
    monkeypatch.setattr(script, '_review_workspace', lambda *args: (ROOT, []))
    monkeypatch.setattr(script.team, 'api', lambda method, *args:
        {'enabled': True, 'models': models} if method == 'get' else
        {'lease_id': str(uuid4()), 'ok': True, 'action': 'escalate'})
    files = {'owner-report.md': 'Authentic public report\r\nTrailing blank\n\n'}
    staged = []
    def harness(claim, prompt, cwd, scratch, timeout, cancelled):
        public = script.team.evidence_dir(scratch)
        staged.append(public)
        assert (public / 'owner-report.md').read_bytes() == files['owner-report.md'].encode()
        return {'success': True, 'text': json.dumps({'action': 'escalate',
            'impact': 'major', 'evidence': 'Actual complete file read'})}
    monkeypatch.setattr(script.team, 'run_harness', harness)
    candidate = {'id': 'b' * 32, 'kind': 'task', 'status': 'review',
        'fingerprint': 'whole-files', 'evidence': {'evidence_files': files}}
    assert script.review_candidate(candidate, models, set(), {'processed': {}, 'failed': {}}) == 'escalate'
    assert staged and not staged[0].exists()
    candidate['evidence']['evidence_files'] = {'../escape.md': 'invalid whole proof'}
    assert script.review_candidate(candidate, models, set(), {'processed': {}, 'failed': {}}) is None
    assert len(staged) == 1  # malformed whole packets refuse before inference


def test_runtime_setup_failure_reported_without_tokens(tmp_path, monkeypatch):
    import mc_pi_runtime
    script = _load_script()
    monkeypatch.delenv("FIXTURE_LOG", raising=False)
    monkeypatch.setattr(script.team, "harness_cmd", lambda *args: (["pi", "--provider", "neuralwatt", "--model", "glm-5.3"], {}))
    def missing(*args):
        raise RuntimeError("synthetic missing model")
    monkeypatch.setattr(mc_pi_runtime, "key_runtime", missing)
    reports = []
    monkeypatch.setattr(script.team, "api", lambda *args: reports.append(args) or {})
    result = script.team.run_harness({"role_kind": "reviewer", "attempt": {"id": "synthetic", "provider": "pi-neuralwatt", "model": "glm-5.3", "thinking_level": "high"}}, "offline", ROOT, tmp_path, 60)
    assert not result["success"] and not result["usage_observed"]
    assert reports and "input_tokens" not in reports[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["entry", "move", "usage"])
async def test_ranking_cannot_write_reserved_policy_namespace(env, endpoint):
    from app.api import model_ranking as ranking_api
    env["app"].include_router(ranking_api.router, prefix="/api/v1")
    set_automation_enabled(True)
    other = uuid4()
    model = f"owner_review:{other}"
    body = {"provider": "_mc_automation", "model": model}
    async with _client(env) as client:
        if endpoint == "entry":
            response = await client.put("/api/v1/ranking/entry", headers=AUTH, json={**body, "level": 1})
        elif endpoint == "move":
            response = await client.put("/api/v1/ranking", headers=AUTH, json={"moves": [{**body, "level": 1}]})
        else:
            response = await client.post("/api/v1/ranking/usage", headers=AUTH, json={**body, "success": False})
    assert response.status_code == 422, response.text
    assert mc_sidecar.get_model_meta("_mc_automation", model) == {}
    assert automation_enabled(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_evidence_revision_never_queues_work_or_spends_correction_round(env, explicit, monkeypatch):
    async def no_retry(*args, **kwargs):
        pytest.fail("acceptance feedback must not invoke QA recovery")
    monkeypatch.setattr(review_api, "_recover_task_evidence", no_retry)
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
        body = _decision_body(candidate, action="revise", feedback="Attach missing coordinator receipts.")
        if explicit:
            body["correction_kind"] = "evidence"
        else:
            body.pop("correction_kind")
        response = await client.post("/api/v1/review/decisions", headers=AUTH, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["correction_kind"] == "evidence"
        fresh = await _first_candidate(env, client, "task", task.id)
        assert fresh["fingerprint"] == response.json()["fingerprint"]
    unchanged = await _get_task(env, task.id)
    assert (unchanged.status, unchanged.description) == ("review", task.description)
    async with env["maker"]() as session:
        assert await review_api._count_delegated_revisions(session, task.id) == 0
        cycles = list(await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))
        assert len(cycles) == 1
        cycle = cycles[0]
        assert cycle.state == "published"
        assert cycle.reviewed_sha == cycle.published_sha == "a" * 40
        attempts = await review_api._cycle_attempts(session, cycle.id)
        assert len(attempts) == 2 and all(a.status == "succeeded" for a in attempts)
        assert next(a for a in attempts if a.role_kind == "qa").verdict == "pass"


@pytest.mark.asyncio
async def test_candidate_includes_separate_pm_manager_evidence(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        cycle = await review_api._latest_cycle(session, task.id)
        for stage in ("pm", "manage-docs"):
            session.add(TeamAttempt(
                cycle_id=cycle.id, board_id=env["board_id"], task_id=task.id,
                member_id=uuid4(), role_kind="pm", stage_key=stage,
                status="succeeded", provider="pi-neuralwatt", model="glm-5.3",
                artifact=f"Durable {stage} reply", assignment="read-only bounded module",
                started_at=utcnow(), finished_at=utcnow(),
            ))
        await session.commit()
    async with _client(env) as client:
        candidate = await _first_candidate(env, client, "task", task.id)
    roles = {a["stage_key"]: a for a in candidate["evidence"]["workers"]}
    assert roles["pm"]["id"] != roles["manage-docs"]["id"]
    assert roles["pm"]["artifact"] == "Durable pm reply"
    assert roles["manage-docs"]["assignment"] == "read-only bounded module"
    assert all(roles[s]["started_at"] and roles[s]["finished_at"] for s in ("pm", "manage-docs"))


@pytest.mark.asyncio
async def test_feedback_cannot_disguise_code_revision_to_bypass_ceiling(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with _client(env) as client:
        for index in range(MAX_AUTO_CORRECTIONS + 1):
            candidate = await _first_candidate(env, client, "task", task.id)
            response = await client.post("/api/v1/review/decisions", headers=AUTH, json=_decision_body(
                candidate, action="revise", correction_kind="code",
                feedback="Fix verified defect; do not use correction_kind=evidence for this code defect.",
            ))
            assert response.status_code == 200, response.text
            expected = "revise" if index < MAX_AUTO_CORRECTIONS else "escalate"
            assert response.json()["action"] == expected
            async with env["maker"]() as session:
                assert await review_api._count_delegated_revisions(session, task.id) == min(index + 1, MAX_AUTO_CORRECTIONS)
                stored = await session.get(Task, task.id)
                if index < MAX_AUTO_CORRECTIONS:
                    assert stored.status == "inbox"
                    stored.status = "review"  # simulate completed original-scope correction
                    await session.commit()
                else:
                    assert stored.status == "review"  # ceiling cannot leave runnable work


def test_revision_parser_requires_explicit_code_kind_to_queue_code():
    script = _load_script()
    body = {"action": "revise", "impact": "routine", "evidence": "Missing host receipt", "feedback": "Attach it."}
    assert script.parse_verdict(json.dumps(body))["correction_kind"] == "evidence"
    assert script.parse_verdict(json.dumps({**body, "correction_kind": "code"}))["correction_kind"] == "code"
    assert script.parse_verdict(json.dumps({**body, "correction_kind": True})) is None


@pytest.mark.parametrize("inflight_policy", [False, True])
def test_finished_reviewer_stops_heartbeats_before_waiting_for_publication(
    tmp_path, monkeypatch, inflight_policy
):
    import fcntl
    import threading
    import time

    script = _load_script()
    script.STOP.clear()
    lock = tmp_path / "publication.lock"
    monkeypatch.setattr(script.team, "GLOBAL_LOCK", str(lock))
    monkeypatch.setattr(script, "_review_workspace", lambda *args: (tmp_path, []))
    models = [{"provider": p, "model": m, "eligible": True} for p, m in REVIEWER_MODELS]
    finished, parked = threading.Event(), threading.Event()
    policy_entered, joining = threading.Event(), threading.Event()
    real_thread = threading.Thread

    class JoinBarrierThread(real_thread):
        def join(self, *args, **kwargs):
            joining.set()  # coordinator has already signalled completion
            return super().join(*args, **kwargs)

    monkeypatch.setattr(script.threading, "Thread", JoinBarrierThread)
    actions = []

    def api(method, path, body=None):
        if method == "get":
            if inflight_policy and threading.current_thread() is not threading.main_thread():
                policy_entered.set()
                assert finished.wait(3) and joining.wait(3)
            return {"enabled": True, "models": models}
        if path.endswith("/review-lease"):
            return {"lease_id": str(uuid4())}
        if path.endswith("/update"):
            actions.append((body["action"], finished.is_set()))
            if body["action"] == "finish":
                parked.set()
        return {"ok": True, "action": "escalate"}

    def harness(*args):
        if inflight_policy:
            assert policy_entered.wait(3)
        finished.set()
        return {"success": True, "text": json.dumps(
            {"action": "escalate", "impact": "major", "evidence": "fixture"})}

    monkeypatch.setattr(script.team, "api", api)
    monkeypatch.setattr(script.team, "run_harness", harness)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        def unlock():
            assert parked.wait(3)
            time.sleep(1.2)  # at least one real monitor tick during coordinator wait
            fcntl.flock(held, fcntl.LOCK_UN)
        thread = threading.Thread(target=unlock)
        thread.start()
        try:
            candidate = {"kind": "task", "id": str(uuid4()), "fingerprint": "b" * 64,
                         "status": "review", "evidence": {}}
            assert script.review_candidate(candidate, models, set(), {"processed": {}, "failed": {}}) == "escalate"
            assert ("finish", True) in actions
            assert ("release", True) in actions
            assert ("heartbeat", True) not in actions
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            thread.join(timeout=4)


def test_incomplete_legacy_policy_metadata_does_not_disable_review(sidecar):
    set_automation_enabled(True)
    other = uuid4()
    mc_sidecar.set_model_meta("_mc_automation", f"owner_review:{other}", level=1)
    assert automation_enabled(other)
    set_automation_enabled(False, other)
    assert not automation_enabled(other)


@pytest.mark.asyncio
async def test_task_linked_item_supersedes_older_and_lists_task_state(env) -> None:
    """2026-10-25: one paused task carried three pending items, none linked to it."""
    task = await _seed_review_task(env, title="stuck", status="paused")
    body = {"title": "MAJOR: x", "type": "task-failure", "summary": "s", "reason": "r",
            "proposed_action": "a", "submitted_by": "fleet-supervisor", "task_id": str(task.id)}
    async with _client(env) as client:
        first = (await client.post("/api/v1/review/items", headers=AUTH, json=body)).json()
        second = (await client.post("/api/v1/review/items", headers=AUTH, json={**body, "title": "MAJOR: y"})).json()
        items = {i["id"]: i for i in (await client.get("/api/v1/review/items?include_snoozed=true", headers=AUTH)).json()}
    assert items[first["id"]]["status"] == "resolved"
    live = items[second["id"]]
    assert live["status"] == "pending" and live["task_status"] == "paused"
    assert live["task_board_id"] == str(env["board_id"]) and live["task_id"] == str(task.id)
