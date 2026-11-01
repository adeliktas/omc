"""Real API/DB activity checks, not model inference or task acceptance evidence."""

import asyncio
from datetime import timedelta
import json
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.api import tasks as tasks_api
from app.core.time import utcnow
from app.models.activity_events import ActivityEvent
from app.models.boards import Board
from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle, TeamMember
from app.services import mc_sidecar
from app.services.board_snapshot import build_board_snapshot
from app.services.owner_reviews import REVIEWER_MODELS, set_automation_enabled
from app.services.task_execution import lease_data, live_review_leases, review_load, task_execution_state
from tests.test_autonomous_review import AUTH, _client, _decision_body, _seed_review_task, env, sidecar  # noqa: F401


async def _candidate(client, task_id):
    response = await client.get("/api/v1/review/candidates", headers=AUTH)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["id"] == str(task_id))


def _claim(candidate, session_id=None):
    provider, model = REVIEWER_MODELS[0]
    return {"fingerprint": candidate["fingerprint"], "reviewer_provider": provider,
            "reviewer_model": model, "reviewer_session_id": str(session_id or uuid4()),
            "timeout_seconds": 60}


async def test_parent_counts_and_inactive_work_are_consistent_in_snapshot_and_tasks(env):
    parent = await _seed_review_task(env, status="in_progress", with_cycle=False)
    async with env["maker"]() as session:
        stored = await session.get(Task, parent.id)
        stored.execution_mode, stored.split_type, stored.split_count = "team", "dag", 4
        session.add(stored)
        for status in ("done", "review", "paused", "blocked"):
            session.add(Task(board_id=env["board_id"], parent_task_id=parent.id,
                             title=status, status=status))
        await session.commit()
        board = await session.get(Board, env["board_id"])
        snapshot = await build_board_snapshot(session, board)
        card = next(card for card in snapshot.tasks if card.id == parent.id)
        read = await tasks_api._task_read_response(session, task=stored, board_id=board.id)
        assert card.dag_done_count == read.dag_done_count == 1
        assert card.work_state == read.work_state == "waiting"
        assert card.work_detail == read.work_detail == "Waiting for children"
        assert stored.status == "in_progress"  # children still have unchanged parent authority
        row = await session.get(Task, parent.id)
        assert row.split_count == 4


async def test_only_unexpired_native_running_work_is_active(env):
    task = await _seed_review_task(env, status="in_progress", with_cycle=False)
    async with env["maker"]() as session:
        stored = await session.get(Task, task.id)
        stored.execution_mode = "team"
        session.add(stored)
        cycle = TeamCycle(board_id=stored.board_id, task_id=stored.id, state="developing",
                          deadline_at=utcnow() + timedelta(minutes=10))
        session.add(cycle)
        await session.flush()
        member = TeamMember(board_id=stored.board_id, name="fixture-dev", role_kind="developer",
                            provider="omniroute-codex", model="gpt-6.1-sol")
        session.add(member)
        await session.flush()
        attempt = TeamAttempt(cycle_id=cycle.id, board_id=stored.board_id, task_id=stored.id,
            member_id=member.id, stage_key="dev-main", role_kind="developer", status="queued",
            provider="omniroute-codex", model="gpt-6.1-sol")
        session.add(attempt)
        await session.flush()
        async def state():
            return (await task_execution_state(session, board_id=stored.board_id, tasks=[stored]))[stored.id]
        assert (await state())["work_state"] == "waiting"
        attempt.status, attempt.started_at = "running", utcnow()
        attempt.lease_expires_at = utcnow() + timedelta(minutes=1)
        session.add(attempt)
        await session.flush()
        assert (await state())["work_state"] == "running"
        assert (await state())["work_started_at"] == attempt.started_at
        foreign = Task(board_id=stored.board_id, title="other", status="in_progress")
        other_board = Board(organization_id=env["org_id"], name="other", slug=str(uuid4()))
        session.add_all([foreign, other_board])
        await session.flush()
        for row, field, wrong, original in (
            (attempt, "task_id", foreign.id, stored.id),
            (attempt, "board_id", other_board.id, stored.board_id),
            (cycle, "board_id", other_board.id, stored.board_id),
        ):
            setattr(row, field, wrong)
            session.add(row)
            await session.flush()
            assert (await state())["work_state"] == "waiting"
            setattr(row, field, original)
            session.add(row)
            await session.flush()
        assert (await state())["work_state"] == "running"
        attempt.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(attempt)
        await session.flush()
        assert (await state())["work_state"] == "waiting"
        attempt.lease_expires_at = utcnow() + timedelta(hours=1)
        session.add(attempt)
        await session.flush()
        assert (await state())["work_expires_at"] == cycle.deadline_at
        cycle.deadline_at = utcnow() - timedelta(seconds=1)
        session.add(cycle)
        await session.flush()
        assert (await state())["work_state"] == "waiting"
        from app.services.team_cycle import PUBLISH_LEASE_SECONDS
        cycle.state, cycle.deadline_at = "publishing", utcnow() + timedelta(hours=3)
        cycle.updated_at = utcnow()
        session.add(cycle)
        await session.flush()
        assert (await state())["work_state"] == "running"
        assert (await state())["work_expires_at"] == cycle.updated_at + timedelta(seconds=PUBLISH_LEASE_SECONDS)
        cycle.updated_at = utcnow() - timedelta(seconds=PUBLISH_LEASE_SECONDS + 1)
        session.add(cycle)
        await session.flush()
        assert (await state())["work_state"] == "waiting"


