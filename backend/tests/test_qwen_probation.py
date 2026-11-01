"""Read-only local probation and the real managed claim/heartbeat/result path."""
import json
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from sqlmodel import select

from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamCycle
from app.services import mc_sidecar, model_ranking, team_cycle as svc
from app.services.owner_reviews import set_managed_boards
from tests.test_autonomous_review import AUTH, _client
from tests.test_autonomous_review import env as api_env, sidecar  # noqa: F401
from tests.test_orchestration_handoff import seed
from tests.test_team_cycle import BASE, RANKING, entry
from tests.test_team_cycle import env  # noqa: F401

QWEN = ("vllm", "Qwen3.8-27B-FP8")
RANK = {**RANKING, "levels": [{"entries": [
    entry(*QWEN, level=3, max_complexity=3, cost_class="local", max_concurrency=1),
    entry("claude-code", "opus-5-5", max_concurrency=2),
    entry("claude-code", "sonnet", provider_priority=99),
]}]}


@pytest.mark.parametrize("allowed", [None, False, "true", 1, True])
def test_only_explicit_owner_true_promotes_local_writes(allowed):
    ranking = deepcopy(RANK)
    if allowed is not None:
        ranking["levels"][0]["entries"][0]["write_enabled"] = allowed
    assert svc.pick_engine(ranking, 3, prefer_local=True) == QWEN
    # Local implementation is limited to simple work (complexity <= 2, owner 2026-10-22).
    assert svc.pick_engine(ranking, 2, prefer_local=True, write=True) == (
        QWEN if allowed is True else ("claude-code", "opus-5-5"))
    assert svc.pick_engine(ranking, 3, prefer_local=True, write=True) != QWEN
    assert svc.pick_engine(ranking, 4, prefer_local=True) != QWEN
    assert svc.pick_engine(ranking, 3, qa=True) != QWEN
    assert not svc.write_engine_allowed(None, *QWEN)


async def test_pinned_local_implementation_cannot_bypass_probation(env, sidecar):
    s, board, task = env
    roles = await svc.ensure_team(s, board, RANK, 3)
    dev = roles["developer"]
    dev.provider, dev.model, dev.created_by = *QWEN, "admin"
    cycle, _ = await svc.start_cycle(s, board, task, RANK)
    with pytest.raises(svc.TeamError, match="read-only probation"):
        await svc._new_attempt(s, cycle, dev, "dev-main", assignment={"scope": ["backend/"]}, ranking=RANK)
    verify = await svc._new_attempt(s, cycle, dev, "dev-check",
        assignment={"scope": ["backend/"], "mode": "verify"}, ranking=RANK)
    assert svc.attempt_capability(verify.role_kind, verify.assignment) == "read_only"


async def managed_collector(api_env, monkeypatch):
    from app.api import team
    async def ranking(_): return RANK
    monkeypatch.setattr(team, "_ranking", ranking)
    api_env["app"].include_router(team.router, prefix="/api/v1")
    task = await seed(api_env)
    async with api_env["maker"]() as s:
        board = await s.get(Board, api_env["board_id"])
        roles = await svc.ensure_team(s, board, RANK, 3)
        for kind in ("pm", "developer", "qa"):
            roles[kind].provider, roles[kind].model, roles[kind].created_by = "claude-code", "sonnet", "admin"
        # Fixture for retained pre-removal cycles, not a current admission path.
        current = await s.get(Task, task.id)
        current.status = "in_progress"
        plan = svc.parse_plan(json.dumps({"workers": [{"key": "acceptance-evidence",
            "role": "developer", "scope": ["backend/"], "mode": "verify", "complexity": 2}]}))
        plan.update(acceptance_evidence_collection=True, prior_findings_complete=True)
        cycle = TeamCycle(board_id=board.id, task_id=task.id, state="developing",
            max_rounds=1, task_fingerprint=svc.task_fingerprint(current),
            deadline_at=svc.utcnow() + timedelta(seconds=1200), plan=json.dumps(plan))
        s.add(cycle)
        await s.flush()
        await svc._queue_devs(s, cycle, board, RANK)
        await s.commit()
        cid = cycle.id
    base = f"/api/v1/boards/{task.board_id}/tasks/{task.id}/team/cycles/{cid}"
    return task, cid, base


