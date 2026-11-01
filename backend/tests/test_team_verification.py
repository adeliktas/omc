"""Evidence-only (verify) team attempts: truthful no-change verification path.

A plan worker may opt into mode="verify": the developer/specialist keeps its
durable identity but runs read-only at the exact base, and a successful attempt
reports head==base, no changed files and substantive evidence. Normal
implementation attempts still require an actual scoped change.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity_events import ActivityEvent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, DEV_SHA, RANKING

INTEGRATED = "d" * 40
VERIFY_PLAN = {
    "summary": "verify only",
    "qa_checks": ["existing tests"],
    "workers": [
        {
            "key": "api",
            "role": "developer",
            "mode": "verify",
            "title": "verify api",
            "scope": ["backend/app"],
            "instructions": "confirm existing behavior",
        }
    ],
}
IMPLEMENT_PLAN = {
    "summary": "implement",
    "qa_checks": [],
    "workers": [
        {
            "key": "api",
            "role": "developer",
            "title": "api",
            "scope": ["backend/app"],
            "instructions": "do",
        }
    ],
}
EVIDENCE = "ran `pytest backend/tests/test_x.py`: 3 passed; requested behavior already present"


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(id=uuid4(), name="o")
        session.add(org)
        gw = Gateway(
            id=uuid4(),
            organization_id=org.id,
            name="g",
            url="https://g.local",
            workspace_root="/tmp/w",
        )
        session.add(gw)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id)
        session.add(board)
        task = Task(
            id=uuid4(),
            board_id=board.id,
            title="audit",
            status="in_progress",
            execution_lane="harness",
            execution_mode="team",
            complexity=3,
        )
        session.add(task)
        await session.commit()
        yield session, board, task
    await engine.dispose()


async def claim(session, cycle, base=BASE):
    got = await svc.claim_next(session, cycle, base_sha=base)
    assert got
    await session.commit()
    return got


async def _planned(session, board, task, plan):
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        pm,
        tok,
        ok=True,
        artifact="```json\n" + json.dumps(plan) + "\n```",
        ranking=RANKING,
    )
    await session.commit()
    return cycle


async def comments(session):
    return [e.message for e in (await session.exec(select(ActivityEvent)))]


# ── plan parsing: explicit opt-in, backward-compatible default ──────────────


def test_parse_plan_accepts_verify_mode_and_defaults_to_implement():
    out = svc.parse_plan(json.dumps(VERIFY_PLAN))
    assert out["workers"][0]["mode"] == "verify"
    out = svc.parse_plan(json.dumps(IMPLEMENT_PLAN))
    assert out["workers"][0]["mode"] == "implement"


@pytest.mark.parametrize(
    "mode",
    ["exec", "VERIFY", 1, None, ["verify"], "", "write"],
)
def test_parse_plan_rejects_unknown_mode_values_or_types(mode):
    plan = {**VERIFY_PLAN, "workers": [{**VERIFY_PLAN["workers"][0], "mode": mode}]}
    with pytest.raises(svc.TeamError):
        svc.parse_plan(json.dumps(plan))


def test_parse_plan_rejects_verify_mode_for_managers():
    plan = {
        "workers": [
            {
                "key": "m",
                "role": "manager:module-lead",
                "mode": "verify",
                "scope": ["backend"],
                "instructions": "x",
            }
        ]
    }
    with pytest.raises(svc.TeamError):
        svc.parse_plan(json.dumps(plan))


# ── effective capability: derived server-side, narrowing only ───────────────


def test_attempt_capability_only_narrows_for_verify_assignments():
    assert svc.attempt_capability("developer", {"mode": "verify"}) == "read_only"
    assert svc.attempt_capability("specialist", {"mode": "verify"}) == "read_only"
    assert svc.attempt_capability("developer", None) == "write_worktree"
    assert svc.attempt_capability("developer", {}) == "write_worktree"
    assert svc.attempt_capability("developer", {"mode": "implement"}) == "write_worktree"
    assert svc.attempt_capability("developer", '{"mode": "verify"}') == "read_only"
    assert svc.attempt_capability("developer", "not json") == "write_worktree"
    # read-only roles can never be widened by assignment input
    for kind in ("pm", "qa"):
        assert (
            svc.attempt_capability(kind, {"mode": "verify"})
            == svc.attempt_capability(kind, None)
            == "read_only"
        )
    assert svc.attempt_capability("coordinator", None) == "orchestrate"
    assert svc.attempt_capability("coordinator", {"mode": "verify"}) == "orchestrate"


# ── verify attempts succeed only with exact base, no files, real evidence ────


async def test_verify_success_at_exact_base_with_no_files_and_evidence(env):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    a, tok = await claim(session, cycle)
    assert a.role_kind == "developer"
    assert svc.attempt_capability(a.role_kind, a.assignment) == "read_only"
    member = await session.get(TeamMember, a.member_id)
    assert member.capability == "write_worktree"  # durable role capability unchanged
    verifier_brief = svc.brief(task, cycle, a, member, None)
    assert "VERIFIER" in verifier_brief and svc.CHECK_ENVIRONMENT in verifier_brief
    assert "Do not edit, create, delete, stage or commit repository files" in verifier_brief
    assert "Never save reports/probes under /tmp/mc-" in verifier_brief
    await svc.complete_attempt(
        session,
        board,
        cycle,
        a,
        tok,
        ok=True,
        artifact=EVIDENCE,
        head_sha=BASE,
        changed_files=[],
        evidence_files={"probe.py": "assert 1 + 1 == 2\n"},
    )
    assert a.status == "succeeded" and a.head_sha == BASE
    assert "### /tmp/probe.py" in a.artifact and "assert 1 + 1 == 2" in a.artifact
    assert json.loads(a.changed_files) == []
    # all-no-change round: integration is the base itself, QA runs on that exact SHA
    await svc.integrate(session, cycle, BASE)
    assert cycle.integration_sha == BASE and cycle.state == "qa"
    qa, qt = await claim(session, cycle)
    history = await svc._attempts(session, cycle.id)
    brief = svc.brief(
        task, cycle, qa, await session.get(TeamMember, qa.member_id), None, history=history
    )
    assert '"capability": "read_only"' in brief  # QA sees the verifier was fenced
    assert svc.CHECK_ENVIRONMENT in brief  # QA knows where pytest is and recreates probes
    assert "assert 1 + 1 == 2" in brief  # the verifier's /tmp probe reaches QA verbatim
    assert qa.member_id != a.member_id  # QA stays a distinct member from verifiers
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        qt,
        ok=True,
        artifact="checked base: behavior holds",
        head_sha=BASE,
        verdict="pass",
    )
    await svc.reserve_publication(session, cycle, BASE, cycle.version)
    await svc.complete_publication(session, cycle, BASE, cycle.version, recurring=False)
    assert cycle.state == "published" and cycle.published_sha == BASE
    assert task.status == "review"  # never done
    assert "no code change was published" in "\n".join(await comments(session))


async def test_normal_developer_same_base_is_blocked_without_replay(env):
    session, board, task = env
    cycle = await _planned(session, board, task, IMPLEMENT_PLAN)
    a, tok = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        a,
        tok,
        ok=True,
        artifact="seems fine already",
        head_sha=BASE,
        changed_files=[],
    )
    assert a.status == "failed" and "must commit a change" in a.error
    assert cycle.state == "blocked" and cycle.published_sha is None
    assert "explicit mode=verify" in cycle.block_reason
    assert "still needs implementation" in cycle.block_reason
    attempts = await svc._attempts(session, cycle.id)
    implementation = [a for a in attempts if a.role_kind == "developer"]
    assert len(implementation) == 1
    assert implementation[0].artifact == "seems fine already"
    assert await svc.claim_next(session, cycle, base_sha=BASE) is None


async def test_implementation_missing_result_metadata_keeps_existing_retry_handling(env):
    session, board, task = env
    cycle = await _planned(session, board, task, IMPLEMENT_PLAN)
    attempt, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        attempt,
        token,
        ok=True,
        artifact="report",
        head_sha=BASE,
        changed_files=None,
    )
    assert attempt.status == "failed"
    assert cycle.state != "blocked"
    retry, _ = await claim(session, cycle)
    assert retry.attempt_no == 2


@pytest.mark.parametrize(
    ("head", "files", "artifact", "expected"),
    [
        (DEV_SHA, ["backend/app/x.py"], EVIDENCE, "exact assigned base head unchanged"),
        (BASE, ["backend/app/x.py"], EVIDENCE, "must not change files"),
        (BASE, [], "  ", "substantive evidence"),
        (None, [], EVIDENCE, "exact assigned base head unchanged"),
        ("not-a-sha", [], EVIDENCE, "exact assigned base head unchanged"),
    ],
)
async def test_verify_wrong_head_changed_files_or_empty_artifact_rejected(
    env, head, files, artifact, expected
):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    a, tok = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        a,
        tok,
        ok=True,
        artifact=artifact,
        head_sha=head,
        changed_files=files,
    )
    assert a.status == "failed" and expected in a.error, a.error
    retry, _ = await claim(session, cycle)
    assert retry.attempt_no == 2  # failed verification is retried, not published
    assert cycle.published_sha is None


async def test_failed_verification_cannot_publish(env):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    a, tok = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        a,
        tok,
        ok=False,
        error="could not run checks",
    )
    assert a.status == "failed"
    retry, rtok = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        retry,
        rtok,
        ok=True,
        artifact="",
        head_sha=BASE,
        changed_files=[],
    )
    assert retry.status == "failed"  # empty evidence is not a verification
    assert cycle.state == "blocked" and cycle.published_sha is None
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(session, cycle, BASE, cycle.version)
    assert task.status == "in_progress"


async def test_qa_fail_on_a_verify_only_plan_replans_instead_of_replaying_the_check(env):
    """0000a00a: a verifier reported the outcome missing, QA passed that stop and the unchanged
    base became Done. QA now fails it, and a read-only plan stops for a replan (pool rescope)."""
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    a, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, a, tok, ok=True, artifact="gate not satisfied: policy doc missing",
                               head_sha=BASE, changed_files=[])
    await svc.integrate(session, cycle, BASE)
    qa, qt = await claim(session, cycle)
    assert "An empty change (integration equals base) completes the task only when" in svc.brief(
        task, cycle, qa, await session.get(TeamMember, qa.member_id), None, history=await svc._attempts(session, cycle.id))
    await svc.complete_attempt(session, board, cycle, qa, qt, ok=True, head_sha=BASE, verdict="fail",
                               artifact="policy doc missing\nCORRECTION_KIND: CODE\nVERDICT: FAIL")
    assert cycle.state == "blocked" and cycle.round == 1
    assert "not retryable unchanged" in cycle.block_reason and cycle.published_sha is None