async def test_review_lease_tracks_work_without_changing_goal_usage_or_fingerprint(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        stored = await session.get(Task, task.id)
        stored.tokens_used, stored.max_tokens_budget = 123, 456
        stored.max_seconds_budget, stored.budget_exhausted_at = 60, utcnow()
        session.add(stored)
        await session.commit()
        stamp, goal, exhausted = stored.updated_at, stored.description, stored.budget_exhausted_at
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        response = await client.post(path, headers=AUTH, json=_claim(candidate))
        assert response.status_code == 200, response.text
        lease_id = response.json()["lease_id"]
        assert (await _candidate(client, task.id))["fingerprint"] == candidate["fingerprint"]
        assert (await client.post(path, headers=AUTH, json=_claim(candidate))).status_code == 409
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            state = (await task_execution_state(session, board_id=stored.board_id, tasks=[stored]))[stored.id]
            event = await session.get(ActivityEvent, UUID(lease_id))
            deadline = lease_data(event)["deadline"]
            assert state["work_state"] == "reviewing"
            assert state["work_detail"].startswith("Reviewing · ")
            assert stored.status == "review" and stored.updated_at == stamp
            assert (stored.description, stored.tokens_used, stored.max_tokens_budget,
                    stored.max_seconds_budget, stored.budget_exhausted_at.replace(tzinfo=None)) == (
                        goal, 123, 456, 60, exhausted.replace(tzinfo=None))
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "heartbeat"})).status_code == 200
        async with env["maker"]() as session:
            event = await session.get(ActivityEvent, UUID(lease_id))
            assert lease_data(event)["deadline"] == deadline
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "release"})).status_code == 200
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            state = (await task_execution_state(session, board_id=stored.board_id, tasks=[stored]))[stored.id]
            assert state["work_state"] is None and stored.status == "review"


@pytest.mark.parametrize("stop", ["paused", "disabled", "policy", "comment", "deadline"])
async def test_review_stops_cannot_resume_or_renew_work(env, stop):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        response = await client.post(path, headers=AUTH, json=_claim(candidate))
        assert response.status_code == 200, response.text
        lease_id = response.json()["lease_id"]
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            if stop == "paused":
                stored.status, stored.paused_from_status = "paused", "review"
                session.add(stored)
            if stop == "comment":
                session.add(ActivityEvent(event_type="task.comment", task_id=stored.id,
                                          board_id=stored.board_id, message="owner changed instructions"))
            if stop == "deadline":
                event = await session.get(ActivityEvent, UUID(lease_id))
                data = lease_data(event)
                data["deadline"] = (utcnow() - timedelta(seconds=1)).isoformat()
                event.message = json.dumps(data)
                session.add(event)
            await session.commit()
        if stop == "disabled":
            mc_sidecar.set_model_meta(*REVIEWER_MODELS[0], disabled=True)
        if stop == "policy":
            set_automation_enabled(False)
        response = await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "heartbeat"})
        assert response.status_code in (403, 409), response.text
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "release"})).status_code == 200
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            assert stored.status == ("paused" if stop == "paused" else "review")