async def api_claim(client, base):
    response = await client.post(base + "/claims", headers=AUTH, json={"base_sha": BASE})
    assert response.status_code == 200 and response.json(), response.text
    return response.json()


async def test_real_managed_collector_heartbeat_result_integration_and_qualified_qa(api_env, monkeypatch):
    task, cid, base = await managed_collector(api_env, monkeypatch)
    async with _client(api_env) as client:
        dev = await api_claim(client, base)
        assert (dev["attempt"]["provider"], dev["attempt"]["model"]) == QWEN
        assert dev["capability"] == "read_only"
        endpoint = base + f"/attempts/{dev['attempt']['id']}"
        assert (await client.post(endpoint + "/heartbeat", headers=AUTH,
            json={"claim_token": dev["claim_token"]})).status_code == 200
        result = await client.post(endpoint + "/complete", headers=AUTH, json={
            "claim_token": dev["claim_token"], "ok": True, "head_sha": BASE,
            "changed_files": [], "artifact": "Fixture source checks executed on exact unchanged base.",
            "tool_counts": {"bash": 1}, "usage": {"input_tokens": 11, "output_tokens": 7}})
        assert result.status_code == 200, result.text
        assert (await client.post(base + "/integrate", headers=AUTH,
            json={"integration_sha": BASE})).status_code == 200
        qa = await api_claim(client, base)
        assert qa["attempt"]["model"] == "opus-5-5" and qa["capability"] == "read_only"
        endpoint = base + f"/attempts/{qa['attempt']['id']}"
        assert (await client.post(endpoint + "/heartbeat", headers=AUTH,
            json={"claim_token": qa["claim_token"]})).status_code == 200
        result = await client.post(endpoint + "/complete", headers=AUTH, json={
            "claim_token": qa["claim_token"], "ok": True, "head_sha": BASE,
            "artifact": "Fixture independent checks on unchanged exact base.", "verdict": "pass"})
        assert result.status_code == 200, result.text
        # A duplicate completion must not cancel newly admitted QA-passed work.
        duplicate = await client.post(endpoint + "/complete", headers=AUTH, json={
            "claim_token": qa["claim_token"], "ok": True, "head_sha": BASE,
            "artifact": "duplicate", "verdict": "pass"})
        assert duplicate.status_code == 409
    async with api_env["maker"]() as s:
        current = await s.get(TeamCycle, cid)
        assert current.state == "qa_passed" and current.reviewed_sha == BASE
        assert (await s.get(Task, task.id)).tokens_used == 339
        assert all(m.model == "sonnet" for m in await svc._members(s, task.board_id) if m.role_kind != "coordinator")


