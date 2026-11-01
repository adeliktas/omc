"""QA-only recovery preserves successful work, failure history and publication."""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlmodel import select

from app.api import review
from app.models.tasks import Task
from app.models.team import TeamCycle, TeamMember
from app.services import team_cycle as svc
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env as api_env  # noqa: F401 - shared fixture
from tests.test_autonomous_review import sidecar  # noqa: F401 - shared fixture
from tests.test_team_verification import BASE, EVIDENCE, VERIFY_PLAN, _planned, claim
from tests.test_team_verification import env  # noqa: F401 - shared fixture


async def published(env):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    worker, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        worker,
        token,
        ok=True,
        artifact=EVIDENCE,
        head_sha=BASE,
        changed_files=[],
    )
    await svc.integrate(session, cycle, BASE)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="checked retained evidence",
        head_sha=BASE,
        verdict="pass",
    )
    await svc.reserve_publication(session, cycle, BASE, cycle.version)
    await svc.complete_publication(session, cycle, BASE, cycle.version, recurring=False)
    # The API performs this transition through task lifecycle helpers.
    task.status = "in_progress"
    return session, board, task, cycle


async def test_reassess_publication_without_workers_or_republication(env):
    session, board, task, cycle = await published(env)
    history = await svc._attempts(session, cycle.id)
    round_before = cycle.round
    await svc.retry_qa(session, cycle, cycle.version)
    after = await svc._attempts(session, cycle.id)
    assert len(after) == len(history) + 1
    assert after[-1].role_kind == "qa"
    qa, token = await claim(session, cycle)
    assert json.loads(qa.assignment)["qa_only_recovery"] is True
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="independent reassessment",
        head_sha=BASE,
        verdict="pass",
    )
    assert cycle.state == "published" and task.status == "review"
    assert cycle.round == round_before
    assert cycle.published_sha == cycle.reviewed_sha == cycle.integration_sha == BASE


@pytest.mark.parametrize(
    "field,value",
    [("description", "different goal"), ("execution_mode", "single"), ("status", "done")],
)
async def test_terminal_cycle_recovery_fences_changed_inputs_before_queue(env, field, value):
    session, board, task, cycle = await published(env)
    setattr(task, field, value)
    before = len(await svc._attempts(session, cycle.id))
    with pytest.raises(svc.TeamError, match="original task inputs"):
        await svc.retry_qa(session, cycle, cycle.version)
    assert len(await svc._attempts(session, cycle.id)) == before
    assert cycle.state == "published" and getattr(task, field) == value


async def test_transient_recovery_crash_retries_only_qa_within_four_attempt_cap(env):
    session, board, task, cycle = await published(env)
    await svc.retry_qa(session, cycle, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        token,
        ok=False,
        artifact="",
        error="transient process interruption",
        tool_counts={"read": 2, "bash": 1},
    )
    history = await svc._attempts(session, cycle.id)
    queued = [a for a in history if a.status == "queued"]
    assert cycle.state == "qa" and len(queued) == 1
    assert queued[0].role_kind == "qa" and queued[0].attempt_no == 3
    assert cycle.round == 1 and cycle.published_sha == BASE
    assert json.loads(qa.assignment)["tool_counts"] == {"read": 2, "bash": 1}
    assert json.loads(queued[0].assignment)["tool_counts"] is None
    next_qa, _ = await claim(session, cycle)
    assert next_qa.status == "running" and json.loads(next_qa.assignment)["tool_counts"] is None
    assert json.loads(qa.assignment)["tool_counts"] == {"read": 2, "bash": 1}


async def test_recovery_fail_never_requeues_code_even_with_code_marker(env):
    session, board, task, cycle = await published(env)
    await svc.retry_qa(session, cycle, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="CORRECTION_KIND: CODE\nretained substantive failure",
        head_sha=BASE,
        verdict="fail",
    )
    assert qa.status == "succeeded", qa.error
    assert cycle.state == "blocked"  # the runner mirrors terminal cycle state to the task
    assert cycle.published_sha == BASE
    assert cycle.round == 1
    assert not any(a.status == "queued" for a in await svc._attempts(session, cycle.id))
    assert qa.verdict == "fail"