async def test_stale_review_result_or_release_does_not_destroy_a_fresh_sibling(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        old_claim = _claim(candidate)
        old = await client.post(path, headers=AUTH, json=old_claim)
        assert old.status_code == 200, old.text
        old_id = old.json()["lease_id"]
        async with env["maker"]() as session:
            event = await session.get(ActivityEvent, UUID(old_id))
            data = lease_data(event)
            data["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
            event.message = json.dumps(data)
            session.add(event)
            await session.commit()
        fresh = await client.post(path, headers=AUTH, json=_claim(candidate))
        assert fresh.status_code == 200, fresh.text
        fresh_id = fresh.json()["lease_id"]
        decision = {**old_claim, "kind": "task", "id": str(task.id), "expected_status": "review",
            "review_lease_id": old_id, "action": "accept", "impact": "routine",
            "evidence": "fixture", "published_sha": "a" * 40}
        decision.pop("timeout_seconds")
        response = await client.post("/api/v1/review/decisions", headers=AUTH, json=decision)
        assert response.status_code == 409, response.text
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": old_id, "action": "release"})).status_code == 200
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": fresh_id, "action": "heartbeat"})).status_code == 200
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            assert stored.status == "review"
            assert (await task_execution_state(session, board_id=stored.board_id,
                                              tasks=[stored]))[stored.id]["work_state"] == "reviewing"


async def test_competing_review_claims_admit_only_one_real_owner(env):
    async with env["maker"]() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("row-lock admission race requires disposable PostgreSQL")
    set_automation_enabled(True)
    mc_sidecar.set_model_meta(*REVIEWER_MODELS[0], max_concurrency=2)
    task = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
    async def claim():
        async with _client(env) as client:
            return await client.post(path, headers=AUTH, json=_claim(candidate))
    first, second = await asyncio.gather(claim(), claim())
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    async with env["maker"]() as session:
        rows = (await session.exec(select(ActivityEvent).where(
            ActivityEvent.task_id == task.id, ActivityEvent.event_type == "review.lease",
        ))).all()
        assert len(rows) == 1


@pytest.mark.parametrize("change", ["parent_pause", "parent_resume", "parent_goal", "cycle"])
@pytest.mark.parametrize("finished", [False, True])
async def test_review_final_decision_rechecks_parent_and_cycle_authority(env, change, finished):
    from app.services import team_cycle as svc

    set_automation_enabled(True)
    parent = await _seed_review_task(env, status="in_progress", with_cycle=False)
    task = await _seed_review_task(env)
    async with env["maker"]() as session:
        root = await session.get(Task, parent.id)
        root.execution_mode, root.split_type, root.split_count = "team", "dag", 1
        child = await session.get(Task, task.id)
        child.parent_task_id, child.execution_mode, child.execution_lane = root.id, "team", "harness"
        child.auto_reason = f"native-backlog:{root.id}:{svc.task_fingerprint(root)}"
        cycle = (await session.exec(select(TeamCycle).where(TeamCycle.task_id == child.id))).one()
        cycle.task_fingerprint = svc.task_fingerprint(child)
        session.add_all([root, child, cycle])
        await session.commit()
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        claim = _claim(candidate)
        response = await client.post(path, headers=AUTH, json=claim)
        assert response.status_code == 200, response.text
        lease_id = response.json()["lease_id"]
        if finished:
            assert (await client.post(path + "/update", headers=AUTH,
                json={"lease_id": lease_id, "action": "finish"})).status_code == 200
        async with env["maker"]() as session:
            root = await session.get(Task, parent.id)
            cycle = (await session.exec(select(TeamCycle).where(TeamCycle.task_id == task.id))).one()
            if change == "cycle":
                cycle.version += 1  # snapshot fingerprint is unchanged; continuation authority is not
                session.add(cycle)
            else:
                if change == "parent_goal":
                    root.description += " new goal"
                else:
                    root.status = "paused"
                    session.add(root)
                    await session.commit()
                    if change == "parent_resume":
                        root.status = "in_progress"
                root.updated_at = utcnow()
                session.add(root)
            await session.commit()
        decision = _decision_body(candidate, review_lease_id=lease_id,
                                  reviewer_session_id=claim["reviewer_session_id"])
        for action in ("heartbeat", "finish"):
            response = await client.post(path + "/update", headers=AUTH,
                                         json={"lease_id": lease_id, "action": action})
            assert response.status_code == 409, response.text
        response = await client.post("/api/v1/review/decisions", headers=AUTH, json=decision)
        assert response.status_code == 409, response.text
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "release"})).status_code == 200
        if change in ("parent_pause", "parent_goal"):
            response = await client.post(path, headers=AUTH, json=_claim(await _candidate(client, task.id)))
            assert response.status_code == 409, response.text
        async with env["maker"]() as session:
            assert (await session.get(Task, task.id)).status == "review"