@pytest.mark.parametrize("when", ["queued", "running"])
@pytest.mark.parametrize("change", ["engine", "same_engine_generation", "disabled", "disable_reenable", "policy", "provider"])
async def test_real_member_edits_and_stops_fence_without_fresh_planning(api_env, monkeypatch, when, change):
    task, cid, base = await managed_collector(api_env, monkeypatch)
    async with _client(api_env) as client:
        claim = await api_claim(client, base) if when == "running" else None
        if change == "policy":
            set_managed_boards(api_env["org_id"], [])
        elif change == "provider":
            mc_sidecar.set_model_meta(*QWEN, disabled=True)
            # This test uses a static fake registry, so change that registry too.
            monkeypatch.setitem(RANK["levels"][0]["entries"][0], "disabled", True)
        else:
            payload = {"role_kind": "developer", "provider": "claude-code",
                "model": "opus-5-5" if change == "engine" else "sonnet", "enabled": change != "disabled"}
            path = f"/api/v1/boards/{task.board_id}/team/members"
            if change == "disable_reenable":
                assert (await client.put(path, headers=AUTH, json={**payload, "enabled": False})).status_code == 200
            response = await client.put(path, headers=AUTH, json=payload)
            assert response.status_code == 200, response.text
        if claim:
            endpoint = base + f"/attempts/{claim['attempt']['id']}/heartbeat"
            response = await client.post(endpoint, headers=AUTH, json={"claim_token": claim["claim_token"]})
        else:
            response = await client.post(base + "/claims", headers=AUTH, json={"base_sha": BASE})
        # An unclaimed engine outage can wait or choose another eligible engine;
        # it is never permission to run the disabled engine.
        if when == "queued" and change == "provider":
            assert response.status_code == 200, response.text
            assert not response.json() or response.json()["attempt"]["provider"] != "vllm"
            return
        assert response.status_code == 409, response.text
        start = base.rsplit("/", 1)[0]
        assert (await client.post(start, headers=AUTH)).status_code == 409
    async with api_env["maker"]() as s:
        current = await s.get(TeamCycle, cid)
        assert current.state == "cancelled"
        assert (await s.get(Task, task.id)).status == "blocked"
        assert len(list(await s.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))) == 1
        assert all(a.role_kind != "pm" for a in await svc._attempts(s, cid))


@pytest.mark.parametrize("claimed", [False, True])
async def test_sibling_quota_reselection_preserves_queued_and_running_authority(env, sidecar, claimed):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANK)
    original = (await svc._attempts(s, cycle.id))[0]
    if claimed:
        original, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=RANK)
    await s.commit()
    member = await s.get(svc.TeamMember, original.member_id)
    authority = svc._member_authority(member)
    held = deepcopy(RANK)
    for e in svc._entries(held):
        if (e["provider"], e["model"]) == (member.provider, member.model):
            e["unavailable_until"] = (svc.utcnow() + timedelta(hours=1)).isoformat()
    sibling = Task(board_id=board.id, title="independent sibling", status="in_progress",
        execution_mode="team", execution_lane="harness", complexity=3)
    s.add(sibling)
    await s.commit()
    await svc.start_cycle(s, board, sibling, held)
    await s.commit()
    assert svc._member_authority(member) == authority
    if claimed:
        await svc.heartbeat(s, cycle, original, token, ranking=RANK)
    else:
        assert await svc.claim_next(s, cycle, base_sha=BASE, ranking=RANK)
    assert cycle.state == "planning" and task.status == "in_progress"


