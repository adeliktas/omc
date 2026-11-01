"""T110 — Model Ranking Board: eligibility + selection math.

Pure-function tests (monkeypatched flows DB + sidecar) — the smallest checks
that fail if the ranking/budget logic breaks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services import model_ranking, mc_sidecar


@pytest.fixture()
def flows_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "llm_flows.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER
        )
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(model_ranking, "_FLOWS_DB", db)
    return db


def _insert_flow(db: Path, model: str, tokens: int, ts: str = "2100-01-01T00:00:00") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO flows (ts, model, prompt_tokens, completion_tokens, total_tokens) VALUES (?,?,?,?,?)",
        (ts, model, tokens, 0, tokens),
    )
    conn.commit()
    conn.close()


def _window(limit_tokens: int = 1000, seconds: int = 604800) -> list[dict]:
    return [{"seconds": seconds, "limit_tokens": limit_tokens}]


# ── usage + eligibility ─────────────────────────────────────────────────────


def test_percent_budget_reports_usage_without_blocking(flows_db):
    meta = {"id": "glm-5.3-flash", "budget": {"type": "percent", "windows": _window(1000)}}
    usage = model_ranking.compute_usage("zai", "glm-5.3-flash", meta)
    assert usage[0]["used_pct"] == 0.0
    assert model_ranking.eligibility(usage) == (True, None)

    _insert_flow(flows_db, "zai/glm-5.3-flash", 1500)  # prefixed form matches
    usage = model_ranking.compute_usage("zai", "glm-5.3-flash", meta)
    assert usage[0]["used"] == 1500
    ok, reason = model_ranking.eligibility(usage)
    assert ok is True and reason is None


def test_credits_budget_burns_by_pricing(flows_db):
    meta = {
        "id": "deepseek-v4-pro",
        "budget": {"type": "credits", "windows": [{"seconds": 604800, "limit_credits": 1.0}]},
        "pricing": {"in_per_mtok": 1000.0, "out_per_mtok": 0.0},
    }
    _insert_flow(flows_db, "deepseek/deepseek-v4-pro", 500)  # 500 tok * $1000/Mtok = $0.5
    usage = model_ranking.compute_usage("deepseek", "deepseek-v4-pro", meta)
    assert usage[0]["used"] == pytest.approx(0.5)
    assert usage[0]["used_pct"] == 50.0
    assert model_ranking.eligibility(usage)[0] is True

    _insert_flow(flows_db, "deepseek/deepseek-v4-pro", 600)
    usage = model_ranking.compute_usage("deepseek", "deepseek-v4-pro", meta)
    assert model_ranking.eligibility(usage)[0] is True


def test_local_budget_always_eligible(flows_db):
    meta = {"id": "Qwen3.8-27B-FP8", "budget": None}
    assert model_ranking.compute_usage("vllm", "Qwen3.8-27B-FP8", meta) == []
    assert model_ranking.eligibility([]) == (True, None)


# ── board + picks ────────────────────────────────────────────────────────────


def _entry(level: int, cost: str, rank: int = 0, model: str = "m") -> dict:
    return {
        "provider": "p", "model": model, "level": level, "rank": rank,
        "cost_class": cost, "iq_score": 50, "disabled": False,
        "provider_status": "online", "eligible": True, "block_reason": None,
        "usage": [], "budget": {}, "budget_type": "local", "pricing": None,
    }


def test_picks_cheapest_sufficient_preferring_local():
    entries = [
        _entry(1, "credits", model="frontier"),
        _entry(4, "local", model="jetson"),     # cheapest sufficient for L1/L2 tasks
        _entry(4, "free", model="freecloud"),
        _entry(5, "free", model="tiny"),        # too weak for complexity 2
    ]
    picks = model_ranking._picks_by_complexity(entries)
    # complexity 2 allows level <= 4 -> jetson (level 4, local) beats freecloud
    assert picks[2]["model"] == "jetson"
    # complexity 1 allows level <= 5 -> tiny (level 5, cheapest)
    assert picks[1]["model"] == "tiny"
    # complexity 5 requires level <= 1 -> frontier
    assert picks[5]["model"] == "frontier"
    # complexity 4 allows <= 2 -> frontier is the only one
    assert picks[4]["model"] == "frontier"


def test_picks_none_when_nothing_eligible():
    entries = [_entry(1, "credits", model="frontier")]
    picks = model_ranking._picks_by_complexity(entries)
    # frontier (level 1) satisfies every max_level 1..5
    assert all(v["model"] == "frontier" for v in picks.values())
    off = [_entry(9, "local", model="off")]  # level 9 never allowed
    assert all(v is None for v in model_ranking._picks_by_complexity(off).values())


def test_disabled_and_offline_block_pick():
    e_off = _entry(4, "local", model="jetson")
    e_off["provider_status"] = "offline"
    e_off["eligible"] = False
    picks = model_ranking._picks_by_complexity([e_off])
    assert all(v is None for v in picks.values())


# ── sidecar persistence (uses real sidecar write path, tmp redirect) ────────


def test_apply_moves_and_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    written = model_ranking.apply_moves(
        [{"provider": "vllm", "model": "Qwen3.8-27B-FP8", "level": 4}]
    )
    assert written == 1
    assert mc_sidecar.get_model_meta("vllm", "Qwen3.8-27B-FP8")["level"] == 4

    model_ranking.seed_default_ranking()
    # idempotent per-field: level not overwritten (still 4 from the move),
    # other missing fields (cost_class) filled in
    assert mc_sidecar.get_model_meta("vllm", "Qwen3.8-27B-FP8")["level"] == 4
    assert mc_sidecar.get_model_meta("vllm", "Qwen3.8-27B-FP8")["cost_class"] == "local"
    # Every CLI harness is ranked and enabled. Subscription harnesses carry the
    # sanction note; metered pi-neuralwatt instead gets a capped credits window.
    expected = model_ranking._AGENT_HARNESS_MODELS
    # Owner 2026-10-06: superseded models are no longer seeded or re-enabled.
    for superseded in (
        ("pi-neuralwatt", "glm-5.3-flash"),
        ("openai-codex-2", "gpt-5.6-sol"),
        ("openai-codex-2", "gpt-5.6-terra"),
        ("claude-code", "opus-5"),
        ("claude-code", "opus-4-8"),
        ("claude-code", "sonnet"),
        ("ollama-cloud-sub", "glm-5.2"),
    ):
        assert superseded not in expected
        assert mc_sidecar.get_model_meta(*superseded) == {}
    for (provider, model), (level, rank, priority) in expected.items():
        meta = mc_sidecar.get_model_meta(provider, model)
        assert (meta["level"], meta["rank"], meta["provider_priority"]) == (
            level, rank, priority,
        )
        assert meta["disabled"] is False
        if provider == "pi-neuralwatt":
            assert "disabled_reason" not in meta
            assert meta["budget"] == model_ranking._METERED_BUDGET
            assert meta["budget"]["type"] != "metered_uncapped"
            assert meta["pricing"] == model_ranking._METERED_PRICING
        else:
            assert "Sanctioned" in meta["disabled_reason"]
            assert meta["budget"] == {"type": "subscription"}


def test_seed_exposes_owner_acceptance_trio(monkeypatch, tmp_path):
    """2026-10-05: the owner-authorized acceptance reviewer trio is seeded.

    gpt-6.1-sol, opus-5-5 and the FULL pi-neuralwatt glm-5.3 (never flash,
    never an older/smaller substitute) are ranked level 1. Seeding only fills
    absent keys, so an existing same-account availability hold recorded
    BEFORE the seed ran is never bypassed, and a manual disable survives.
    """
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    # Pre-existing genuine hold + manual disable on the trio's accounts.
    mc_sidecar.set_model_meta(
        "openai-codex-2", "gpt-5.6-sol",
        unavailable_until="2999-01-01T00:00:00+00:00",
        availability_reason="usage_limit",
    )
    mc_sidecar.set_model_meta(
        "claude-code", "opus-5-5", disabled=True, disabled_reason="owner paused"
    )

    seeded = model_ranking.seed_default_ranking()
    trio = {
        ("openai-codex-2", "gpt-6.1-sol"): (1, 5, 1),
        ("claude-code", "opus-5-5"): (1, 6, 0),
        ("pi-neuralwatt", "glm-5.3"): (1, 7, 4),
    }
    for (provider, model), (level, rank, priority) in trio.items():
        assert (level, rank, priority) == model_ranking._AGENT_HARNESS_MODELS[
            (provider, model)
        ]
        meta = mc_sidecar.get_model_meta(provider, model)
        assert (meta["level"], meta["rank"], meta["provider_priority"]) == (
            level, rank, priority,
        )
    # The FULL glm-5.3 is seeded alongside — never silently replaced by flash.
    assert ("pi-neuralwatt", "glm-5.3") in model_ranking._AGENT_HARNESS_MODELS
    assert ("pi-neuralwatt", "glm-5.3-flash") not in model_ranking._AGENT_HARNESS_MODELS
    for key in (
        "openai-codex-2/gpt-6.1-sol (harness)",
        "claude-code/opus-5-5 (harness)",
        "pi-neuralwatt/glm-5.3 (harness)",
    ):
        assert key in seeded
    # Existing hold on the SAME account (gpt-5.6-sol shares openai-codex-2)
    # is untouched; the manual disable of opus-5-5 survives the seed.
    legacy = mc_sidecar.get_model_meta("openai-codex-2", "gpt-5.6-sol")
    assert legacy["unavailable_until"] == "2999-01-01T00:00:00+00:00"
    assert mc_sidecar.get_model_meta("claude-code", "opus-5-5")["disabled"] is True
    # Full glm-5.3 is metered like its flash sibling but is a distinct entry.
    full = mc_sidecar.get_model_meta("pi-neuralwatt", "glm-5.3")
    assert full["budget"] == model_ranking._METERED_BUDGET
    assert full["pricing"] == model_ranking._METERED_PRICING


@pytest.mark.asyncio
async def test_harnesses_are_eligible_but_separate_from_endpoint_picks(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    model_ranking.seed_default_ranking()

    board = await model_ranking.get_board([])
    entries = [entry for level in board["levels"] for entry in level["entries"]]
    harnesses = [entry for entry in entries if entry["execution"] == "agent_harness"]

    # 2026-10-07: 9 -> 10 with omniroute-codex/gpt-6.1-sol.
    assert len(harnesses) == len(model_ranking._AGENT_HARNESS_MODELS) == 10
    assert all(entry["provider_status"] == "harness" and entry["eligible"] for entry in harnesses)
    # Owner removed spending gates; shared-key usage remains observable.
    neuralwatt = [e for e in harnesses if e["provider"] == "pi-neuralwatt"]
    assert len(neuralwatt) == 1
    assert all(e["budget_type"] == "unlimited" for e in neuralwatt)
    assert {e["model"] for e in neuralwatt} == {"glm-5.3"}
    assert board["agent_harness_picks"]["5"]["model"] == "opus-5-5"
    assert all(pick is None for pick in board["complexity_picks"].values())

    # A superseded row that still exists keeps its hold for the whole account.
    mc_sidecar.set_model_meta(
        "claude-code", "opus-5",
        unavailable_until="2999-01-01T00:00:00+00:00",
        availability_reason="session_limit",
    )
    board = await model_ranking.get_board([])
    held = [
        entry
        for level in board["levels"]
        for entry in level["entries"]
        if entry["provider"] == "claude-code"
    ]
    assert all(not entry["eligible"] and entry["block_reason"] == "session_limit" for entry in held)
    assert board["agent_harness_picks"]["5"]["provider"] == "openai-codex-1"
    assert board["agent_harness_picks"]["2"]["model"] == "gpt-5.6-terra"

    for model in ("gpt-5.6-sol", "gpt-5.6-terra"):
        mc_sidecar.set_model_meta(
            "openai-codex-1", model,
            unavailable_until="2999-01-01T00:00:00+00:00",
            availability_reason="usage_limit",
        )
    board = await model_ranking.get_board([])
    # 2026-10-07: the OmniRoute codex pool (rank 2) now precedes the single codex-2 account.
    assert board["agent_harness_picks"]["5"]["provider"] == "omniroute-codex"


def test_usage_reporting_and_cloud_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    model_ranking.seed_default_ranking()
    model_ranking.record_usage(
        "openai-codex-1", "gpt-5.6-sol",
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
         "cost_usd": 0.02, "success": True},
    )
    usage = mc_sidecar.get_model_meta("openai-codex-1", "gpt-5.6-sol")["observed_usage"]
    assert (usage["calls"], usage["total_tokens"], usage["cost_usd"]) == (1, 15, 0.02)

    mc_sidecar.set_model_meta(
        "openai-codex-2", "gpt-5.6-sol", disabled=True, disabled_reason="superseded"
    )
    model_ranking.apply_cloud_policy()
    # The owner policy never re-enables a superseded model it no longer lists.
    assert mc_sidecar.get_model_meta("openai-codex-2", "gpt-5.6-sol")["disabled"] is True
    local = mc_sidecar.get_model_meta("vllm", "Qwen3.8-27B-FP8")
    assert local["disabled"] is True
    assert mc_sidecar.get_model_meta("deepseek-sub", "deepseek-v4-pro")["max_complexity"] == 3


def test_agent_harnesses_are_never_routing_targets():
    """Unblocking either harness must not open the OmniRoute endpoint path."""
    from app.services import model_router

    assert model_router._model_id_to_mc_provider("claude-code/opus-5") is None
    assert model_router._model_id_to_mc_provider("openai-codex/gpt-5.6-sol") is None
    assert model_router._model_id_to_mc_provider("openai-codex-2/gpt-5.6-sol") is None
    assert model_router._model_id_to_mc_provider("ollama-cloud-sub/glm-5.2") is None
    assert model_router._model_id_to_mc_provider("deepseek-sub/deepseek-v4-pro") is None
    assert model_router._model_id_to_mc_provider("vllm/Qwen3.8-27B-FP8") == "vllm"