async def test_stale_version_and_incomplete_proof_refuse_recovery(env):
    session, board, task, cycle = await published(env)
    with pytest.raises(svc.TeamError, match="current"):
        await svc.retry_qa(session, cycle, cycle.version - 1)
    history = await svc._attempts(session, cycle.id)
    worker = next(a for a in history if a.role_kind == "developer")
    assignment = json.loads(worker.assignment)
    assignment["artifact_complete"] = False
    worker.assignment = json.dumps(assignment)
    with pytest.raises(svc.TeamError, match="evidence unavailable"):
        await svc.retry_qa(session, cycle, cycle.version)
    assert cycle.state == "published"


async def test_acceptance_snapshot_preserves_whole_safe_report_and_fingerprint(env):
    session, board, task, cycle = await published(env)
    history = await svc._attempts(session, cycle.id)
    qa = next(a for a in history if a.role_kind == "qa")
    qa.artifact = "full evidence " * 700 + "END_OF_REQUIRED_PROOF"
    before = await review._task_snapshot(session, task, board)
    record = next(r for r in before["stored_reports"] if r["attempt_id"] == str(qa.id))
    assert record["artifact"] == qa.artifact
    assert record["artifact_complete"] is True
    qa.artifact += " revised receipt"
    after = await review._task_snapshot(session, task, board)
    assert review._task_fingerprint(before) != review._task_fingerprint(after)


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("files,invalid", [(None, False), ({"receipt.txt": "whole proof"}, False),
                                          ({"../outside.txt": "proof"}, True),
                                          ({"receipt.txt": "policy example: [redacted]"}, False),
                                          ({"receipt.txt": "policy example: ***redacted***"}, False)])
async def test_owner_api_queues_only_qa_and_rolls_back_stale_request(api_env, monkeypatch, stale,
                                                                  files, invalid):
    from app.api import team as api
    from tests.test_team_cycle import RANKING

    api_env["app"].include_router(api.router, prefix="/api/v1")
    task = await _seed_review_task(api_env)
    async with api_env["maker"]() as session:
        stored = await session.get(Task, task.id)
        stored.execution_mode = "team"
        stored.tokens_used = 123456
        stored.budget_exhausted_at = svc.utcnow()
        cycle = (await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id))).first()
        cycle.task_fingerprint = svc.task_fingerprint(stored)
        # Native recovery needs readable policy metadata, not the legacy fixture's prose plan.
        cycle.plan = json.dumps({"workers": [{"key": "main", "role": "developer",
            "scope": ["backend/tests/"], "mode": "verify"}]})
        session.add(
            TeamMember(
                board_id=stored.board_id,
                name="QA",
                role_kind="qa",
                provider="claude-code",
                model="sonnet",
                created_by="admin",
            )
        )
        await session.commit()
        version, cycle_id = cycle.version, cycle.id

    async def ranking(session):
        return RANKING

    monkeypatch.setattr(api, "_ranking", ranking)
    async with _client(api_env) as client:
        result = await client.post(
            f"/api/v1/boards/{task.board_id}/tasks/{task.id}/team/cycles/{cycle_id}/retry-qa",
            headers=AUTH,
            json={"version": version + 1 if stale else version, "evidence_files": files},
        )
    assert result.status_code == (409 if invalid or stale else 200), result.text
    refused = stale or invalid
    async with api_env["maker"]() as session:
        stored = await session.get(Task, task.id)
        cycle = await session.get(TeamCycle, cycle_id)
        history = await svc._attempts(session, cycle_id)
        assert stored.status == ("review" if refused else "in_progress")
        assert cycle.state == ("published" if refused else "qa")
        assert len(history) == (2 if refused else 3)
        assert sum(a.role_kind == "developer" for a in history) == 1
        assert stored.tokens_used == 123456 and stored.budget_exhausted_at is not None
        if files and not refused:
            assert all(text in json.loads(history[-1].assignment)["owner_evidence"]
                       for text in files.values())