@pytest.mark.parametrize("promotion", ["nominal", "promoted", "revoked", "unavailable", "partial"])
async def test_code_fail_and_usage_survive_correction_dispatch(env, sidecar, monkeypatch, promotion):
    s, board, task = env
    ranking = deepcopy(RANK)
    local = ranking["levels"][0]["entries"][0]
    promoted = promotion in ("promoted", "revoked")
    local["level"], local["write_enabled"] = 1, promoted
    ranking["levels"][0]["entries"][1]["provider_priority"] = 1
    roles = await svc.ensure_team(s, board, ranking, 3)
    if promoted:
        roles["developer"].provider, roles["developer"].model, roles["developer"].created_by = *QWEN, "admin"
    cycle, _ = await svc.start_cycle(s, board, task, ranking)
    pm, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
    await s.commit()
    await svc.complete_attempt(s, board, cycle, pm, token, ok=True, ranking=ranking,
        artifact=json.dumps({"workers": [{"key": "main", "role": "developer", "scope": ["backend/"]}]}))
    await s.commit()
    writer, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
    await s.commit()
    assert (writer.provider == "vllm") == promoted
    await svc.complete_attempt(s, board, cycle, writer, token, ok=True, artifact="Fixture implementation",
        head_sha="b" * 40, changed_files=["backend/x.py"], ranking=ranking)
    await svc.integrate(s, cycle, "b" * 40, ranking=ranking)
    await s.commit()
    qa, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
    await s.commit()
    if promotion == "revoked":
        local["write_enabled"] = False
    elif promotion == "unavailable":
        for e in svc._entries(ranking):
            e["eligible"] = False
    elif promotion == "partial":
        real = svc._new_attempt
        async def refuse_after_queue(*args, **kwargs):
            await real(*args, **kwargs)
            await s.flush()
            raise svc.TeamError("fixture refusal after partial admission")
        monkeypatch.setattr(svc, "_new_attempt", refuse_after_queue)
    findings = "CORRECTION_KIND: CODE\nFixture reproduced off-by-one defect"
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True, artifact=findings,
        head_sha="b" * 40, verdict="fail", usage={"input_tokens": 11, "output_tokens": 7}, ranking=ranking)
    await s.commit()
    assert qa.status == "succeeded" and qa.verdict == "fail" and qa.artifact == findings
    assert task.tokens_used == 18 and cycle.qa_feedback == findings
    refused = promotion in ("revoked", "unavailable", "partial")
    assert cycle.state == ("blocked" if refused else "developing")
    if refused:
        assert cycle.round == 1 and cycle.integration_sha == "b" * 40
        assert len(await svc._attempts(s, cycle.id)) == 3  # no spent round or partial correction survives
        assert await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking) is None
    else:
        next_writer, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
        assert next_writer.base_sha == "b" * 40
        assert (next_writer.provider == "vllm") == promoted


@pytest.mark.parametrize("change", ["generation", "disable_reenable"])
async def test_real_owner_revocation_during_qa_cannot_regrant_a_writer(api_env, monkeypatch, change):
    from app.api import team
    from tests.test_autonomous_review import _seed_review_task
    ranking = deepcopy(RANK)
    ranking["levels"][0]["entries"][0]["write_enabled"] = True  # fixture only, never a live grant
    async def registry(_): return ranking
    monkeypatch.setattr(team, "_ranking", registry)
    api_env["app"].include_router(team.router, prefix="/api/v1")
    task = await _seed_review_task(api_env, with_cycle=False, status="in_progress")
    async with api_env["maker"]() as s:
        current = await s.get(Task, task.id)
        current.execution_mode, current.execution_lane = "team", "harness"
        await s.commit()
    member_path = f"/api/v1/boards/{task.board_id}/team/members"
    pin = {"role_kind": "developer", "provider": QWEN[0], "model": QWEN[1], "enabled": True}
    async with _client(api_env) as c:
        assert (await c.put(member_path, headers=AUTH, json=pin)).status_code == 200
        root = f"/api/v1/boards/{task.board_id}/tasks/{task.id}/team/cycles"
        response = await c.post(root, headers=AUTH)
        assert response.status_code == 200, response.text
        cid = UUID(response.json()["cycle"]["id"])
        base = root + f"/{cid}"
        pm = await api_claim(c, base)
        result = await c.post(base + f"/attempts/{pm['attempt']['id']}/complete", headers=AUTH,
            json={"claim_token": pm["claim_token"], "ok": True,
                "artifact": json.dumps({"workers": [{"key": "main", "role": "developer", "scope": ["backend/"]}]})})
        assert result.status_code == 200, result.text
        writer = await api_claim(c, base)
        assert writer["attempt"]["provider"] == "vllm"
        result = await c.post(base + f"/attempts/{writer['attempt']['id']}/complete", headers=AUTH,
            json={"claim_token": writer["claim_token"], "ok": True, "artifact": "Fixture implemented",
                "head_sha": "b" * 40, "changed_files": ["backend/x.py"]})
        assert result.status_code == 200, result.text
        assert (await c.post(base + "/integrate", headers=AUTH, json={"integration_sha": "b" * 40})).status_code == 200
        qa = await api_claim(c, base)
        if change == "disable_reenable":
            assert (await c.put(member_path, headers=AUTH, json={**pin, "enabled": False})).status_code == 200
        assert (await c.put(member_path, headers=AUTH, json=pin)).status_code == 200
        findings = "CORRECTION_KIND: CODE\nFixture reproduced original defect"
        result = await c.post(base + f"/attempts/{qa['attempt']['id']}/complete", headers=AUTH,
            json={"claim_token": qa["claim_token"], "ok": True, "artifact": findings,
                "head_sha": "b" * 40, "verdict": "fail", "usage": {"input_tokens": 11, "output_tokens": 7}})
        assert result.status_code == 200, result.text
        assert (await c.post(base + "/claims", headers=AUTH, json={"base_sha": BASE})).json() is None
    async with api_env["maker"]() as s:
        cycle = await s.get(TeamCycle, cid)
        assert cycle.state == "blocked" and cycle.round == 1
        assert cycle.integration_sha == "b" * 40 and cycle.qa_feedback == findings
        saved = await s.get(svc.TeamAttempt, UUID(qa["attempt"]["id"]))
        assert saved.status == "succeeded" and saved.verdict == "fail" and saved.artifact == findings
        assert (await s.get(Task, task.id)).tokens_used == 18
        assert len(await svc._attempts(s, cid)) == 3  # no correction, clone, PM replay or new grant


