"""Registered owner-qualified native QA keeps order, holds and no-weak-fallback."""
import pytest
from app.services import team_cycle as svc
from app.services.owner_reviews import REVIEWER_MODELS


def registry():
    rows = [{"provider": p, "model": m, "level": 1, "rank": 0,
             "provider_priority": 100, "execution": "agent_harness",
             "eligible": True, "disabled": False, "max_complexity": 5,
             "max_concurrency": 4} for p, m in REVIEWER_MODELS]
    rows.append({**rows[-1], "model": "glm-5.3-flash", "provider_priority": 0})
    return {"max_level_for_complexity": {str(i): 5 for i in range(1, 6)},
            "levels": [{"entries": rows}]}


def test_qualified_qa_preserves_order_even_same_model_and_busy_load():
    ranking = registry()
    assert svc._qualified_qa_registered(ranking)
    assert svc.pick_engine(ranking, 4, qa=True, avoid=REVIEWER_MODELS[0],
                           load={REVIEWER_MODELS[0]: 3}) == REVIEWER_MODELS[0]
    ranking["levels"][0]["entries"][0]["eligible"] = False
    assert svc.pick_engine(ranking, 4, qa=True) == REVIEWER_MODELS[1]
    ranking["levels"][0]["entries"][1]["disabled"] = True
    assert svc.pick_engine(ranking, 4, qa=True) == REVIEWER_MODELS[2]
    ranking["levels"][0]["entries"][2]["disabled"] = True
    assert svc.pick_engine(ranking, 4, qa=True) == REVIEWER_MODELS[3]


def test_all_qualified_unavailable_never_silently_substitutes_flash():
    ranking = registry()
    for row in ranking["levels"][0]["entries"][:len(REVIEWER_MODELS)]:
        row["eligible"] = False
    with pytest.raises(svc.TeamError, match="no eligible"):
        svc.pick_engine(ranking, 4, qa=True)
    # Capacity-filtered rows can contain only a weak engine. Caller passes
    # qa=True from the ORIGINAL registry, not the filtered candidate list.
    ranking["levels"][0]["entries"] = ranking["levels"][0]["entries"][len(REVIEWER_MODELS):]
    with pytest.raises(svc.TeamError, match="no eligible"):
        svc.pick_engine(ranking, 4, qa=True)


def test_canonical_qualified_opus_is_supported_without_enabling_any_hold():
    assert svc.engine_supported("claude-code", "opus-5-5")
    ranking = registry()
    ranking["levels"][0]["entries"][0]["eligible"] = False
    ranking["levels"][0]["entries"][1]["eligible"] = False
    ranking["levels"][0]["entries"][2]["unavailable_until"] = "9999-01-01T00:00:00+00:00"
    assert svc.pick_engine(ranking, 4, qa=True) == REVIEWER_MODELS[3]
