"""One owner report-correction grant; original successful work is never replayed."""
# ruff: noqa: F811 - imported pytest fixture names are injected as parameters
import json
from uuid import uuid4

import pytest
from sqlmodel import select

from app.schemas.team import TeamResumeWork
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING, entry
from tests.test_team_verification import VERIFY_PLAN, _planned, claim, env  # noqa: F401


async def source_report(env, *, preqa=False):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact="Original substantive report; required probe file was cleaned.", head_sha=BASE)
    if preqa:
        await svc._block(s, cycle, "evidence unavailable: dev-api artifact completeness unknown")
        failure = None
    else:
        await svc.integrate(s, cycle, BASE)
        failure, token = await claim(s, cycle)
        failure.provider, failure.model = "omniroute-codex", "gpt-6.1-sol"
        await svc.complete_attempt(s, board, cycle, failure, token, ok=True,
            artifact="VERDICT: FAIL\nCORRECTION_KIND: EVIDENCE\nF1 missing original probe; retain finding.",
            verdict="fail", head_sha=BASE)
    task.status = "blocked"
    await s.commit()
    return s, board, task, cycle, worker, failure


@pytest.mark.parametrize("preqa", [False, True])
async def test_report_grant_keeps_old_history_and_requires_new_qa(env, preqa):
    assert TeamResumeWork(version=1, cause="report_evidence").cause == "report_evidence"
    s, board, task, old, worker, failure = await source_report(env, preqa=preqa)
    before = old.model_dump()
    attempts = {a.id: a.model_dump() for a in await svc._attempts(s, old.id)}
    task.tokens_used = 123
    import copy
    ranking = copy.deepcopy(RANKING)
    ranking["levels"][0]["entries"].append(entry("omniroute-codex", "gpt-6.1-sol", provider_priority=100))
    new = await svc.resume_work(s, old, old.version, "report_evidence",
        failure.id if failure else None, ranking=ranking)
    assert old.model_dump() == before
    assert {a.id: a.model_dump() for a in await svc._attempts(s, old.id)} == attempts
    assert new.id != old.id and new.round == new.max_rounds == 1
    policy = json.loads(new.plan)
    assert policy.pop("report_evidence_recovery") is True and policy == json.loads(old.plan)
    assert task.tokens_used == 123 and 1190 < (new.deadline_at - svc.utcnow()).total_seconds() <= 1200
    from app.api.review import _count_delegated_revisions
    assert await _count_delegated_revisions(s, task.id) == 0
    fresh = (await svc._attempts(s, new.id))[0]
    assignment = json.loads(fresh.assignment)
    assert assignment["mode"] == "verify" and assignment["scope"] == VERIFY_PLAN["workers"][0]["scope"]
    assert assignment["owner_work_recovery"]["source_cycle_id"] == str(old.id)
    assert worker.artifact in new.qa_feedback
    assert "report-only" in assignment["instructions"]
    task.status = "in_progress"
    fresh, token = await claim(s, new)
    await svc.complete_attempt(s, board, new, fresh, token, ok=True,
        artifact="Actual new report and attached new probe", head_sha=BASE,
        evidence_files={"new-probe.txt": "Authentic new proof"})
    await svc.integrate(s, new, BASE, ranking=ranking)
    qa, token = await claim(s, new)
    from app.services.owner_reviews import is_reviewer_engine
    assert is_reviewer_engine(qa.provider, qa.model)
    member = await s.get(svc.TeamMember, qa.member_id)
    brief = svc.brief(task, new, qa, member, None, history=await svc._attempts(s, new.id))
    assert json.dumps(worker.artifact) in brief
    if failure: assert json.dumps(failure.artifact) in brief
    await svc.complete_attempt(s, board, new, qa, token, ok=True,
        artifact="VERDICT: FAIL\nCORRECTION_KIND: EVIDENCE\nStill missing proof.",
        verdict="fail", head_sha=BASE)
    task.status = "blocked"
    assert new.state == "blocked" and new.reviewed_sha is None and task.tokens_used >= 123
    with pytest.raises(svc.TeamError, match="already used"):
        await svc.resume_work(s, new, new.version, "report_evidence", qa.id, ranking=RANKING)


@pytest.mark.parametrize("change", ["code", "unqualified", "partial_qa", "wrong_qa", "writer",
    "scope", "partial_worker", "missing_assignment", "empty_worker", "wrong_head",
    "paused", "version", "running", "published"])
async def test_report_grant_refuses_missing_evidence_and_wrong_authority(env, change):
    s, board, task, old, worker, failure = await source_report(env)
    version, aid = old.version, failure.id
    if change == "code": failure.artifact = failure.artifact.replace("EVIDENCE", "CODE")
    elif change == "unqualified": failure.provider, failure.model = "vllm", "qwen"
    elif change == "partial_qa": failure.assignment = json.dumps({"artifact_complete": False})
    elif change == "wrong_qa": aid = uuid4()
    elif change in ("writer", "scope"):
        p = json.loads(old.plan)
        if change == "writer": p["workers"][0]["mode"] = "implement"
        else: p["workers"][0]["scope"] = ["frontend/"]
        old.plan = json.dumps(p)
    elif change == "partial_worker": worker.assignment = json.dumps({**json.loads(worker.assignment), "artifact_complete": False})
    elif change == "missing_assignment": worker.assignment = None
    elif change == "empty_worker": worker.artifact = " \n"
    elif change == "wrong_head": worker.head_sha = "e" * 40
    elif change == "paused": task.status = "paused"
    elif change == "version": version -= 1
    elif change == "running": failure.status = "running"
    elif change == "published": old.published_sha = BASE
    count = len((await s.exec(select(svc.TeamCycle))).all())
    with pytest.raises(svc.TeamError):
        await svc.resume_work(s, old, version, "report_evidence", aid, ranking=RANKING)
    assert len((await s.exec(select(svc.TeamCycle))).all()) == count