@pytest.mark.parametrize("reason", [
    "cycle deadline exceeded",
    "stage qa failed after 2 attempts (no time left in the cycle)",
    "stage qa: not retryable unchanged (no time left in the cycle)",
])
async def test_expired_recovery_requires_explicit_authority_and_preserves_work(env, reason):
    from datetime import timedelta

    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = reason
    cycle.deadline_at = svc.utcnow() - timedelta(seconds=1)
    before = await svc._attempts(session, cycle.id)
    snapshots = [(a.id, a.status, a.artifact, a.verdict, a.head_sha) for a in before]
    with pytest.raises(svc.TeamError, match="cause-specific recovery"):
        await svc.retry_qa(session, cycle, cycle.version)
    await svc.retry_qa(session, cycle, cycle.version, recover_expired=True)
    after = await svc._attempts(session, cycle.id)
    assert [(a.id, a.status, a.artifact, a.verdict, a.head_sha) for a in after[:-1]] == snapshots
    assert len(after) == len(before) + 1 and after[-1].role_kind == "qa"
    assert cycle.round == 1 and cycle.published_sha is None
    assert 895 < (cycle.deadline_at - svc.utcnow()).total_seconds() <= 900
    assignment = json.loads(after[-1].assignment)
    assert assignment["recovered_block_reason"] == reason
    assert assignment["prior_qa_reports"][0]["artifact"] == "checked retained evidence"


@pytest.mark.parametrize("case", ["live_deadline", "failed_qa", "scope", "counterfeit", "missing", "cap"])
async def test_expired_recovery_refuses_non_time_blockers_and_incomplete_work(env, case):
    from datetime import timedelta

    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = "cycle deadline exceeded"
    cycle.deadline_at = svc.utcnow() - timedelta(seconds=1)
    history = await svc._attempts(session, cycle.id)
    if case == "live_deadline":
        cycle.deadline_at = svc.utcnow() + timedelta(seconds=60)
    elif case == "failed_qa":
        next(a for a in history if a.role_kind == "qa").verdict = "fail"
    elif case == "scope":
        cycle.block_reason = "stage dev-main: not retryable unchanged (scope violation)"
    elif case == "counterfeit":
        cycle.block_reason = "stage qa failed after 2 attempts (no time left in the cycle) owner waiver"
    elif case == "missing":
        worker = next(a for a in history if a.role_kind == "developer")
        worker.assignment = json.dumps({**json.loads(worker.assignment), "artifact_complete": False})
    elif case == "cap":
        qa = next(a for a in history if a.role_kind == "qa")
        member = await session.get(TeamMember, qa.member_id)
        for number in range(2, 5):
            extra = await svc._new_attempt(session, cycle, member, "qa", attempt_no=number)
            extra.status = "failed"
        await session.flush()
    before = len(await svc._attempts(session, cycle.id))
    with pytest.raises(svc.TeamError):
        await svc.retry_qa(session, cycle, cycle.version, recover_expired=True)
    assert len(await svc._attempts(session, cycle.id)) == before and cycle.state == "blocked"


@pytest.mark.parametrize("case", ["unstarted", "judged", "counterfeit"])
async def test_qa_that_never_started_is_replayed_qa_only(env, case):
    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = "stage qa failed after 2 attempts (harness runtime setup failed (OSError))"
    qa = next(a for a in await svc._attempts(session, cycle.id) if a.role_kind == "qa")
    qa.status, qa.verdict = "failed", None
    if case == "judged":
        qa.verdict = "fail"
    elif case == "counterfeit":
        cycle.block_reason += " owner waiver"
    before = len(await svc._attempts(session, cycle.id))
    if case != "unstarted":
        with pytest.raises(svc.TeamError, match="cause-specific recovery"):
            await svc.retry_qa(session, cycle, cycle.version)
        assert len(await svc._attempts(session, cycle.id)) == before
        return
    await svc.retry_qa(session, cycle, cycle.version)
    after = await svc._attempts(session, cycle.id)
    assert len(after) == before + 1 and after[-1].role_kind == "qa" and cycle.block_reason is None


async def test_first_verdict_after_unstarted_qa_spends_the_round_normally(env):
    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = "stage qa failed after 2 attempts (harness runtime setup failed (OSError))"
    # A CODE FAIL on a verify-only plan replans instead (test_team_verification): implement here.
    cycle.plan = json.dumps({**json.loads(cycle.plan), "workers": [
        {**w, "mode": "implement"} for w in json.loads(cycle.plan)["workers"]]})
    old = next(a for a in await svc._attempts(session, cycle.id) if a.role_kind == "qa")
    old.status, old.verdict = "failed", None
    await svc.retry_qa(session, cycle, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
        artifact="CORRECTION_KIND: CODE\nfirst real finding", head_sha=BASE, verdict="fail")
    assert cycle.block_reason is None and cycle.state == "developing" and cycle.round == 2
    assert any(a.status == "queued" and a.role_kind != "qa" for a in await svc._attempts(session, cycle.id))