async def test_wrong_or_expired_retry_token_cannot_cancel_live_sibling(env, sidecar):
    s, board, task = env
    set_managed_boards(board.organization_id, [str(board.id)])
    cycle, _ = await svc.start_cycle(s, board, task, RANK)
    old, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=RANK)
    await s.commit()
    await svc.complete_attempt(s, board, cycle, old, token, ok=False, error="transient", ranking=RANK)
    await s.commit()
    fresh, fresh_token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=RANK)
    await s.commit()
    assert fresh.id != old.id
    with pytest.raises(svc.TeamFenced):
        await svc.heartbeat(s, cycle, old, token, ranking=RANK)
    with pytest.raises(svc.TeamFenced):
        await svc.heartbeat(s, cycle, fresh, "wrong", ranking=RANK)
    assert cycle.state == "planning" and fresh.status == "running"
    await svc.heartbeat(s, cycle, fresh, fresh_token, ranking=RANK)


async def test_terminal_collection_even_behind_later_history_never_grants_original_pm(env, sidecar):
    s, board, task = env
    # Retained pre-removal history must still fence implementation replay.
    collector = TeamCycle(board_id=board.id, task_id=task.id, state="cancelled",
        deadline_at=svc.utcnow() + timedelta(hours=1),
        plan=json.dumps({"acceptance_evidence_collection": True}))
    s.add(collector)
    await s.flush()
    later = TeamCycle(board_id=board.id, task_id=task.id, state="cancelled",
        deadline_at=svc.utcnow() + timedelta(hours=1), plan="{}")
    s.add(later)
    task.status, task.tokens_used = "in_progress", 321
    await s.commit()
    ids = {a.id for a in await svc._attempts(s, collector.id)}
    with pytest.raises(svc.TeamFenced, match="no PM or implementation replay"):
        await svc.start_cycle(s, board, task, RANK)
    assert task.status == "blocked" and task.tokens_used == 321
    assert {a.id for a in await svc._attempts(s, collector.id)} == ids
    assert len(list(await s.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))) == 2


