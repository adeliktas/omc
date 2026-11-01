"""Every report grant needs qualified QA, even unmanaged cloud/pinned rosters."""
# ruff: noqa: F811 - imported pytest fixture names are injected as parameters
import copy
import json

import pytest

from app.services import team_cycle as svc
from app.services.owner_reviews import is_reviewer_engine
from tests.test_team_cycle import BASE, RANKING, entry
from tests.test_team_report_recovery import source_report
from tests.test_team_verification import claim, env  # noqa: F401


def report_ranking():
    ranking = copy.deepcopy(RANKING)
    ranking["levels"][0]["entries"].append(entry("omniroute-codex", "gpt-6.1-sol", provider_priority=100))
    return ranking


async def prepared(env, preqa, ranking, *, weak_pin=False):
    s, board, task, old, author, failure = await source_report(env, preqa=preqa)
    members = await svc._members(s, board.id)
    writer = next(m for m in members if m.role_kind == "developer")
    writer.created_by, writer.provider, writer.model = "admin", "deepseek-sub", "deepseek-flash"
    reviewer = next(m for m in members if m.role_kind == "qa")
    if weak_pin:
        reviewer.created_by, reviewer.provider, reviewer.model = "admin", "claude-code", "sonnet"
    new = await svc.resume_work(s, old, old.version, "report_evidence",
        failure.id if failure else None, ranking=ranking)
    task.status = "in_progress"
    worker, token = await claim(s, new)
    assert worker.provider != "vllm" and not await svc._managed_resource_board(s, new)
    await svc.complete_attempt(s, board, new, worker, token, ok=True,
        artifact="Actual corrected report and authentic new public probe", head_sha=BASE)
    return s, board, task, old, new, reviewer


@pytest.mark.parametrize("preqa", [False, True])
@pytest.mark.parametrize("case", ["qualified", "weak_pin", "missing", "held", "claim_drift", "result_drift", "refresh"])
async def test_report_qualification_at_every_shared_boundary(env, preqa, case):
    ranking = copy.deepcopy(RANKING) if case == "missing" else report_ranking()
    if case == "held": ranking["levels"][0]["entries"][-1]["unavailable_until"] = "9999-01-01T00:00:00+00:00"
    s, board, task, old, new, reviewer = await prepared(env, preqa, ranking, weak_pin=case == "weak_pin")
    if case in ("weak_pin", "missing", "held"):
        with pytest.raises(svc.TeamError):
            await svc.integrate(s, new, BASE, ranking=ranking)
        assert not any(a.role_kind == "qa" for a in await svc._attempts(s, new.id))
        if case == "weak_pin": assert (reviewer.created_by, reviewer.provider, reviewer.model) == ("admin", "claude-code", "sonnet")
        return
    await svc.integrate(s, new, BASE, ranking=ranking)
    queued = next(a for a in await svc._attempts(s, new.id) if a.role_kind == "qa")
    assert is_reviewer_engine(queued.provider, queued.model)
    if case == "claim_drift":
        queued.provider, queued.model = "claude-code", "sonnet"
        assert await svc.claim_next(s, new, base_sha=BASE) is None
        assert queued.status == "queued" and not queued.claim_count
        return
    qa, token = await claim(s, new)
    if case == "result_drift":
        qa.provider, qa.model = "claude-code", "sonnet"
        with pytest.raises(svc.TeamFenced, match="unqualified"):
            await svc.complete_attempt(s, board, new, qa, token, ok=True,
                artifact="Unqualified claimed PASS", verdict="pass", head_sha=BASE)
        assert new.reviewed_sha is None and new.state != "qa_passed"
        return
    await svc.complete_attempt(s, board, new, qa, token, ok=True,
        artifact="Whole report independently checked\nVERDICT: PASS", verdict="pass", head_sha=BASE)
    if case == "refresh":
        await svc.refresh_integration(s, new, "e" * 40, "e" * 40, new.version, ranking=ranking)
        assert json.loads(new.plan)["report_evidence_recovery"] is True
        queued = max((a for a in await svc._attempts(s, new.id) if a.role_kind == "qa"), key=lambda a: a.attempt_no)
        assert is_reviewer_engine(queued.provider, queued.model) and new.reviewed_sha is None
    else:
        assert new.state == "qa_passed" and new.reviewed_sha == BASE
    assert task.status != "done" and old.state == "blocked"  # no original acceptance fabricated