@pytest.mark.parametrize("ended", ["expired", "finished_expired", "released"])
async def test_once_leased_review_cannot_omit_its_authority(env, ended):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        claim = _claim(candidate)
        response = await client.post(path, headers=AUTH, json=claim)
        assert response.status_code == 200, response.text
        lease_id = response.json()["lease_id"]
        if ended == "finished_expired":
            assert (await client.post(path + "/update", headers=AUTH,
                json={"lease_id": lease_id, "action": "finish"})).status_code == 200
        if ended == "released":
            assert (await client.post(path + "/update", headers=AUTH,
                json={"lease_id": lease_id, "action": "release"})).status_code == 200
        else:
            async with env["maker"]() as session:
                event = await session.get(ActivityEvent, UUID(lease_id))
                data = lease_data(event)
                data["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
                event.message = json.dumps(data)
                session.add(event)
                await session.commit()
        response = await client.post("/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, review_lease_id=lease_id,
                                reviewer_session_id=claim["reviewer_session_id"]))
        assert response.status_code == 409, response.text
        for session_id in (claim["reviewer_session_id"], str(uuid4())):
            response = await client.post("/api/v1/review/decisions", headers=AUTH,
                json=_decision_body(candidate, reviewer_session_id=session_id))
            assert response.status_code == 409, response.text
        async with env["maker"]() as session:
            assert (await session.get(Task, task.id)).status == "review"


async def test_finished_reviewer_frees_activity_not_final_decision_fences(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    other = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        claim = _claim(candidate)
        response = await client.post(path, headers=AUTH, json=claim)
        assert response.status_code == 200, response.text
        lease_id = response.json()["lease_id"]
        async with env["maker"]() as session:
            before = lease_data(await session.get(ActivityEvent, UUID(lease_id)))
            assert await review_load(session)
        finish = {"lease_id": lease_id, "action": "finish"}
        assert (await client.post(path + "/update", headers=AUTH, json=finish)).status_code == 200
        assert (await client.post(path + "/update", headers=AUTH, json=finish)).json()["already_finished"]
        assert (await client.post(path + "/update", headers=AUTH,
            json={"lease_id": lease_id, "action": "heartbeat"})).status_code == 409
        async with env["maker"]() as session:
            stored = await session.get(Task, task.id)
            data = lease_data(await session.get(ActivityEvent, UUID(lease_id)))
            assert data["deadline"] == before["deadline"] and data["expires_at"] == before["expires_at"]
            assert not await review_load(session)
            assert len(await live_review_leases(session, [task.id], include_finished=True)) == 1
            assert (await task_execution_state(session, board_id=stored.board_id, tasks=[stored]))[stored.id]["work_state"] is None
        # The same task cannot launch a duplicate, but another reviewer can use the freed slot.
        assert (await client.post(path, headers=AUTH, json=_claim(candidate))).status_code == 409
        other_candidate = await _candidate(client, other.id)
        assert (await client.post(f"/api/v1/review/tasks/{other.id}/review-lease", headers=AUTH,
                                  json=_claim(other_candidate))).status_code == 200
        response = await client.post("/api/v1/review/decisions", headers=AUTH,
            json=_decision_body(candidate, review_lease_id=lease_id,
                                reviewer_session_id=claim["reviewer_session_id"]))
        assert response.status_code == 200, response.text
        async with env["maker"]() as session:
            assert (await session.get(Task, task.id)).status == "done"  # fixture only


async def test_unqualified_or_stale_claim_has_no_authority(env):
    set_automation_enabled(True)
    task = await _seed_review_task(env)
    path = f"/api/v1/review/tasks/{task.id}/review-lease"
    async with _client(env) as client:
        candidate = await _candidate(client, task.id)
        for change, status in (({"reviewer_provider": "vllm", "reviewer_model": "Qwen3.8-27B-FP8"}, 403),
                               ({"fingerprint": "0" * 64}, 409), ({"timeout_seconds": True}, 422),
                               ({"timeout_seconds": 901}, 422)):
            response = await client.post(path, headers=AUTH, json={**_claim(candidate), **change})
            assert response.status_code == status, response.text
        async with env["maker"]() as session:
            assert not (await session.exec(select(ActivityEvent).where(
                ActivityEvent.task_id == task.id, ActivityEvent.event_type == "review.lease",
            ))).all()