@pytest.mark.parametrize("model", [QWEN[1], "sonnet"])
async def test_local_results_never_authorize_weak_or_self_qa(env, sidecar, model):
    s, board, task = env
    roles = await svc.ensure_team(s, board, RANK, 3)
    cycle, _ = await svc.start_cycle(s, board, task, RANK)
    author = await svc._new_attempt(s, cycle, roles["developer"], "dev-check",
        assignment={"mode": "verify", "scope": ["backend/"]}, ranking=RANK)
    author.provider, author.model, author.status = *QWEN, "succeeded"
    roles["qa"].provider = "vllm" if model == QWEN[1] else "claude-code"
    roles["qa"].model, roles["qa"].created_by = model, "admin"
    with pytest.raises(svc.TeamError, match="qualified QA"):
        await svc._new_attempt(s, cycle, roles["qa"], "qa", ranking=RANK)
    with pytest.raises(svc.TeamError, match="qualified QA"):
        await svc._new_attempt(s, cycle, roles["qa"], "qa")


async def test_promoted_write_permission_can_be_revoked_mid_lease(env, sidecar):
    s, board, task = env
    ranking = deepcopy(RANK)
    ranking["levels"][0]["entries"][0]["write_enabled"] = True
    roles = await svc.ensure_team(s, board, ranking, 3)
    roles["developer"].provider, roles["developer"].model, roles["developer"].created_by = *QWEN, "admin"
    cycle, _ = await svc.start_cycle(s, board, task, ranking)
    pm, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
    await s.commit()
    await svc.complete_attempt(s, board, cycle, pm, token, ok=True, ranking=ranking,
        artifact=json.dumps({"workers": [{"key": "main", "role": "developer", "scope": ["backend/"]}]}))
    await s.commit()
    writer, token = await svc.claim_next(s, cycle, base_sha=BASE, ranking=ranking)
    await s.commit()
    assert writer.provider == "vllm" and svc.attempt_capability(writer.role_kind, writer.assignment) == "write_worktree"
    ranking["levels"][0]["entries"][0]["write_enabled"] = False
    with pytest.raises(svc.TeamFenced, match="read-only"):
        await svc.heartbeat(s, cycle, writer, token, ranking=ranking)
    assert cycle.state == "cancelled" and task.status == "blocked"


async def test_local_qa_is_never_a_reviewer_even_without_ranking(env, sidecar):
    s, board, task = env
    roles = await svc.ensure_team(s, board, RANK, 3)
    roles["qa"].provider, roles["qa"].model, roles["qa"].created_by = *QWEN, "admin"
    cycle, _ = await svc.start_cycle(s, board, task, RANK)
    with pytest.raises(svc.TeamError, match="qualified QA"):
        await svc._new_attempt(s, cycle, roles["qa"], "qa")


async def test_pinned_local_medium_ceiling_is_not_bypassed(env, sidecar):
    s, board, task = env
    roles = await svc.ensure_team(s, board, RANK, 3)
    roles["pm"].provider, roles["pm"].model, roles["pm"].created_by = *QWEN, "admin"
    task.complexity = 4
    with pytest.raises(svc.TeamError, match="medium"):
        await svc.start_cycle(s, board, task, RANK)


async def test_ranking_probation_setting_is_strict_audited_and_omission_preserves(api_env):
    from app.api import model_ranking as api
    api_env["app"].include_router(api.router, prefix="/api/v1")
    path, identity = "/api/v1/ranking/entry", {"provider": QWEN[0], "model": QWEN[1]}
    async with _client(api_env) as client:
        for value in ("true", 1, None):
            response = await client.put(path, headers=AUTH, json={**identity, "write_enabled": value})
            assert response.status_code == 422
        assert (await client.put(path, json={**identity, "write_enabled": True})).status_code == 401
        response = await client.put(path, headers=AUTH, json={**identity, "write_enabled": False})
        assert response.status_code == 200, response.text
        assert (await client.put(path, headers=AUTH, json={**identity, "max_complexity": 3})).status_code == 200
    assert mc_sidecar.get_model_meta(*QWEN)["write_enabled"] is False
    mc_sidecar.set_model_meta(*QWEN, level=3)
    entries = svc._entries(await model_ranking.get_board([{"provider_name": "vllm", "status": "online"}]))
    assert next(e for e in entries if (e["provider"], e["model"]) == QWEN)["write_enabled"] is False
