"""A qualified pinned QA engine is reserved across transport/account aliases."""

from datetime import timedelta

from app.core.time import utcnow
from app.models.team import TeamCycle, TeamMember
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, env as env
from tests.test_team_qualified_qa import registry


async def queued(env, *, pinned=False, available=True):
    session, board, task = env
    cycle = TeamCycle(
        board_id=board.id,
        task_id=task.id,
        base_sha=BASE,
        task_fingerprint=svc.task_fingerprint(task),
        state="developing",
        deadline_at=utcnow() + timedelta(hours=1),
    )
    qa = TeamMember(
        board_id=board.id,
        name="team-qa",
        role_kind="qa",
        provider="omniroute-codex",
        model="gpt-6.1-sol",
        created_by="admin",
    )
    writer = TeamMember(
        board_id=board.id,
        name="team-developer",
        role_kind="developer",
        provider="openai-codex-2",
        model="gpt-6.1-sol",
        created_by="admin" if pinned else "pool",
    )
    session.add_all([cycle, qa, writer])
    await session.flush()
    ranking = registry()
    for row in ranking["levels"][0]["entries"]:
        if row["provider"] not in ("openai-codex-2", "omniroute-codex"):
            row["eligible"] = available and row["provider"] == "claude-code"
    attempt = await svc._new_attempt(session, cycle, writer, "writer", ranking=ranking)
    await session.commit()
    return session, cycle, writer, attempt, ranking


async def test_automatic_writer_never_consumes_reserved_engine_at_creation_or_reselection(env):
    session, cycle, _, attempt, ranking = await queued(env)
    assert attempt.provider == "claude-code"
    # Old queued selection does not bypass the claim-time reservation.
    attempt.provider, attempt.model = "openai-codex-2", "gpt-6.1-sol"
    await session.commit()
    claimed = await svc.claim_next(session, cycle, base_sha=BASE, ranking=ranking)
    assert claimed and claimed[0].provider == "claude-code"


async def test_only_preferred_reviewer_engine_does_not_starve_writer(env):
    session, cycle, _, attempt, ranking = await queued(env, available=False)
    got = await svc.claim_next(session, cycle, base_sha=BASE, ranking=ranking)
    assert got and attempt.status == "running" and attempt.claim_count == 1
    assert svc.engine_identity(attempt.provider, attempt.model) == ("openai-codex", "gpt-6.1-sol")


async def test_owner_writer_pin_is_preserved_with_separate_qa_session_policy(env):
    session, cycle, member, attempt, ranking = await queued(env, pinned=True)
    got = await svc.claim_next(session, cycle, base_sha=BASE, ranking=ranking)
    assert got and got[0].provider == "openai-codex-2"
    assert member.created_by == "admin" and member.provider == "openai-codex-2"
    assert attempt.status == "running" and attempt.claim_count == 1


async def test_manager_can_use_reserved_engine_without_becoming_a_source_author(env):
    session, cycle, _, _, ranking = await queued(env)
    manager = TeamMember(
        board_id=cycle.board_id,
        name="team-manager-backend",
        role_kind="pm",
        provider="openai-codex-2",
        model="gpt-6.1-sol",
        created_by="pool",
    )
    session.add(manager)
    await session.flush()
    attempt = await svc._new_attempt(session, cycle, manager, "manager", ranking=ranking)
    assert svc.engine_identity(attempt.provider, attempt.model) == ("openai-codex", "gpt-6.1-sol")


def test_pool_qualification_is_exact_and_does_not_admit_flash_or_other_models():
    from app.services.owner_reviews import is_reviewer_engine

    assert is_reviewer_engine("omniroute-codex", "gpt-6.1-sol")
    assert not is_reviewer_engine("omniroute-codex", "gpt-5.6-sol")
    assert not is_reviewer_engine("pi-neuralwatt", "glm-5.3-flash")
    assert svc.engine_identity("omniroute-codex", "gpt-6.1-sol") == svc.engine_identity(
        "openai-codex-2", "gpt-6.1-sol"
    )
