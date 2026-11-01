"""Acceptance feedback never queues more execution; historical fences remain."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.models.activity_events import ActivityEvent
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle
from app.services import team_cycle as svc
from app.services.owner_reviews import managed_boards, set_automation_enabled, set_managed_boards
from tests.test_autonomous_review import AUTH, _client, _first_candidate, _load_script, _seed_review_task
from tests.test_autonomous_review import env as api_env, sidecar  # noqa: F401
from tests.test_team_cycle import RANKING
from tests.test_team_cycle import env  # noqa: F401


async def seed(api_env):
    set_automation_enabled(True, api_env["org_id"])
    set_managed_boards(api_env["org_id"], [str(api_env["board_id"])])
    task = await _seed_review_task(api_env, with_cycle=False)
    async with api_env["maker"]() as s:
        task = await s.get(Task, task.id)
        task.execution_mode, task.execution_lane, task.tokens_used = "team", "harness", 321
        s.add(ActivityEvent(task_id=task.id, board_id=task.board_id, event_type="task.comment",
            message="[auto-review] action=revise reviewer=qualified correction_kind=evidence\nAttach missing proof"))
        await s.commit()
    return task


async def candidate(api_env, task):
    async with _client(api_env) as c:
        return await _first_candidate(api_env, c, "task", task.id)


async def handoff(api_env, task, snapshot):
    async with _client(api_env) as c:
        return await c.post(f"/api/v1/review/tasks/{task.id}/evidence", headers=AUTH,
                            json={"fingerprint": snapshot["fingerprint"]})


@pytest.mark.parametrize("status", ["review", "blocked", "paused", "cancelled", "done"])
async def test_retired_handoff_never_changes_task_or_enqueues(api_env, status):
    task = await seed(api_env)
    async with api_env["maker"]() as session:
        t = await session.get(Task, task.id)
        t.status = status
        await session.commit()
    denied = await handoff(api_env, task, {"fingerprint": "a" * 64})
    assert denied.status_code == 410, denied.text
    async with api_env["maker"]() as session:
        assert not list(await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == task.id)))
        assert not list(await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))
        t = await session.get(Task, task.id)
        assert (t.status, t.tokens_used, t.description) == (status, 321, task.description)


async def test_owner_scoped_policy_strict_bool_and_no_cross_org_write(api_env):
    async with _client(api_env) as c:
        for body in ({"enabled": "true"}, {"enabled": True, "managed_board_ids": [str(uuid4())]}):
            r = await c.put("/api/v1/review/automation", headers=AUTH, json=body)
            assert r.status_code in (422, 404)
        r = await c.put("/api/v1/review/automation", headers=AUTH,
            json={"enabled": True, "managed_board_ids": [str(api_env["board_id"])]})
        assert r.status_code == 200, r.text
        assert r.json()["managed_board_ids"] == [str(api_env["board_id"])]
        assert managed_boards(uuid4()) == []
        assert (await c.post(f"/api/v1/review/tasks/{uuid4()}/evidence", json={"fingerprint": "a" * 64})).status_code == 401


async def test_agent_lane_review_is_visible_but_cannot_use_native_acceptance(api_env, monkeypatch):
    from app.api import review
    from tests.test_autonomous_review import _decision_body
    task = await seed(api_env)
    async with api_env["maker"]() as s:
        t = await s.get(Task, task.id); t.execution_lane = "agent"
        s.add(svc.TeamCycle(board_id=t.board_id, task_id=t.id, state="published",
            task_fingerprint=svc.task_fingerprint(t), deadline_at=svc.utcnow() + timedelta(hours=1)))
        await s.commit()
    snap = await candidate(api_env, task)
    assert snap["evidence"]["execution_lane"] == "agent"
    async def refuse_native(*args, **kwargs):
        pytest.fail("Gateway feedback must not attempt native QA recovery")
    monkeypatch.setattr(review, "_recover_task_evidence", refuse_native)
    async with _client(api_env) as c:
        r = await c.post("/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(snap, action="revise", correction_kind="evidence", feedback="Missing original task-session completion evidence; do not replay gateway work."))
    assert r.status_code == 200, r.text
    async with api_env["maker"]() as s:
        assert not (await s.exec(select(TeamAttempt))).all()


@pytest.mark.parametrize("eligible", [True, False])
def test_processed_evidence_revision_never_launches_more_work(tmp_path, monkeypatch, eligible):
    script = _load_script()
    monkeypatch.setattr(script, "STATE_FILE", tmp_path / "state.json")
    c = {"kind": "task", "id": str(uuid4()), "fingerprint": "a" * 64,
        "evidence": {"board_id": "b", "execution_mode": "team", "split_count": 0,
                     "evidence_handoff_pending": True}}
    state = {"processed": {c["fingerprint"]: "earlier"}, "failed": {}}
    calls = []
    def fake(method, path, *body):
        calls.append((method, path))
        if path == "/api/v1/review/automation":
            return {"enabled": True, "managed_board_ids": ["b"], "models": [{"eligible": eligible}]}
        if method == "post": return {"ok": True, "cycle_id": "fresh-cycle"}
        return {"items": [c], "total": 1}
    monkeypatch.setattr(script.team, "api", fake)
    monkeypatch.setattr(script, "review_candidate", lambda *a: pytest.fail("do not replay the successful reviewer"))
    assert script.one_pass(state) == 0
    assert script.one_pass(state) == 0
    assert not any(m == "post" for m, _ in calls)


def test_local_fit_before_scarce_quota_but_never_local_qa():
    ranking = {**RANKING, "levels": [{"entries": [
        {**RANKING["levels"][0]["entries"][0], "model": "opus-5-5"},
        {**RANKING["levels"][0]["entries"][0], "provider": "vllm", "model": "Qwen3.8-27B-FP8", "level": 4, "cost_class": "local", "max_concurrency": 1},
        {**RANKING["levels"][0]["entries"][0], "provider": "pi-neuralwatt", "model": "glm-5.3-flash", "level": 4, "cost_class": "credits", "max_complexity": 2},
    ]}]}
    assert svc.pick_engine(ranking, 2) == ("claude-code", "opus-5-5")  # unmanaged baseline order
    assert svc.pick_engine(ranking, 2, prefer_local=True) == ("vllm", "Qwen3.8-27B-FP8")
    assert svc.pick_engine(ranking, 3, prefer_local=True) == ("claude-code", "opus-5-5")
    assert svc.pick_engine(ranking, 2, qa=True) == ("claude-code", "opus-5-5")


async def test_managed_dispatch_reuses_roles_but_honors_live_capacity_and_qa_quality(env, sidecar):
    s, board, task = env
    from tests.test_team_cycle import entry
    ranking = {**RANKING, "levels": [{"entries": [
        entry("claude-code", "opus-5-5", max_concurrency=2),
        entry("omniroute-codex", "gpt-6.1-sol", max_concurrency=3),
        entry("vllm", "Qwen3.8-27B-FP8", level=4, cost_class="local", max_concurrency=1),
    ]}]}
    team = await svc.ensure_team(s, board, ranking, 2)
    for kind in ("pm", "developer", "qa"):
        team[kind].created_by = "admin"
        team[kind].provider, team[kind].model = "claude-code", "opus-5-5"
    task.complexity = 2
    old, _ = await svc.start_cycle(s, board, task, ranking)
    old_pm = (await svc._attempts(s, old.id))[0]
    assert old_pm.provider == "claude-code"  # unmanaged administrator pin remains binding
    set_automation_enabled(True, board.organization_id)
    set_managed_boards(board.organization_id, [str(board.id)])
    one, _ = await svc.claim_next(s, old, base_sha="a" * 40, ranking=ranking)
    assert one.provider == "vllm"
    other = Task(board_id=board.id, title="disjoint planning", status="in_progress",
        execution_mode="team", execution_lane="harness", complexity=2)
    s.add(other); await s.flush()
    nxt, _ = await svc.start_cycle(s, board, other, ranking)
    two, _ = await svc.claim_next(s, nxt, base_sha="a" * 40, ranking=ranking)
    assert two.provider != "vllm"  # local capacity=1, do not wait on its busy pin
    assert svc.pick_engine(ranking, 2, qa=True)[0] != "vllm"
    assert len(await svc._members(s, board.id)) == 4


async def test_managed_fallback_preserves_invalid_admin_pins_and_refuses_unqualified_qa(env, sidecar):
    from tests.test_team_cycle import entry
    s, board, task = env
    ranking = {**RANKING, "levels": [{"entries": [entry("claude-code", "opus-5-5")]}]}
    roles = await svc.ensure_team(s, board, ranking, 2)
    qa = roles["qa"]; qa.created_by = "admin"; qa.model = "opus-4-8"
    with pytest.raises(svc.TeamError):
        await svc.ensure_team(s, board, ranking, 2)
    set_automation_enabled(True, board.organization_id)
    set_managed_boards(board.organization_id, [str(board.id)])
    await svc.ensure_team(s, board, ranking, 2)
    assert (qa.created_by, qa.provider, qa.model) == ("admin", "claude-code", "opus-4-8")
    cycle = svc.TeamCycle(board_id=board.id, task_id=task.id, deadline_at=svc.utcnow() + timedelta(hours=1))
    s.add(cycle); await s.flush()
    attempt = await svc._new_attempt(s, cycle, qa, "qa", ranking=ranking)
    assert attempt.model == "opus-5-5" and qa.model == "opus-4-8"
    local_only = {**RANKING, "levels": [{"entries": [entry("vllm", "Qwen3.8-27B-FP8", level=4)]}]}
    with pytest.raises(svc.TeamError):
        await svc._new_attempt(s, cycle, qa, "qa", ranking=local_only)
    set_automation_enabled(False, board.organization_id)
    qa.provider, qa.model = "vllm", "Qwen3.8-27B-FP8"
    with pytest.raises(svc.TeamError):
        await svc._new_attempt(s, cycle, qa, "qa", assignment={"acceptance_evidence_collection": True})


async def test_complete_goal_not_clipped_to_last_four_thousand_characters(api_env):
    task = await seed(api_env)
    goal = "First mandatory acceptance criterion.\n" + "context " * 700
    async with api_env["maker"]() as s:
        t = await s.get(Task, task.id); t.description = goal; await s.commit()
    snap = await candidate(api_env, task)
    assert snap["evidence"]["goal_complete"] is True
    assert snap["evidence"]["description"] == goal


@pytest.mark.parametrize("change", ["none", "opt_out", "started", "hold", "grant_used", "frozen", "bad_assignment", "manual_comment", "evidence_collection"])
async def test_unstarted_worklist_is_scoped_pure_and_does_not_replay_authors(api_env, change):
    task = await seed(api_env)
    set_managed_boards(api_env["org_id"], [str(api_env["board_id"])], recover_unstarted=change != "opt_out")
    async with api_env["maker"]() as s:
        t = await s.get(Task, task.id); t.status = "blocked"
        plan = {"workers": [{"key": "main", "role": "developer", "scope": ["backend/tests/"]}]}
        if change == "frozen": plan["workers"][0]["scope"] = ["docs/"]
        if change == "evidence_collection": plan["acceptance_evidence_collection"] = True
        c = TeamCycle(board_id=t.board_id, task_id=t.id, state="blocked", plan=json.dumps(plan),
            task_fingerprint=svc.task_fingerprint(t), deadline_at=svc.utcnow() - timedelta(hours=1),
            block_reason="owner hold" if change == "hold" else "cycle deadline exceeded")
        s.add(c); await s.flush()
        s.add(ActivityEvent(task_id=t.id, board_id=t.board_id, event_type="task.comment",
            message=svc.COORDINATOR + ": BLOCKED: cycle deadline exceeded"))
        if change == "manual_comment":
            s.add(ActivityEvent(task_id=t.id, board_id=t.board_id, event_type="task.comment", message="Owner hold"))
        if change in ("started", "grant_used", "bad_assignment"):
            member = svc.TeamMember(board_id=t.board_id, name="fixture", role_kind="developer",
                                    provider="claude-code", model="sonnet")
            s.add(member); await s.flush()
            s.add(TeamAttempt(board_id=t.board_id, task_id=t.id, cycle_id=c.id, member_id=member.id,
                role_kind="developer", stage_key="dev-main", status="expired", provider="claude-code", model="sonnet",
                started_at=svc.utcnow() if change == "started" else None,
                assignment=("[]" if change == "bad_assignment" else json.dumps({"owner_work_recovery": {"source": "old"}} if change == "grant_used" else {}))))
        await s.commit()
    async with _client(api_env) as client:
        r = await client.get("/api/v1/review/work-recoveries", headers=AUTH)
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == (1 if change == "none" else 0)
    async with api_env["maker"]() as s:
        assert (await s.get(Task, task.id)).status == "blocked"
        assert len(list(await s.exec(select(TeamCycle).where(TeamCycle.task_id == task.id)))) == 1


def test_work_coordinator_uses_existing_once_only_admission_not_a_new_task(tmp_path, monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script, "STATE_FILE", tmp_path / "state.json")
    item = {"board_id": "b", "task_id": "t", "cycle_id": "old", "version": 3}
    calls = []
    def fake(method, path, *body):
        calls.append((method, path, body))
        return {"items": [item]} if method == "get" else {"id": "new-cycle"}
    monkeypatch.setattr(script.team, "api", fake)
    state = {"processed": {}, "failed": {}}
    assert script.recover_unstarted({}, state) == 0 and not calls
    assert script.recover_unstarted({"recover_unstarted": True}, state) == 1
    assert script.recover_unstarted({"recover_unstarted": True}, state) == 0
    post = [call for call in calls if call[0] == "post"]
    assert len(post) == 1 and post[0][1].endswith("/old/resume-work")
    assert post[0][2] == ({"version": 3, "cause": "expired_unstarted"},)


async def test_historical_evidence_cycle_cannot_spend_original_work_grant(api_env):
    task = await seed(api_env)
    async with api_env["maker"]() as session:
        t = await session.get(Task, task.id)
        t.status = "blocked"
        cycle = TeamCycle(board_id=t.board_id, task_id=t.id, state="blocked",
            plan=json.dumps({"acceptance_evidence_collection": True}),
            deadline_at=svc.utcnow() - timedelta(hours=1),
            task_fingerprint=svc.task_fingerprint(t), block_reason="cycle deadline exceeded")
        session.add(cycle)
        await session.commit()
        with pytest.raises(svc.TeamError, match="evidence collection is not original work"):
            await svc.resume_work(session, cycle, cycle.version, "expired_unstarted", ranking=RANKING)
        assert not list(await session.exec(select(TeamAttempt).where(TeamAttempt.task_id == t.id)))
