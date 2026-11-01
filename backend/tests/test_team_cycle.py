"""Native team cycles: policy, fencing, independent QA and reviewed publication."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamMember
from app.services import team_cycle as svc
from sqlmodel import select

BASE = "a" * 40
DEV_SHA = "b" * 40
DEV2_SHA = "c" * 40
INTEGRATED = "d" * 40


def entry(provider, model, level=1, **kw):
    return {
        "provider": provider, "model": model, "level": level, "rank": 0, "provider_priority": 0,
        "max_complexity": 5, "execution": "agent_harness", "eligible": True, "disabled": False,
        "cost_class": "free", **kw,
    }


RANKING = {
    "max_level_for_complexity": {"1": 5, "2": 4, "3": 3, "4": 2, "5": 1},
    "levels": [{"level": 1, "entries": [
        entry("claude-code", "sonnet"),
        entry("deepseek-sub", "deepseek-flash", provider_priority=1),
        entry("claude-code", "opus-4-8", disabled=True),
        entry("openai-codex-1", "gpt-5.6-sol"),
        entry("pi-neuralwatt", "glm-5.3-flash", cost_class="credits"),
        entry("ollama-cloud-sub", "mystery-alias"),
        entry("deepseek-sub", "ineligible", eligible=False),
        {**entry("some-endpoint", "m"), "execution": "model_endpoint"},
    ]}],
}
PLAN = {"summary": "s", "qa_checks": ["run tests"], "workers": [
    {"key": "api", "role": "developer", "title": "API", "scope": ["backend/app"], "instructions": "do"},
    {"key": "ui", "role": "specialist:devops-engineer", "title": "UI", "scope": ["frontend"], "instructions": "do"},
]}


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org, gw = Organization(id=uuid4(), name="o"), None
        session.add(org)
        gw = Gateway(id=uuid4(), organization_id=org.id, name="g", url="https://g.local", workspace_root="/tmp/w")
        session.add(gw)
        board = Board(id=uuid4(), organization_id=org.id, name="b", slug="b", gateway_id=gw.id)
        session.add(board)
        task = Task(id=uuid4(), board_id=board.id, title="audit", status="in_progress",
                    execution_lane="harness", execution_mode="team", complexity=3, repeat_enabled=False)
        session.add(task)
        await session.commit()
        yield session, board, task
    await engine.dispose()


async def claim(session, cycle, base=BASE):
    got = await svc.claim_next(session, cycle, base_sha=base, ranking=RANKING)
    assert got
    await session.commit()
    return got


async def to_qa(session, board, task, plan=PLAN):
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact="```json\n" + json.dumps(plan) + "\n```", ranking=RANKING)
    devs = []
    for sha, files in ((DEV_SHA, ["backend/app/x.py"]), (DEV2_SHA, ["frontend/a.ts"])):
        a, t = await claim(session, cycle)
        await svc.complete_attempt(session, board, cycle, a, t, ok=True, artifact="done", head_sha=sha, changed_files=files)
        devs.append(a)
    await svc.integrate(session, cycle, INTEGRATED)
    await session.commit()
    return cycle, devs


async def comments(session):
    return [e.message for e in (await session.exec(select(ActivityEvent)))]


# ── engine policy / recruitment ─────────────────────────────────────────────


@pytest.mark.parametrize("prov,model", [
    ("claude-code", "nope"), ("claude-code", "opus-4-8"), ("openai-codex-1", "gpt-5.6-sol"),
    ("ollama-cloud-sub", "mystery-alias"), ("deepseek-sub", "ineligible"), ("some-endpoint", "m"),
])
def test_unregistered_disabled_or_unsupported_engines_are_rejected(prov, model):
    with pytest.raises(svc.TeamError):
        svc.validate_engine(RANKING, prov, model)


def test_owner_allows_paid_engines_but_lg_pi_never():
    svc.validate_engine(RANKING, "pi-neuralwatt", "glm-5.3-flash")
    with pytest.raises(svc.TeamError):
        svc.validate_engine(RANKING, "openai-codex-1", "gpt-5.6-sol", allow_paid=True)


def test_auto_pick_includes_paid_and_prefers_other_model_for_qa():
    assert svc.pick_engine(RANKING, 3) == ("claude-code", "sonnet")
    assert svc.pick_engine(RANKING, 3, avoid=("claude-code", "sonnet")) == ("pi-neuralwatt", "glm-5.3-flash")


async def test_ensure_team_is_idempotent_scoped_and_qa_uses_another_model(env):
    session, board, task = env
    a = await svc.ensure_team(session, board, RANKING, 3)
    b = await svc.ensure_team(session, board, RANKING, 3)
    assert {k: m.id for k, m in a.items()} == {k: m.id for k, m in b.items()}
    assert a["coordinator"].provider is None and a["coordinator"].capability == "orchestrate"
    assert a["developer"].capability == "write_worktree" and a["qa"].capability == "read_only"
    assert (a["qa"].provider, a["qa"].model) != (a["developer"].provider, a["developer"].model)
    assert all(m.board_id == board.id for m in a.values())


async def test_recruit_policy_and_caps(env):
    session, board, _ = env
    await svc.ensure_team(session, board, RANKING, 3)
    with pytest.raises(svc.TeamError):
        await svc.recruit(session, board, "specialist", RANKING, slug="red-team-operator")
    with pytest.raises(svc.TeamError):
        await svc.recruit(session, board, "root", RANKING)
    for slug in ("security-architect", "devops-engineer"):
        await svc.recruit(session, board, "specialist", RANKING, slug=slug)
    assert (await svc.recruit(session, board, "specialist", RANKING, slug="devops-engineer")).name == "team-specialist-devops-engineer"
    for i in range(svc.MAX_MEMBERS_PER_BOARD - len(await svc._members(session, board.id))):  # fill to the cap
        session.add(TeamMember(board_id=board.id, name=f"x{i}", role_kind="qa", provider="p", model="m"))
    await session.flush()
    with pytest.raises(svc.TeamError):
        await svc.recruit(session, board, "specialist", RANKING, slug="software-architect")  # member cap


async def test_coordinator_can_never_own_an_attempt(env):
    session, board, task = env
    team = await svc.ensure_team(session, board, RANKING, 3)
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    session.add(TeamAttempt(cycle_id=cycle.id, board_id=board.id, task_id=task.id, member_id=team["coordinator"].id,
                            role_kind="coordinator", stage_key="x", provider="p", model="m"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# ── happy path ──────────────────────────────────────────────────────────────


async def test_full_cycle_plan_develop_independent_qa_reviewed_publication(env):
    session, board, task = env
    cycle, devs = await to_qa(session, board, task)
    assert cycle.state == "qa" and {d.stage_key for d in devs} == {"dev-api", "dev-ui"}
    assert devs[1].role_kind == "specialist"
    qa, tok = await claim(session, cycle, base="e" * 40)  # production moved: QA may still run
    assert qa.role_kind == "qa" and qa.member_id not in {d.member_id for d in devs}
    # QA exiting 0 without a verdict is not success.
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="looks fine", head_sha=INTEGRATED)
    assert qa.status == "failed" and cycle.state == "qa"
    qa2, tok2 = await claim(session, cycle, base="e" * 40)
    assert qa2.attempt_no == 2
    await svc.complete_attempt(session, board, cycle, qa2, tok2, ok=True, artifact="ran tests: 5 passed", head_sha=INTEGRATED,
                               verdict="pass", usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 900, "cost_usd": 0.12})
    assert cycle.state == "qa_passed" and cycle.reviewed_sha == INTEGRATED
    assert (qa2.input_tokens, qa2.cache_read_tokens, qa2.cli_cost_estimate_usd) == (10, 900, 0.12)
    assert task.tokens_used == 15  # cache reads are not folded into the counter
    await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)  # second reservation loses
    await svc.complete_publication(session, cycle, INTEGRATED, cycle.version, recurring=False)
    assert cycle.state == "published" and task.status == "review"  # never done
    text = "\n".join(await comments(session))
    assert "team-coordinator" in text and "team/team-qa" in text and "NOT billed" in text
    assert "deterministic orchestrator, no model" in text


async def test_recurring_team_task_returns_to_inbox(env):
    session, board, task = env
    task.repeat_enabled = True
    cycle, _ = await to_qa(session, board, task)
    qa, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="ok", head_sha=INTEGRATED, verdict="pass")
    await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)
    await svc.complete_publication(session, cycle, INTEGRATED, cycle.version, recurring=True)
    assert task.status == "inbox"


async def test_vanished_publisher_block_never_claims_nothing_was_published(env):
    """2026-10-07 canary 0000a012: independent QA reproduced a fast-forward before DB
    completion followed by "Publication did not happen"; the outcome is unknown."""
    from datetime import timedelta

    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    qa, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="ok", head_sha=INTEGRATED, verdict="pass")
    await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)
    cycle.updated_at = svc.utcnow() - timedelta(seconds=svc.PUBLISH_LEASE_SECONDS + 1)
    await svc.reap(session, cycle)
    assert cycle.state == "blocked" and cycle.published_sha is None
    text = (await comments(session))[-1]
    assert "publisher vanished" in text and "Publication did not happen" not in text
    assert f"Publication outcome unknown: production may already be at {INTEGRATED}" in text


async def test_block_before_publication_still_says_it_did_not_happen(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    await svc._block(session, cycle, "cycle deadline exceeded")
    assert "Publication did not happen" in (await comments(session))[-1]


async def test_unreported_usage_is_null_not_zero(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=False, error="boom")
    assert pm.usage_reported is False and pm.input_tokens is None and pm.cli_cost_estimate_usd is None


# ── QA failure, plan and scope rules ────────────────────────────────────────


async def test_docs_defect_is_code_classified_and_enters_bounded_correction(env):
    session, board, task = env
    task.title = "Refresh architecture documentation"
    task.description = "Document source behavior and diagrams."
    plan = {"summary": "docs", "qa_checks": ["compare docs to source"], "workers": [
        {"key": "docs", "role": "developer", "title": "Docs", "scope": ["backend/app"], "instructions": "Update docs."},
        {"key": "docs2", "role": "specialist:documentation-engineer", "title": "Docs", "scope": ["frontend"], "instructions": "Update docs."}
    ]}
    cycle, _ = await to_qa(session, board, task, plan)
    qa, token = await claim(session, cycle)
    brief = svc.brief(task, cycle, qa, await session.get(TeamMember, qa.member_id), None)
    assert "source/document contradictions" in brief
    finding = "Reproduced: docs claim endpoint X; source route does not expose it.\nCORRECTION_KIND: CODE"
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True, artifact=finding,
                               head_sha=INTEGRATED, verdict="fail")
    assert cycle.state == "developing" and cycle.round == 2
    assert cycle.qa_feedback == finding and cycle.published_sha is None
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)


@pytest.mark.parametrize("marker", ["CORRECTION_KIND: EVIDENCE", "", "CORRECTION_KIND: CODE\nCORRECTION_KIND: EVIDENCE"])
async def test_docs_fail_without_unambiguous_code_marker_fails_closed(env, marker):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    qa, token = await claim(session, cycle)
    artifact = "Reproduced source/document mismatch\n" + marker
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True, artifact=artifact,
                               head_sha=INTEGRATED, verdict="fail")
    assert cycle.state == "blocked" and cycle.round == 1 and cycle.published_sha is None
    if marker == "CORRECTION_KIND: EVIDENCE":
        assert "evidence incomplete" in cycle.block_reason
        # 0000a005, 2026-10-25: QA asked for missing tests and recovery re-planned from PM twice.
        # Resumed in place, the findings become a correction round on the same integration.
        task.status = "in_progress"
        again, _ = await svc.start_cycle(session, board, task, RANKING)
        assert again.id == cycle.id and again.state == "developing" and again.round == 2
        assert "Reproduced source/document mismatch" in cycle.qa_feedback
    else:
        assert "classification" in cycle.block_reason


async def _stop(session, task, cycle, reason="cycle deadline exceeded"):
    await svc._block(session, cycle, reason)
    task.status = "in_progress"
    await session.commit()


async def test_stopped_cycle_resumes_in_place_keeping_finished_leaves(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact="```json\n" + json.dumps(PLAN) + "\n```", ranking=RANKING)
    a, t = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, a, t, ok=True, artifact="done", head_sha=DEV_SHA, changed_files=["backend/app/x.py"])
    await _stop(session, task, cycle)
    await svc.restamp_budget(session, task, {"max_tokens_budget": 1_500_000})  # a cap raise is no new instruction
    task.max_tokens_budget = 1_500_000
    again, resumed = await svc.start_cycle(session, board, task, RANKING)
    assert resumed and again.id == cycle.id and again.state == "developing" and again.block_reason is None
    rows = await svc._attempts(session, cycle.id)
    assert not [x for x in rows if x.role_kind == "pm" and x.attempt_no > 1]  # no re-plan
    queued = [x for x in rows if x.status == "queued"]
    assert [x.stage_key for x in queued] == ["dev-ui"] and queued[0].attempt_no == 2
    assert "resumed after: cycle deadline exceeded" in json.loads(queued[0].assignment)["retry_feedback"]


@pytest.mark.parametrize("appended", ["\n\n## Owner decision (2026-10-29): warn instead", "\n\n## Re-scope (fleet)\nsplit"])
async def test_owner_decision_amendment_keeps_the_failed_integration(env, appended):
    # 2026-10-29: a task's five QA rounds failed on one question only the owner could answer;
    # writing the answer into the brief re-planned from PM and dropped a 1.8M-token implementation.
    session, board, task = env
    task.description = "Brief"
    cycle, _ = await to_qa(session, board, task)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True, head_sha=INTEGRATED,
                               artifact="needs an owner decision\nCORRECTION_KIND: EVIDENCE", verdict="fail")
    assert cycle.state == "blocked"
    await svc.restamp_budget(session, task, {"description": task.description + appended})
    task.description += appended
    task.status = "in_progress"
    again, _ = await svc.start_cycle(session, board, task, RANKING)
    if "Owner decision" in appended:  # a correction round on the kept integration reads the live brief
        assert again.id == cycle.id and again.state == "developing" and again.round == 2
        assert "needs an owner decision" in again.qa_feedback
    else:  # any other edit is a new instruction: fresh PM
        assert again.id != cycle.id


@pytest.mark.parametrize("refusal", [None, "other_block", "wrong_sha", "changed_task", "missing_artifact", "incomplete_artifact", "dependent_qa"])
async def test_production_refresh_conflict_resumes_exact_pass_without_replay(env, refusal):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    qa, token = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, token, ok=True,
                               artifact="checked exact tree\nVERDICT: PASS",
                               head_sha=INTEGRATED, verdict="pass")
    reason = "production-refresh conflict; owner resolution required; stage=production-refresh"
    await _stop(session, task, cycle, "publisher vanished mid-publication" if refusal == "other_block" else reason)
    if refusal == "wrong_sha":
        cycle.reviewed_sha = "9" * 40
    elif refusal == "changed_task":
        task.description = "Changed instructions"
    elif refusal == "missing_artifact":
        qa.artifact = ""
    elif refusal == "incomplete_artifact":
        qa.assignment = json.dumps({**json.loads(qa.assignment), "artifact_complete": False})
    elif refusal == "dependent_qa":
        author = next(a for a in await svc._attempts(session, cycle.id) if a.role_kind == "developer")
        qa.member_id = author.member_id
    before = [a.model_dump() for a in await svc._attempts(session, cycle.id)]
    used, rnd = task.tokens_used, cycle.round
    again, resumed = await svc.start_cycle(session, board, task, RANKING)
    if refusal:
        assert not resumed and again.id != cycle.id and again.state == "planning"
        return
    assert resumed and again.id == cycle.id and again.state == "qa_passed"
    assert again.reviewed_sha == again.integration_sha == INTEGRATED
    assert again.round == rnd and again.block_reason is None
    assert [a.model_dump() for a in await svc._attempts(session, cycle.id)] == before
    assert task.tokens_used == used
    await svc.reserve_publication(session, again, INTEGRATED, again.version)
    await svc.complete_publication(session, again, INTEGRATED, again.version, recurring=False)
    assert again.published_sha == INTEGRATED and task.status == "review"


async def test_leaf_integration_conflict_replans_instead_of_resuming_the_same_merge(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact="```json\n" + json.dumps(PLAN) + "\n```", ranking=RANKING)
    await _stop(session, task, cycle, "leaf-integration conflict; owner resolution required; stage=leaf-integration")
    again, _ = await svc.start_cycle(session, board, task, RANKING)
    assert again.id != cycle.id and again.state == "planning"


async def test_resumed_qa_fail_gets_a_correction_round_and_changed_task_starts_fresh(env):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    for head in (INTEGRATED, "3" * 40):
        qa, tok = await claim(session, cycle)
        await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, verdict="fail", head_sha=head,
                                   artifact="bad\nCORRECTION_KIND: CODE")
        if cycle.state == "developing":
            for sha, files in (("1" * 40, ["backend/app/y.py"]), ("2" * 40, ["frontend/b.ts"])):
                a, t = await claim(session, cycle)
                await svc.complete_attempt(session, board, cycle, a, t, ok=True, head_sha=sha, changed_files=files)
            await svc.integrate(session, cycle, "3" * 40)
    assert cycle.block_reason == "QA failed in round 2/2"
    task.status = "in_progress"
    again, _ = await svc.start_cycle(session, board, task, RANKING)
    assert again.id == cycle.id and again.state == "developing" and again.round == again.max_rounds == 3
    assert len([x for x in await svc._attempts(session, cycle.id) if x.status == "queued"]) == 2
    await _stop(session, task, again)
    task.description = "new instructions"
    fresh, _ = await svc.start_cycle(session, board, task, RANKING)
    assert fresh.id != cycle.id and fresh.state == "planning"


async def test_qa_fail_requeues_developers_then_blocks_and_never_publishes(env):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    qa, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="test_x fails\nCORRECTION_KIND: CODE", head_sha=INTEGRATED, verdict="fail")
    assert cycle.state == "developing" and cycle.round == 2 and "test_x fails" in (cycle.qa_feedback or "")
    assert cycle.reviewed_sha is None
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)
    for sha, files in (("1" * 40, ["backend/app/y.py"]), ("2" * 40, ["frontend/b.ts"])):
        a, t = await claim(session, cycle)
        assert "test_x fails" in svc.brief(task, cycle, a, await session.get(TeamMember, a.member_id), None)
        await svc.complete_attempt(session, board, cycle, a, t, ok=True, head_sha=sha, changed_files=files)
    await svc.integrate(session, cycle, "3" * 40)
    qa, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="still bad\nCORRECTION_KIND: CODE", head_sha="3" * 40, verdict="fail")
    assert cycle.state == "blocked" and cycle.published_sha is None


async def test_correction_leaf_without_findings_may_keep_the_failed_integration(env):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    qa, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="api bad\nCORRECTION_KIND: CODE", head_sha=INTEGRATED, verdict="fail")
    fixer, t = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, fixer, t, ok=True, head_sha="1" * 40, changed_files=["backend/app/y.py"])
    idle, t = await claim(session, cycle)
    assert idle.base_sha == INTEGRATED
    await svc.complete_attempt(session, board, cycle, idle, t, ok=True, head_sha=INTEGRATED, changed_files=[])
    assert idle.status == "succeeded" and cycle.state == "developing"
    await svc.integrate(session, cycle, "3" * 40)
    assert cycle.state == "qa"


@pytest.mark.parametrize("bad", [
    {"workers": []},
    {"workers": [{"key": "a", "scope": ["../etc"]}]},
    {"workers": [{"key": "a", "scope": ["/abs"]}]},
    {"workers": [{"key": "a", "scope": ["x"]}, {"key": "b", "scope": ["x/y"]}]},
    {"workers": [{"key": "a", "role": "root-shell", "scope": ["x"]}]},
    {"workers": [{"key": f"w{i}", "scope": [f"d{i}"]} for i in range(svc.MAX_PARALLEL_WORKERS + 1)]},
])
def test_plan_rejects_unbounded_or_unsafe_assignments(bad):
    with pytest.raises(svc.TeamError):
        svc.parse_plan(json.dumps(bad))


async def test_long_plan_instructions_and_qa_checks_reach_the_worker_intact(env):
    session, board, task = env
    instructions = "Inventory the explicitly allowed source files.\n" * 100
    instructions += "Finish the allowlist with internal/testutil, internal/utils and tools/openapipatch."
    check = "Inspect only the two owned guides and their explicitly allowed source references. " * 5
    check += "Never read deployment inventory or real configuration."
    plan = {"workers": [{"key": "guide", "role": "developer", "scope": ["docs"],
                         "instructions": instructions}], "qa_checks": [check]}
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True,
                               artifact=json.dumps(plan), ranking=RANKING)
    worker, _ = await claim(session, cycle)
    assert json.loads(worker.assignment)["instructions"] == instructions
    assert json.loads(cycle.plan)["qa_checks"] == [check]
    brief = svc.brief(task, cycle, worker, await session.get(TeamMember, worker.member_id), None)
    assert instructions in brief


async def test_developer_out_of_scope_output_is_accepted_as_guidance(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    a, t = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, a, t, ok=True, head_sha=DEV_SHA, changed_files=["scripts/evil.sh"])
    # owner 2026-10-22: scope is guidance; overlapping leaves surface as integration conflicts
    assert a.status == "succeeded" and not a.error


async def test_integrate_requires_every_developer(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    a, t = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, a, t, ok=True, head_sha=DEV_SHA, changed_files=["backend/app/x.py"])
    with pytest.raises(svc.TeamError):  # second developer still queued
        await svc.integrate(session, cycle, INTEGRATED)


async def test_dev_output_cannot_publish_without_qa_pass_and_wrong_sha_is_refused(env):
    session, board, task = env
    cycle, _ = await to_qa(session, board, task)
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(session, cycle, INTEGRATED, cycle.version)
    qa, tok = await claim(session, cycle)
    # QA reviewed a different commit than the integrated one: attempt fails, no pass recorded.
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True, artifact="ok", head_sha="9" * 40, verdict="pass")
    assert qa.status == "failed" and cycle.state == "qa" and cycle.reviewed_sha is None


# ── fencing: stale, restart, cancel, deadline, cross-board ──────────────────


async def test_second_claim_gets_a_different_attempt_and_wrong_token_is_fenced(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    assert await svc.claim_next(session, cycle, base_sha=BASE) is None  # nothing else queued
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(session, board, cycle, pm, "wrong", ok=True, artifact=json.dumps(PLAN))
    assert pm.status == "running"


async def test_restart_recover_invalidates_old_claim_and_stale_worker_cannot_overwrite(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    old, old_tok = await claim(session, cycle)
    assert await svc.recover(session, cycle) == 0  # a live concurrent worker must not be stolen
    old.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert await svc.recover(session, cycle) == 1
    new, new_tok = await claim(session, cycle)
    assert new.id != old.id and new.attempt_no == 2
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(session, board, cycle, old, old_tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    assert cycle.state == "planning" and cycle.plan is None
    await svc.complete_attempt(session, board, cycle, new, new_tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    assert cycle.state == "developing"


async def test_repeated_restarts_block_instead_of_looping_forever(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    for _i in range(2):
        a, _ = await claim(session, cycle)
        a.lease_expires_at = utcnow() - timedelta(seconds=1)
        await svc.recover(session, cycle)
    assert cycle.state == "blocked"


async def test_expired_lease_requeues_and_late_result_is_discarded(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    a, tok = await claim(session, cycle)
    a.lease_expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(session, board, cycle, a, tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    assert a.status == "expired"
    b, _ = await claim(session, cycle)
    assert b.attempt_no == 2


async def test_heartbeat_extends_lease_only_for_live_claim(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    a, tok = await claim(session, cycle)
    a.lease_expires_at = utcnow() + timedelta(seconds=5)
    assert await svc.heartbeat(session, cycle, a, tok) > utcnow() + timedelta(seconds=60)
    with pytest.raises(svc.TeamFenced):
        await svc.heartbeat(session, cycle, a, "nope")


async def test_deadline_blocks_cycle(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    cycle.deadline_at = utcnow() - timedelta(seconds=1)
    cycle.created_at = utcnow() - timedelta(hours=svc.CYCLE_MAX_HOURS + 1)  # past the hard ceiling
    assert await svc.claim_next(session, cycle, base_sha=BASE) is None
    assert cycle.state == "blocked" and "deadline" in cycle.block_reason


async def test_task_leaving_in_progress_cancels_inflight_workers(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    a, tok = await claim(session, cycle)
    task.status = "blocked"
    await session.commit()
    with pytest.raises(svc.TeamFenced):
        await svc.complete_attempt(session, board, cycle, a, tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    assert cycle.state == "cancelled" and a.status == "cancelled"


async def test_one_active_cycle_per_task_and_start_resumes(env):
    session, board, task = env
    c1, resumed1 = await svc.start_cycle(session, board, task, RANKING)
    c2, resumed2 = await svc.start_cycle(session, board, task, RANKING)
    assert c1.id == c2.id and (resumed1, resumed2) == (False, True)
    task.status = "inbox"
    with pytest.raises(svc.TeamError):
        await svc.start_cycle(session, board, task, RANKING)


async def test_start_requires_team_mode_and_same_board(env):
    session, board, task = env
    task.execution_mode = "single"
    with pytest.raises(svc.TeamError):
        await svc.start_cycle(session, board, task, RANKING)
    task.execution_mode = "team"
    other = Board(id=uuid4(), organization_id=board.organization_id, name="o", slug="o", gateway_id=board.gateway_id)
    with pytest.raises(svc.TeamError):
        await svc.start_cycle(session, other, task, RANKING)


async def test_worker_output_secrets_are_redacted(env):
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    a, tok = await claim(session, cycle)
    await svc.complete_attempt(session, board, cycle, a, tok, ok=False, artifact="key sk-abcdefghijklmnopqrstuvwxyz and Bearer abcdefghijklmnopqrstuv1234", error="x")
    assert "sk-abc" not in a.artifact and "Bearer abcdef" not in a.artifact


async def test_registered_task_engine_pin_does_not_bypass_availability_hold(env):
    session, board, task = env
    task.harness_provider, task.harness_model = "claude-code", "sonnet"
    held = {**RANKING, "levels": [{"entries": [
        {**entry("claude-code", "sonnet"), "unavailable_until": "9999-01-01T00:00:00+00:00"},
        entry("pi-neuralwatt", "glm-5.3-flash", provider_priority=1),
    ]}]}
    cycle, _ = await svc.start_cycle(session, board, task, held)
    pm, token = await svc.claim_next(session, cycle, base_sha=BASE, ranking=held)
    assert pm
    await svc.complete_attempt(session, board, cycle, pm, token, ok=True,
                               artifact=json.dumps(PLAN), ranking=held)
    dev, _ = await svc.claim_next(session, cycle, base_sha=BASE, ranking=held)
    assert dev and (dev.provider, dev.model) == ("pi-neuralwatt", "glm-5.3-flash")


async def test_blocked_runner_timeout_does_not_increase_effort(env):
    session, board, task = env
    prior = svc.TeamCycle(board_id=board.id, task_id=task.id, state="blocked",
                          block_reason="stage dev-container-lifecycle: not retryable unchanged "
                          "(interrupted: attempt time limit reached; partial output discarded)",
                          deadline_at=utcnow() + timedelta(hours=1))
    session.add(prior)
    await session.flush()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    attempt = svc.TeamAttempt(cycle_id=cycle.id, board_id=board.id, task_id=task.id,
                              role_kind="developer", stage_key="x", attempt_no=1)
    assert await svc._escalation(session, cycle, attempt) == 0


async def test_quoted_timeout_in_provider_failure_still_increases_effort(env):
    session, board, task = env
    prior = svc.TeamCycle(
        board_id=board.id, task_id=task.id, state="blocked",
        block_reason=("stage pm failed after 2 attempts (provider API error (503): echoed prompt: "
                      "interrupted: attempt time limit reached; partial output discarded)"),
        deadline_at=utcnow() + timedelta(hours=1),
    )
    session.add(prior)
    await session.flush()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    attempt = svc.TeamAttempt(cycle_id=cycle.id, board_id=board.id, task_id=task.id,
                              role_kind="developer", stage_key="x", attempt_no=1)
    assert await svc._escalation(session, cycle, attempt) == 1


async def test_cancelled_prior_cycle_does_not_increase_retry_effort(env):
    session, board, task = env
    prior = svc.TeamCycle(board_id=board.id, task_id=task.id, state="cancelled",
                          deadline_at=utcnow() + timedelta(hours=1))
    session.add(prior)
    await session.flush()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    dev = (await svc._members(session, board.id))[1]
    attempt = svc.TeamAttempt(cycle_id=cycle.id, board_id=board.id, task_id=task.id,
                              member_id=dev.id, role_kind="developer", stage_key="x")
    assert await svc._escalation(session, cycle, attempt) == 0


async def test_task_engine_pin_overrides_implementation_engine_only(env):
    # Fleet escalation switches a meta-debug task's implementation model by pinning the task.
    session, board, task = env
    task.harness_provider, task.harness_model = "pinned-provider", "pinned-model"
    await session.commit()
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    pm, tok = await claim(session, cycle)
    assert (pm.provider, pm.model) != ("pinned-provider", "pinned-model")  # planning keeps its engine
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact=json.dumps(PLAN), ranking=RANKING)
    dev, _ = await claim(session, cycle)
    assert (dev.provider, dev.model) == ("pinned-provider", "pinned-model")


async def test_healthy_worker_survives_overdue_cycle_extension(env, monkeypatch):
    from datetime import UTC, datetime

    session, board, task = env
    now = [datetime.now(UTC).replace(tzinfo=None)]
    monkeypatch.setattr(svc, "utcnow", lambda: now[0])
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    cycle.deadline_at = now[0] + timedelta(seconds=5)
    cycle.created_at = now[0] - timedelta(hours=svc.CYCLE_MAX_HOURS) + timedelta(minutes=10)
    attempt, token = await claim(session, cycle)

    now[0] += timedelta(seconds=4)
    await svc.heartbeat(session, cycle, attempt, token)
    now[0] += timedelta(seconds=2)  # healthy heartbeat crosses the extendable cycle deadline
    await svc.heartbeat(session, cycle, attempt, token)

    assert attempt.status == "running"
    assert cycle.deadline_at == cycle.created_at + timedelta(hours=svc.CYCLE_MAX_HOURS)
    assert attempt.lease_expires_at > now[0]


async def test_waiting_cycle_is_extended_past_its_window_not_blocked(env):
    # 2026-10-24: 3h cycles died with their developer queued behind exhausted codex quota.
    from datetime import timedelta
    from app.core.time import utcnow
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)  # PM attempt queued
    cycle.deadline_at = utcnow() - timedelta(minutes=1)
    await svc.reap(session, cycle)
    assert cycle.state != "blocked" and cycle.deadline_at > utcnow()
    cycle.deadline_at = utcnow() - timedelta(minutes=1)
    cycle.created_at = utcnow() - timedelta(hours=svc.CYCLE_MAX_HOURS + 1)  # hard ceiling
    await svc.reap(session, cycle)
    assert cycle.state == "blocked" and "deadline" in (cycle.block_reason or "")


async def test_models_of_one_provider_have_their_own_concurrency_slots(env):
    # 2026-10-24: load was counted per provider, so luna's 3 running developers used up
    # sol's 3 slots too and PM/QA queued for hours behind them.
    session, board, task = env
    two = {**RANKING, "levels": [{"entries": [
        entry("omniroute-codex", "gpt-6-luna", max_concurrency=1),
        entry("omniroute-codex", "gpt-6.1-sol", max_concurrency=1),
    ]}]}
    cycle, _ = await svc.start_cycle(session, board, task, two)
    pm, tok = await svc.claim_next(session, cycle, base_sha=BASE, ranking=two)
    await session.commit()
    await svc.complete_attempt(session, board, cycle, pm, tok, ok=True, artifact=json.dumps(PLAN), ranking=two)
    first, _ = await svc.claim_next(session, cycle, base_sha=BASE, ranking=two)
    await session.commit()
    second = await svc.claim_next(session, cycle, base_sha=BASE, ranking=two)
    assert second, "the other model's slot is free"
    assert {first.model, second[0].model} == {"gpt-6-luna", "gpt-6.1-sol"}


async def test_a_freed_engine_slot_goes_to_the_oldest_polling_waiter(env):
    # 2026-10-27: busy runners re-claimed the slot their own attempt freed; QA and PM queued
    # since 22:42 never got sol while leaves created at 05:10 were claimed within a second.
    session, board, task = env
    one = {**RANKING, "levels": [{"entries": [entry("omniroute-codex", "gpt-6.1-sol", max_concurrency=1)]}]}
    cycles = {}
    for name in ("busy", "old", "new"):
        t = task if name == "busy" else Task(
            id=uuid4(), board_id=board.id, title=name, status="in_progress",
            execution_lane="harness", execution_mode="team", complexity=3, repeat_enabled=False)
        session.add(t)
        await session.commit()
        cycles[name], _ = await svc.start_cycle(session, board, t, one)
    running, _ = await svc.claim_next(session, cycles["busy"], base_sha=BASE, ranking=one)
    await session.commit()
    assert await svc.claim_next(session, cycles["old"], base_sha=BASE, ranking=one) is None
    await session.commit()  # the old cycle's runner polled while the slot was taken
    running.status = "succeeded"
    await session.commit()

    assert await svc.claim_next(session, cycles["new"], base_sha=BASE, ranking=one) is None
    got = await svc.claim_next(session, cycles["old"], base_sha=BASE, ranking=one)
    assert got and got[0].cycle_id == cycles["old"].id
    got[0].status = "succeeded"
    old_left = TeamAttempt(**{**got[0].model_dump(), "id": uuid4(), "status": "queued",
                              "stage_key": "dev-x", "updated_at": utcnow() - timedelta(minutes=5)})
    session.add(old_left)  # an older waiter whose runner went away does not hold the engine
    await session.commit()
    assert await svc.claim_next(session, cycles["new"], base_sha=BASE, ranking=one)


async def test_failed_local_draft_round_does_not_use_up_a_frontier_correction_round(env, monkeypatch):
    from app.services import owner_reviews
    monkeypatch.setattr(owner_reviews, "is_reviewer_engine", lambda *a: True)
    session, board, task = env
    cycle, devs = await to_qa(session, board, task)
    qa, tok = await claim(session, cycle)
    for d in devs:
        d.provider, d.model = "metis", "qwen3.8"
    await session.commit()
    await svc.complete_attempt(session, board, cycle, qa, tok, ok=True,
                               artifact="draft misses X\nCORRECTION_KIND: CODE", head_sha=INTEGRATED, verdict="fail")
    assert cycle.state == "developing" and cycle.round == 2 and cycle.max_rounds == 3
