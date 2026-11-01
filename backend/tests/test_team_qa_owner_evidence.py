"""Owner evidence reaches only QA; incomplete packets cannot spend recovery."""
import json

import pytest
from pydantic import ValidationError

from app.models.team import TeamMember
from app.schemas.team import TeamRetryQA
from app.services import team_cycle as svc
from tests.test_team_qa_recovery import published
from tests.test_team_verification import env


def test_owner_recovery_evidence_is_an_explicit_bounded_api_field():
    payload = TeamRetryQA(version=1, evidence_files={"receipt.json": "{}\n"})
    assert payload.model_dump()["evidence_files"] == {"receipt.json": "{}\n"}
    with pytest.raises(ValidationError):
        TeamRetryQA(version=1, evidence_files={f"p{i}.txt": "proof" for i in range(17)})


async def test_whole_owner_packet_reaches_only_new_qa_with_original_history(env):
    s, board, task, cycle = await published(env)
    history = await svc._attempts(s, cycle.id)
    prior = [(a.id, a.artifact, a.assignment, a.status) for a in history]
    task.tokens_used = 123
    files = {"external-receipt.json": '{"head":"' + cycle.integration_sha + '","deployment":false}\n'}
    await svc.retry_qa(s, cycle, cycle.version, evidence_files=files)
    after = await svc._attempts(s, cycle.id)
    assert len(after) == len(history) + 1 and after[-1].role_kind == "qa"
    assert [(a.id, a.artifact, a.assignment, a.status) for a in history] == prior
    assignment = json.loads(after[-1].assignment)
    assert assignment["qa_only_recovery"] is True
    assert files["external-receipt.json"] in assignment["owner_evidence"]
    member = await s.get(TeamMember, after[-1].member_id)
    brief = svc.brief(task, cycle, after[-1], member, None, history=after)
    assert files["external-receipt.json"] in brief
    assert "Owner-supplied supplemental evidence" in brief
    assert "not new authority" in brief and "Prior QA reports" in brief
    assert task.tokens_used == 123 and cycle.round == 1
    assert svc.attempt_capability("qa", assignment) == "read_only"


@pytest.mark.parametrize("files", [
    {"../outside.txt": "proof"},
    {"too-big.txt": "x" * svc.MAX_EVIDENCE_BYTES},
    {"bad.txt": "\ud800"},
    {f"p{i}.txt": "proof" for i in range(17)},
    {"env.sh": "LOCAL_AUTH_TOKEN=" + "unsafe-fixture-0123456789abcdef"},
])
async def test_incomplete_owner_evidence_refuses_without_queue_or_clock_renewal(env, files):
    s, board, task, cycle = await published(env)
    before = (cycle.state, cycle.version, cycle.deadline_at, task.status, task.tokens_used)
    history = await svc._attempts(s, cycle.id)
    with pytest.raises(svc.TeamError, match="whole owner evidence"):
        await svc.retry_qa(s, cycle, cycle.version, evidence_files=files)
    assert (cycle.state, cycle.version, cycle.deadline_at, task.status, task.tokens_used) == before
    assert [a.id for a in await svc._attempts(s, cycle.id)] == [a.id for a in history]