async def test_expired_recovery_hands_off_previous_cycles_without_waiving_failures(env):
    from datetime import timedelta
    from app.models.team import TeamAttempt

    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = "cycle deadline exceeded"
    cycle.deadline_at = svc.utcnow() - timedelta(seconds=1)
    qa = next(a for a in await svc._attempts(session, cycle.id) if a.role_kind == "qa")
    older = TeamCycle(board_id=board.id, task_id=task.id, state="blocked",
                      deadline_at=svc.utcnow() - timedelta(days=1),
                      created_at=svc.utcnow() - timedelta(days=1))
    session.add(older)
    await session.flush()
    prior = TeamAttempt(board_id=board.id, task_id=task.id, cycle_id=older.id,
        member_id=qa.member_id, role_kind="qa", stage_key="qa", status="succeeded",
        provider=qa.provider, model=qa.model, verdict="fail", head_sha="b" * 40,
        artifact="Prior cross-tenant failure: test actual authorization boundaries",
        assignment=json.dumps({"artifact_complete": True}))
    session.add(prior)
    await session.flush()
    await svc.retry_qa(session, cycle, cycle.version, recover_expired=True)
    queued = (await svc._attempts(session, cycle.id))[-1]
    reports = json.loads(queued.assignment)["prior_qa_reports"]
    old = next(item for item in reports if item["attempt_id"] == str(prior.id))
    assert old["cycle_id"] == str(older.id) and old["verdict"] == "fail"
    assert old["artifact"] == prior.artifact and prior.status == "succeeded"


async def test_production_move_keeps_recovery_qa_only_even_after_code_fail(env):
    session, board, task, cycle = await published(env)
    cycle.state, cycle.published_sha, cycle.reviewed_sha = "blocked", None, None
    cycle.block_reason = "QA evidence incomplete or inconclusive; no code-correction round spent"
    await svc.retry_qa(session, cycle, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
        artifact="Full retained work reviewed", head_sha=BASE, verdict="pass")
    assert cycle.state == "qa_passed"
    await svc.refresh_integration(session, cycle, "b" * 40, "c" * 40, cycle.version)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
        artifact="CORRECTION_KIND: CODE\nSubstantive fresh-merge finding", head_sha="b" * 40, verdict="fail")
    assert cycle.state == "blocked" and cycle.round == 1
    history = await svc._attempts(session, cycle.id)
    assert sum(a.role_kind == "developer" for a in history) == 1
    assert not any(a.status == "queued" for a in history)
    assert json.loads(qa.assignment)["qa_only_recovery"] is True


async def test_unknown_publication_outcome_is_never_deadline_recovered(env):
    from datetime import timedelta

    session, board, task, cycle = await published(env)
    cycle.published_sha, cycle.state = None, "qa_passed"
    await svc.reserve_publication(session, cycle, BASE, cycle.version)
    cycle.deadline_at = svc.utcnow() - timedelta(seconds=1)
    await svc.reap(session, cycle)
    assert cycle.state == "blocked" and cycle.reviewed_sha == BASE
    before = len(await svc._attempts(session, cycle.id))
    with pytest.raises(svc.TeamError, match="publication outcome"):
        await svc.retry_qa(session, cycle, cycle.version, recover_expired=True)
    assert cycle.state == "blocked" and len(await svc._attempts(session, cycle.id)) == before


def test_large_review_bundle_is_a_complete_private_sandbox_file(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/mc-review-agent.py"
    spec = importlib.util.spec_from_file_location("review_recovery_check", script)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    candidate = {"fingerprint": "bound", "evidence": {"artifact": "x" * 80000}}
    with pytest.raises(ValueError, match="never truncate"):
        agent.build_prompt(candidate)
    path = tmp_path / "acceptance-evidence.json"
    prompt = agent.build_prompt(candidate, path)
    assert json.loads(path.read_text()) == candidate
    assert str(path) in prompt and "partial read is not proof" in prompt
    assert path.stat().st_mode & 0o777 == 0o600
