"""Capacity policy, hierarchical managers and repository module reservations."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.tasks import Task
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, DEV_SHA, INTEGRATED, RANKING, claim, env, to_qa


async def planned(s, board, task, scope, ranking=RANKING):
    cycle, _ = await svc.start_cycle(s, board, task, ranking)
    pm, token = await claim(s, cycle)
    plan = {"workers": [{"key": "write", "scope": [scope]}]}
    await svc.complete_attempt(
        s, board, cycle, pm, token, ok=True, artifact=json.dumps(plan), ranking=ranking
    )
    return cycle


async def another_task(s, board):
    t = Task(
        id=uuid4(),
        board_id=board.id,
        title="other",
        status="in_progress",
        execution_lane="harness",
        execution_mode="team",
        complexity=3,
    )
    s.add(t)
    await s.commit()
    return t


async def test_module_reservation_lasts_until_cycle_ends_not_only_worker_exit(env):
    s, board, task = env
    a = await planned(s, board, task, "backend/app")
    writer, token = await claim(s, a)
    await svc.complete_attempt(
        s, board, a, writer, token, ok=True, head_sha=DEV_SHA, changed_files=["backend/app/a.py"]
    )
    b = await planned(s, board, await another_task(s, board), "backend/app/submodule")
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING) is None
    await svc.cancel_cycle(s, a, "operator cancelled")
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING)


async def test_different_modules_dispatch_at_the_same_time_and_balance_providers(env):
    s, board, task = env
    a = await planned(s, board, task, "backend/api")
    b = await planned(s, board, await another_task(s, board), "frontend/components")
    x, _ = await svc.claim_next(s, a, base_sha=BASE, ranking=RANKING)
    y, _ = await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING)
    assert x.status == y.status == "running"
    assert x.provider != y.provider  # enabled paid Neuralwatt participates


async def test_account_slots_are_shared_across_claude_models(env):
    s, board, task = env
    r = deepcopy(RANKING)
    for e in r["levels"][0]["entries"]:
        e["eligible"] = e["provider"] == "claude-code" and not e["disabled"]
        e["max_concurrency"] = 1
    a, _ = await svc.start_cycle(s, board, task, r)
    b, _ = await svc.start_cycle(s, board, await another_task(s, board), r)
    assert await svc.claim_next(s, a, base_sha=BASE, ranking=r)
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=r) is None


async def test_automatic_attempt_reselects_without_mutating_shared_roster(env):
    s, board, task = env
    team = await svc.ensure_team(s, board, RANKING, 3)
    for kind in ('pm', 'developer', 'qa'):
        team[kind].created_by = 'pool'
    r = deepcopy(RANKING)
    for e in r['levels'][0]['entries']:
        if e['provider'] == 'claude-code': e['eligible'] = False
    before = {k: updated.model_dump() for k, updated in team.items()}
    updated = await svc.ensure_team(s, board, r, 3)
    assert {k: m.model_dump() for k, m in updated.items()} == before
    cycle, _ = await svc.start_cycle(s, board, task, r)
    attempt, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=r)
    assert attempt.provider != 'claude-code'


async def test_manual_provider_disable_fences_a_running_worker(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    a, token = await claim(s, cycle)
    r = deepcopy(RANKING)
    for e in r["levels"][0]["entries"]:
        if (e["provider"], e["model"]) == (a.provider, a.model):
            e["disabled"] = True
    with pytest.raises(svc.TeamFenced, match="provider disabled"):
        await svc.heartbeat(s, cycle, a, token, ranking=r)
    assert cycle.state == "cancelled"


async def test_qa_receives_exact_base_and_safe_durable_stage_evidence(env):
    from app.models.team import TeamMember
    s, board, task = env
    cycle, _ = await to_qa(s, board, task)
    qa, token = await claim(s, cycle)
    member = await s.get(TeamMember, qa.member_id)
    text = svc.brief(task, cycle, qa, member, None, history=await svc._attempts(s, cycle.id))
    assert f'Exact review base SHA: {BASE}' in text
    assert f'Exact integration SHA: {INTEGRATED}' in text
    assert 'NOT the original PM reply' in text
    assert 'attribution AFTER each sandbox exits' in text
    assert 'Durable host attempt evidence' in text
    assert '"status": "succeeded"' in text and '"capability": "write_worktree"' in text
    assert 'List every defect you reproduce' in text and 'Convergence rule' not in text


async def test_correction_round_qa_converges_on_previous_findings(env):
    from app.models.team import TeamMember
    s, board, task = env
    cycle, _ = await to_qa(s, board, task)
    qa, token = await claim(s, cycle)
    cycle.qa_feedback = "CORRECTION_KIND: CODE\nRedis labels unchecked\nVERDICT: FAIL"
    member = await s.get(TeamMember, qa.member_id)
    text = svc.brief(task, cycle, qa, member, None, history=await svc._attempts(s, cycle.id))
    assert 'Redis labels unchecked' in text
    assert 'FAIL only when a previous finding is not fixed' in text and '`Follow-ups:`' in text
    assert token not in text and qa.claim_hash not in text


async def test_new_production_tree_requires_independent_qa_again(env):
    s, board, task = env
    cycle, _ = await to_qa(s, board, task)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(
        s, board, cycle, qa, token, ok=True, artifact="checked", head_sha=INTEGRATED, verdict="pass"
    )
    await svc.refresh_integration(s, cycle, "e" * 40, "f" * 40, cycle.version)
    assert cycle.state == "qa" and cycle.reviewed_sha is None
    with pytest.raises(svc.TeamError):
        await svc.reserve_publication(s, cycle, INTEGRATED, cycle.version)
    qa2, token2 = await claim(s, cycle, base="f" * 40)
    assert qa2.attempt_no == 2
    await svc.complete_attempt(
        s,
        board,
        cycle,
        qa2,
        token2,
        ok=True,
        artifact="rechecked",
        head_sha="e" * 40,
        verdict="pass",
    )
    assert cycle.reviewed_sha == "e" * 40


def test_api_runtime_copies_only_selected_static_provider_not_oauth(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "runtime", Path(__file__).parents[2] / "scripts/mc_pi_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "models.json"
    source.write_text(
        json.dumps(
            {
                "providers": {
                    "neuralwatt": {
                        "apiKey": "fake-test-only",
                        "models": [{"id": "glm"}, {"id": "other"}],
                    },
                    "unrelated": {"apiKey": "must-not-copy"},
                }
            }
        )
    )
    (tmp_path / "settings.json").write_text(json.dumps({
        "packages": ["must-not-load"], "extensions": ["must-not-load"],
        "shellCommandPrefix": "must-not-run",
        "compaction": {"enabled": True, "reserveTokens": 8192, "keepRecentTokens": 20000,
                       "modelOverrides": {"neuralwatt/glm": {
                           "reserveTokens": 16384, "keepRecentTokens": 12000}}},
    }))
    runtime = module.key_runtime("neuralwatt", "glm", tmp_path, source)
    data = json.loads((runtime / "models.json").read_text())
    assert list(data["providers"]) == ["neuralwatt"]
    assert data["providers"]["neuralwatt"]["models"] == [{"id": "glm"}]
    assert json.loads((runtime / "auth.json").read_text()) == {}
    assert (runtime / "models.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((runtime / "settings.json").read_text()) == {
        "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 12000}}
    assert (runtime / "settings.json").stat().st_mode & 0o777 == 0o600


def test_api_runtime_refuses_invalid_compaction_before_copying(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "runtime_invalid", Path(__file__).parents[2] / "scripts/mc_pi_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "models.json"
    source.write_text(json.dumps({"providers": {"test": {"models": [{"id": "fixture"}]}}}))
    for invalid in (-1, True, "16384", 2**53):
        (tmp_path / "settings.json").write_text(json.dumps({
            "compaction": {"reserveTokens": invalid}}))
        with pytest.raises(RuntimeError, match="invalid Pi compaction"):
            module.key_runtime("test", "fixture", tmp_path, source)
        assert not (tmp_path / "api-runtime").exists()


def test_watchdog_has_execution_deadlines_but_no_token_spending_trigger():
    import inspect
    from app.services.watchdog_loop import _enforce_task_budgets
    code=inspect.getsource(_enforce_task_budgets)
    assert "THEN 'wall-clock'" in code
    assert "THEN 'token'" not in code
    assert 't.tokens_used > t.max_tokens_budget' not in code


async def _planned_verify(s, board, task, scope):
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    pm, token = await claim(s, cycle)
    plan = {"workers": [{"key": "verify", "scope": [scope], "mode": "verify"}]}
    await svc.complete_attempt(
        s, board, cycle, pm, token, ok=True, artifact=json.dumps(plan), ranking=RANKING
    )
    return cycle


async def test_read_only_verifier_neither_holds_nor_waits_for_a_writer_scope(env):
    """2026-10-07 stage 2: a verifier on backend/tests held three writer cycles."""
    s, board, task = env
    verifying = await _planned_verify(s, board, task, "backend/app")
    verifier, _ = await svc.claim_next(s, verifying, base_sha=BASE, ranking=RANKING)
    assert svc.attempt_capability(verifier.role_kind, verifier.assignment) == "read_only"
    writing = await planned(s, board, await another_task(s, board), "backend/app/submodule")
    writer, _ = await svc.claim_next(s, writing, base_sha=BASE, ranking=RANKING)
    assert writer.status == "running"  # the verifier does not lock its scope
    later = await _planned_verify(s, board, await another_task(s, board), "backend/app")
    assert await svc.claim_next(s, later, base_sha=BASE, ranking=RANKING)  # nor waits for writers


async def test_pr_board_cycles_do_not_lock_each_others_scope(env):
    # 2026-10-24: one running PR-board stage held 6 other tasks idle. PR-publishing boards
    # start every cycle from upstream, so serializing them protects nothing.
    s, board, task = env
    a = await planned(s, board, task, "internal/container")
    assert await svc.claim_next(s, a, base_sha=BASE, ranking=RANKING, independent=True)
    b = await planned(s, board, await another_task(s, board), "internal/container/compose")
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING) is None  # MC-style boards still serialize
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING, independent=True)


async def test_scope_locks_never_cross_boards(env):
    from app.models.boards import Board
    s, board, task = env
    a = await planned(s, board, task, "docs")
    assert await svc.claim_next(s, a, base_sha=BASE, ranking=RANKING)
    other = Board(id=uuid4(), organization_id=board.organization_id, name="o", slug="o", gateway_id=board.gateway_id)
    s.add(other)
    await s.commit()
    b = await planned(s, other, await another_task(s, other), "docs/operations")
    assert await svc.claim_next(s, b, base_sha=BASE, ranking=RANKING)  # another board may be another repo


async def test_held_frontier_engines_let_a_local_engine_draft_a_hard_leaf(env):
    from tests.test_team_cycle import entry
    s, board, task = env
    task.complexity = 5
    a = await planned(s, board, task, "internal/scan")
    local = entry("metis", "qwen3.8", level=3, max_complexity=3, cost_class="local", write_enabled=True, max_concurrency=2)
    busy = {"max_level_for_complexity": RANKING["max_level_for_complexity"], "levels": [{"entries": [
        entry("claude-code", "sonnet", max_concurrency=1), local]}]}
    b = await planned(s, board, await another_task(s, board), "frontend")
    other, _ = await svc.claim_next(s, b, base_sha=BASE, ranking=busy)
    assert other.provider == "claude-code"
    assert await svc.claim_next(s, a, base_sha=BASE, ranking=busy) is None  # only busy: wait, no draft
    held = {**busy, "levels": [{"entries": [
        {**entry("claude-code", "sonnet"), "unavailable_until": "9999-01-01T00:00:00+00:00"}, local]}]}
    dev, _ = await svc.claim_next(s, a, base_sha=BASE, ranking=held)
    assert (dev.provider, dev.model) == ("metis", "qwen3.8")  # held: the local engine drafts


async def test_held_owner_pinned_pm_drafts_the_plan_locally_but_qa_and_leads_never_do(env):
    from types import SimpleNamespace
    from tests.test_team_cycle import entry
    s, board, task = env
    task.complexity = 5
    team = await svc.ensure_team(s, board, RANKING, 3)
    team["pm"].created_by, team["pm"].provider, team["pm"].model = "admin", "claude-code", "sonnet"  # owner pin
    await s.commit()
    held = {"max_level_for_complexity": RANKING["max_level_for_complexity"], "levels": [{"entries": [
        {**entry("claude-code", "sonnet"), "eligible": False, "unavailable_until": "9999-01-01T00:00:00+00:00"},
        entry("metis", "qwen3.8", level=3, max_complexity=3, cost_class="local", write_enabled=True, max_concurrency=2)]}]}
    cycle, _ = await svc.start_cycle(s, board, task, held)
    pm, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    assert (pm.role_kind, pm.provider) == ("pm", "metis")
    assert (team["pm"].provider, team["pm"].created_by) == ("claude-code", "admin")  # the pin is untouched
    disabled = {**held, "levels": [{"entries": [{**held["levels"][0]["entries"][0], "disabled": True}, held["levels"][0]["entries"][1]]}]}
    assert not svc._held(disabled, "claude-code", "sonnet")  # an owner-disabled engine is not drafted around
    member = lambda kind, name: SimpleNamespace(role_kind=kind, name=name)
    assert not svc._draftable(member("qa", "team-qa"), {})
    assert svc._draftable(member("pm", "team-pm"), {})


@pytest.mark.parametrize("error", ["interrupted: attempt time limit reached; partial output discarded",
                                   "Connection error."])
async def test_a_timed_out_local_draft_waits_for_the_frontier_engine_instead_of_blocking(env, error):
    from tests.test_team_cycle import entry
    s, board, task = env
    task.complexity = 5
    team = await svc.ensure_team(s, board, RANKING, 3)
    team["pm"].created_by, team["pm"].provider, team["pm"].model = "admin", "claude-code", "sonnet"
    await s.commit()
    local = entry("metis", "qwen3.8", level=3, max_complexity=3, cost_class="local", write_enabled=True, max_concurrency=2)
    held = {"max_level_for_complexity": RANKING["max_level_for_complexity"], "levels": [{"entries": [
        {**entry("claude-code", "sonnet"), "eligible": False, "unavailable_until": "9999-01-01T00:00:00+00:00"}, local]}]}
    cycle, _ = await svc.start_cycle(s, board, task, held)
    pm, tok = await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    assert pm.provider == "metis"
    await svc.complete_attempt(s, board, cycle, pm, tok, ok=False, ranking=held, error=error)
    assert cycle.state != "blocked"  # 0000a002, 2026-10-24: the slow draft blocked the cycle
    retry = (await svc._attempts(s, cycle.id))[-1]
    assert (retry.attempt_no, retry.status, retry.provider) == (2, "queued", "claude-code")
    with pytest.raises(svc.TeamError):  # still held: wait, no second local draft
        await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    free = {**held, "levels": [{"entries": [entry("claude-code", "sonnet"), local]}]}
    again, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=free)
    assert (again.id, again.provider) == (retry.id, "claude-code")


async def test_an_unreachable_local_draft_engine_is_a_hold_not_a_stage_failure(env):
    # 2026-10-26: codex was held and metis' vLLM refused connections. Each stage's second
    # attempt drafted on metis, failed in under a second and blocked 22 cycles "after 2 attempts".
    from tests.test_team_cycle import entry
    s, board, task = env
    team = await svc.ensure_team(s, board, RANKING, 3)
    team["pm"].created_by, team["pm"].provider, team["pm"].model = "admin", "claude-code", "sonnet"
    await s.commit()
    local = entry("metis", "qwen3.8", level=3, max_complexity=3, cost_class="local", write_enabled=True, max_concurrency=2)
    sonnet_held = {**entry("claude-code", "sonnet"), "eligible": False, "unavailable_until": "9999-01-01T00:00:00+00:00"}
    held = {"max_level_for_complexity": RANKING["max_level_for_complexity"], "levels": [{"entries": [sonnet_held, local]}]}
    cycle, _ = await svc.start_cycle(s, board, task, held)
    pm, tok = await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    pm.attempt_no = 2  # the frontier attempt 1 already failed on the quota
    await svc.complete_attempt(s, board, cycle, pm, tok, ok=False, ranking=held, error="Connection error.")
    assert cycle.state != "blocked"
    retry = (await svc._attempts(s, cycle.id))[-1]
    assert (retry.attempt_no, retry.status, retry.provider) == (3, "queued", "claude-code")
    # Every local engine held as well: the claim reports the pin's hold, which the runner waits out.
    both = {**held, "levels": [{"entries": [sonnet_held, {**local, "eligible": False,
                                                          "unavailable_until": "9999-01-01T00:00:00+00:00"}]}]}
    retry.assignment = json.dumps({**json.loads(retry.assignment), "no_local": False})
    with pytest.raises(svc.TeamError, match=r"claude-code/sonnet is disabled or ineligible$"):
        await svc.claim_next(s, cycle, base_sha=BASE, ranking=both)


@pytest.mark.parametrize("hold_ended", [False, True])
async def test_a_stored_local_draft_engine_is_still_a_draft_at_claim_time(env, hold_ended):
    # 2026-10-24/25: a failed complexity-5 PM draft was retried on its stored local engine (a
    # skipped claim pass can store one too). Each claim then read vllm as a plain assignment, the
    # local complexity ceiling skipped it, and two cycles idled 24h until 'cycle deadline
    # exceeded' (0000a00f, 0000a014), long after the frontier hold had ended.
    from tests.test_team_cycle import entry
    s, board, task = env
    task.complexity = 5
    team = await svc.ensure_team(s, board, RANKING, 3)
    team["pm"].created_by, team["pm"].provider, team["pm"].model = "admin", "claude-code", "sonnet"
    await s.commit()
    local = entry("metis", "qwen3.8", level=3, max_complexity=3, cost_class="local", write_enabled=True, max_concurrency=2)
    held = {"max_level_for_complexity": RANKING["max_level_for_complexity"], "levels": [{"entries": [
        {**entry("claude-code", "sonnet"), "eligible": False, "unavailable_until": "9999-01-01T00:00:00+00:00"}, local]}]}
    cycle, _ = await svc.start_cycle(s, board, task, held)
    pm, tok = await svc.claim_next(s, cycle, base_sha=BASE, ranking=held)
    await svc.complete_attempt(s, board, cycle, pm, tok, ok=False, ranking=held,
                               error="worker perf-harness needs 1..10 safe relative scope paths")
    retry = (await svc._attempts(s, cycle.id))[-1]
    assert (retry.attempt_no, retry.status, retry.provider) == (2, "queued", "metis")
    free = {**held, "levels": [{"entries": [entry("claude-code", "sonnet"), local]}]}
    again, _ = await svc.claim_next(s, cycle, base_sha=BASE, ranking=free if hold_ended else held)
    assert (again.id, again.provider) == (retry.id, "claude-code" if hold_ended else "metis")


def test_retired_manager_roles_are_rejected():
    """Owner 2026-10-26: the three manager roles never ran in a week; a plan of several
    workers becomes child tasks instead (team_cycle._split)."""
    plan = {"workers": [{"key": "m", "role": "manager:module-lead", "scope": ["backend"]}]}
    with pytest.raises(svc.TeamError, match="not an allowed profile"):
        svc.parse_plan(json.dumps(plan))
