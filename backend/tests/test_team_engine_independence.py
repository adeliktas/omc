"""QA engine independence is checked from actual attempts at every effect boundary."""

import json

import pytest

from app.api import review
from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING, entry
from tests.test_team_verification import VERIFY_PLAN, _planned, claim
from tests.test_team_verification import env as _env

env = _env


async def prepared(env, *, same_model_pin=False):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    writer, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        writer,
        token,
        ok=True,
        artifact="Checked fixture behavior.",
        head_sha=BASE,
        changed_files=[],
    )
    if same_model_pin:
        writer.provider, writer.model = "claude-code", "opus-5-5"
        member = await svc._qa_member(session, cycle, [writer])
        member.created_by = "admin"
        member.provider, member.model = writer.provider, writer.model
    await svc.integrate(session, cycle, BASE)
    qa = next(a for a in await svc._attempts(session, cycle.id) if a.role_kind == "qa")
    return session, board, task, cycle, writer, qa


async def test_qualified_same_model_pin_uses_a_separate_session_without_override(env):
    # An authorized pin must precede admission; editing a queued member now fences it.
    session, board, _, cycle, writer, qa = await prepared(env, same_model_pin=True)
    from tests.test_team_qualified_qa import registry

    member = await session.get(TeamMember, qa.member_id)
    got = await svc.claim_next(session, cycle, base_sha=BASE, ranking=registry())
    assert got and svc.qa_is_independent(got[0], [writer], cycle)
    assert qa.member_id != writer.member_id and qa.id != writer.id
    assert (member.provider, member.model) == (writer.provider, writer.model)
    assert json.loads(qa.assignment)["independent_model"] is False
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        got[1],
        ok=True,
        artifact="Fixture separate-session review.\nVERDICT: PASS",
        head_sha=BASE,
        verdict="pass",
    )
    assert qa.status == "succeeded" and cycle.state == "qa_passed"


@pytest.mark.parametrize(
    "author,grader",
    [
        (("claude-code", "claude-opus-5-5"), ("claude-code", "opus-5-5")),
        (("claude-code-p", "claude-opus-5-5"), ("claude-code", "opus-5-5")),
        (("openai-codex-1", "gpt-6.1-sol"), ("openai-codex-2", "gpt-6.1-sol")),
        (("", "unknown"), ("pi-neuralwatt", "glm-5.3")),
        ((" \t", "unknown"), ("pi-neuralwatt", "glm-5.3")),
        (("claude-code", " \n"), ("pi-neuralwatt", "glm-5.3")),
        (("claude-code", "claude-"), ("pi-neuralwatt", "glm-5.3")),
    ],
)
async def test_aliases_and_unknown_author_identity_cannot_prove_independence(env, author, grader):
    _, _, _, cycle, writer, qa = await prepared(env)
    writer.provider, writer.model = author
    qa.provider, qa.model = grader
    assert not svc.qa_is_independent(qa, [writer], cycle)


async def test_old_round_author_is_not_independent_after_a_correction(env):
    session, _, _, cycle, writer, qa = await prepared(env)
    cycle.round = qa.round = 2
    qa.provider, qa.model = writer.provider, writer.model
    assert not svc.qa_is_independent(qa, [writer], cycle)


async def test_qa_alias_cannot_escape_claim_time_author_exclusion(env):
    from tests.test_team_qualified_qa import registry

    session, _, _, cycle, writer, qa = await prepared(env)
    writer.provider, writer.model = "claude-code", "claude-opus-5-5"
    ranking = registry()
    for row in ranking["levels"][0]["entries"]:
        row["eligible"] = svc.engine_identity(row["provider"], row["model"]) == ("claude-code", "opus-5-5")
    got = await svc.claim_next(session, cycle, base_sha=BASE, ranking=ranking)
    assert got and svc.qa_is_independent(got[0], [writer], cycle)
    assert json.loads(qa.assignment)["independent_model"] is False


async def test_only_qualified_author_engine_uses_separate_session(env):
    session, _, _, cycle, writer, qa = await prepared(env)
    writer.provider, writer.model = "pi-neuralwatt", "glm-5.3"
    ranking = {**RANKING, "levels": [{"entries": [entry(writer.provider, writer.model)]}]}
    got = await svc.claim_next(session, cycle, base_sha=BASE, ranking=ranking)
    assert got and svc.qa_is_independent(got[0], [writer], cycle)
    assert json.loads(qa.assignment)["independent_session"] is True
    assert json.loads(qa.assignment)["independent_model"] is False
    qa.member_id = writer.member_id
    assert not svc.qa_is_independent(qa, [writer], cycle)


async def test_actual_engine_rechecked_at_completion_not_assignment_claim(env):
    session, board, _, cycle, writer, _ = await prepared(env)
    qa, token = await claim(session, cycle)
    qa.provider, qa.model = writer.provider, writer.model
    qa.assignment = json.dumps({**json.loads(qa.assignment or "{}"), "independent_model": True})
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="Fixture verdict.\nVERDICT: PASS",
        head_sha=BASE,
        verdict="pass",
    )
    assert qa.status == "failed" and qa.verdict is None
    assert cycle.state == "blocked" and cycle.round == 1 and not cycle.published_sha
    assert len([a for a in await svc._attempts(session, cycle.id) if a.role_kind == "qa"]) == 1


async def test_legacy_self_review_cannot_authorize_done_from_snapshot_metadata(env):
    session, _, task, cycle, writer, qa = await prepared(env)
    cycle.state = "published"
    cycle.reviewed_sha = cycle.published_sha = BASE
    qa.status, qa.verdict, qa.head_sha = "succeeded", "pass", BASE
    qa.provider, qa.model = writer.provider, writer.model
    qa.assignment = json.dumps({"independent_model": True, "artifact_complete": True})
    qa.artifact = "Fixture independent QA report.\nVERDICT: PASS"
    snapshot = {
        "cycle": {"state": "published", "published_sha": BASE, "reviewed_sha": BASE},
        "qa_attempts": [{"status": "succeeded", "verdict": "pass", "head_sha": BASE}],
    }
    assert not await review._qa_pass_evidence(session, task, snapshot)
    qa.provider, qa.model = "pi-neuralwatt", "glm-5.3"  # fixture author is not full GLM
    assert svc.qa_is_independent(qa, [writer], cycle)
    assert await review._qa_pass_evidence(session, task, snapshot)
    report = qa.artifact
    qa.artifact = ""
    assert not await review._qa_pass_evidence(session, task, snapshot)
    qa.artifact = report
    assignment = qa.assignment
    for invalid in ("{}", "null", "[]", "invalid", json.dumps({"artifact_complete": False})):
        qa.assignment = invalid
        assert not await review._qa_pass_evidence(session, task, snapshot)
    qa.assignment = assignment
    old_goal = task.description
    task.description = "New goal after the reviewed cycle"
    assert not await review._qa_pass_evidence(session, task, snapshot)
    task.description = old_goal
    fingerprint = cycle.task_fingerprint
    cycle.task_fingerprint = None
    assert not await review._qa_pass_evidence(session, task, snapshot)
    cycle.task_fingerprint = fingerprint
    cycle.integration_sha = "f" * 40
    assert not await review._qa_pass_evidence(session, task, snapshot)
    cycle.integration_sha = BASE
    qa.provider, qa.model = "deepseek-sub", "deepseek-flash"
    assert not await review._qa_pass_evidence(session, task, snapshot)
    qa.provider, qa.model = "pi-neuralwatt", "glm-5.3"
    writer.status = "failed"
    assert not await review._qa_pass_evidence(session, task, snapshot)
