from app.services import team_cycle as svc

E = dict(execution="agent_harness", eligible=True, disabled=False)
RANKING = {
    "max_level_for_complexity": {"5": 1, "4": 2, "3": 3, "2": 4, "1": 5},
    "levels": [
        {"level": 1, "entries": [dict(E, provider="omniroute-codex", model="gpt-6.1-sol", level=1, max_complexity=5, cost_class="credits", max_concurrency=3)]},
        {"level": 2, "entries": [dict(E, provider="omniroute-codex", model="gpt-6-luna", level=2, max_complexity=4, cost_class="credits", max_concurrency=3, write_enabled=True)]},
        {"level": 3, "entries": [dict(E, provider="vllm", model="Qwen3.8-27B-FP8", execution="model_endpoint", level=3, max_complexity=3, cost_class="local", max_concurrency=1, write_enabled=True)]},
    ],
}


def pick(complexity, **kw):
    return svc.pick_engine(RANKING, complexity, **kw)


def test_frontier_plans_and_cheapest_sufficient_engine_implements(monkeypatch):
    monkeypatch.setattr(svc, "validate_engine", lambda *a, **k: None)
    monkeypatch.setattr(svc, "engine_supported", lambda *a: True)
    assert pick(3) == ("omniroute-codex", "gpt-6.1-sol")            # planning/review: strongest
    assert pick(3, write=True) == ("omniroute-codex", "gpt-6-luna")  # implementation: cheaper frontier
    assert pick(2, write=True) == ("vllm", "Qwen3.8-27B-FP8")        # simple implementation: local, free
    assert pick(2, write=True, load={("vllm", "Qwen3.8-27B-FP8"): 1}) == ("omniroute-codex", "gpt-6-luna")  # local busy: spread
    assert pick(5, write=True) == ("omniroute-codex", "gpt-6.1-sol")  # hardest work stays frontier


def test_effort_starts_cheap_and_escalates_on_failure_up_to_xhigh():
    from types import SimpleNamespace
    pool = SimpleNamespace(created_by="pool", thinking_level="medium")
    effort = lambda role, c, up=0: svc.resolve_effort(role, c, pool, "omniroute-codex", up)["effective"]
    assert [effort("developer", c) for c in (1, 2, 3, 4, 5)] == ["low", "low", "medium", "medium", "high"]
    assert effort("pm", 2) == "medium" and effort("pm", 5) == "high"
    assert effort("developer", 3, 1) == "high" and effort("developer", 3, 2) == "xhigh"
    assert effort("developer", 1, 9) == "xhigh"  # never above the ceiling


def test_held_frontier_lets_a_local_engine_draft_any_leaf(monkeypatch):
    # 2026-10-24: every codex session was out for hours and the fleet idled; local Qwen drafts
    # instead, qualified QA reviews it and a correction round builds on the draft.
    import pytest
    monkeypatch.setattr(svc, "engine_supported", lambda *a: True)

    def held(ranking, provider, model, **kw):
        if provider == "omniroute-codex":
            raise svc.TeamError("temporarily unavailable", 409)

    monkeypatch.setattr(svc, "validate_engine", held)
    with pytest.raises(svc.TeamError):
        pick(5, write=True)  # normal routing: no sufficient engine is available
    assert pick(5, write=True, draft=True) == ("vllm", "Qwen3.8-27B-FP8")
