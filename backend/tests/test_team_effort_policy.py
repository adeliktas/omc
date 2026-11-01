"""Effort follows role and difficulty, keeps owner pins, and is what the CLI receives."""

import json
from types import SimpleNamespace

import pytest

from app.services import team_cycle as svc
from tests.test_backlog_independent_regressions import runner
from tests.test_team_cycle import RANKING, env  # noqa: F401  (fixture)


def _member(created_by="pool", thinking="low"):
    return SimpleNamespace(created_by=created_by, thinking_level=thinking)


@pytest.mark.parametrize(
    "role,complexity,expected",
    [
        ("developer", 1, "low"),  # owner 2026-10-22: start cheap, escalate on failure
        ("developer", 2, "low"),
        ("developer", 3, "medium"),
        ("developer", 4, "medium"),
        ("developer", 5, "high"),
        ("pm", 1, "medium"),
        ("pm", 5, "high"),
        ("qa", 1, "medium"),  # QA follows risk, never below medium
        ("qa", 4, "medium"),
        ("specialist", 2, "low"),
    ],
)
def test_policy_table(role, complexity, expected):
    effort = svc.resolve_effort(role, complexity, _member(), "pi-neuralwatt")
    assert effort["requested"] == effort["effective"] == expected
    assert effort["reason"].startswith("policy:") and effort["clamped"] is False


def test_owner_pin_is_kept_and_reported():
    effort = svc.resolve_effort("developer", 1, _member("admin", "high"), "pi-neuralwatt")
    assert effort == {"requested": "high", "effective": "high", "reason": "owner pin on member",
                      "clamped": False}


def test_claude_clamps_to_levels_its_cli_accepts():
    low = svc.resolve_effort("developer", 1, _member(), "claude-code")
    assert (low["requested"], low["effective"], low["clamped"]) == ("low", "low", False)
    assert svc.resolve_effort("developer", 5, _member(), "claude-code", escalate=1)["effective"] == "xhigh"


async def test_attempts_record_requested_and_effective_effort(env):  # noqa: F811
    s, board, task = env
    task.complexity = 4
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    (pm,) = await svc._attempts(s, cycle.id)
    effort = json.loads(pm.assignment)["effort"]
    assert pm.thinking_level == effort["effective"]
    assert effort["requested"] == "high" and effort["reason"] == "policy: pm at complexity 4"


def test_claude_receives_the_effort_it_was_given():
    r = runner()
    argv, _ = r.harness_cmd("claude-code", "opus-5-5", "high", "x", "/tmp/s", "abc12345")
    assert argv[argv.index("--effort") + 1] == "high"
    argv, _ = r.harness_cmd("claude-code", "opus-5-5", "minimal", "x", "/tmp/s", "abc12345")
    assert "--effort" not in argv  # never send a level the CLI rejects


def test_model_setting_wins_over_member_pins_and_policy(monkeypatch, tmp_path):
    """Owner 2026-10-26: Qwen3.8-27B always thinks at xhigh, set once on /models."""
    from app.services import mc_sidecar, model_ranking

    monkeypatch.undo()  # the real per-model lookup (conftest disables the host's)
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    model_ranking.set_entry_meta("vllm", "Qwen3.8-27B-FP8", {"thinking_level": "xhigh"})
    pinned = svc.resolve_effort("specialist", 1, _member("admin", "low"), "vllm", model="Qwen3.8-27B-FP8")
    assert (pinned["requested"], pinned["effective"], pinned["reason"]) == ("xhigh", "xhigh", "owner setting on model")
    other = svc.resolve_effort("specialist", 1, _member(), "metis", model="qwen3.8")
    assert other["reason"].startswith("policy:") and other["effective"] == "low"
    model_ranking.set_entry_meta("vllm", "Qwen3.8-27B-FP8", {"thinking_level": None})  # back to policy
    assert svc.resolve_effort("pm", 5, _member(), "vllm", model="Qwen3.8-27B-FP8")["reason"].startswith("policy:")
    assert "thinking_level" not in mc_sidecar.get_model_meta("vllm", "Qwen3.8-27B-FP8")


async def test_overview_reports_roles_members_and_real_use(env):  # noqa: F811
    from app.api.model_ranking import get_overview

    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    await session.commit()
    out = await get_overview(db=session, _actor=None)
    roles = {(r["kind"], r["slug"]): r for r in out["roles"]}
    pm = roles[("pm", "project-manager")]
    assert pm["attempts"] == 1 and pm["succeeded"] == 0 and pm["capability"] == "read_only"
    assert pm["members"] and pm["members"][0]["board"] == board.name
    assert pm["effort"] == {1: "medium", 2: "medium", 3: "medium", 4: "high", 5: "high"}
    assert roles[("qa", "qa-engineer")]["effort"][1] == "medium"  # QA's floor
    assert ("pm", "module-lead") not in roles  # manager roles retired 2026-10-26
    assert len(pm["members"]) == 1  # the canonical team-pm only, never a retired team-manager-* row
    (engine, n), = pm["engines"].items()
    assert n == 1 and out["models"][engine]["roles"] == {"pm": 1}
    assert out["levels"][-2:] == ["xhigh", "max"]
