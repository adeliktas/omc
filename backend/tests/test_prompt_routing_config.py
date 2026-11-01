"""Policy-only changes must survive rebalancing without losing account/budget config."""

import importlib.util
from copy import deepcopy
from pathlib import Path

from app.services.omniroute_combo_sync import prompt_routing_config


def test_prompt_config_preserves_budget_and_member_metadata():
    original = {
        "auto": {"budgetCap": 0.01, "budgetFallback": "strict"},
        "memberContexts": {"vllm/Qwen3.8-27B-FP8": 229376},
        "maxRetries": 0,
        "disableSessionStickiness": True,
    }
    saved = deepcopy(original)
    result = prompt_routing_config(original)
    assert original == saved
    assert result["auto"]["budgetCap"] == 0.01
    assert result["auto"]["budgetFallback"] == "strict"
    assert result["auto"]["promptAwareRouting"] is True
    assert result["auto"]["complexityAwareRouting"] is True
    assert "complexityAwareRouting" not in result
    assert result["memberContexts"] == original["memberContexts"]
    assert prompt_routing_config(result) == result


def _balance():
    path = Path(__file__).resolve().parents[2] / "scripts/omniroute-balance.py"
    spec = importlib.util.spec_from_file_location("balance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rebalance_changes_strategy_without_membership_changes():
    module = _balance()
    member = {
        "id": "pinned",
        "model": "codex/gpt-5.5",
        "providerId": "codex",
        "connectionId": "approved-account",
        "weight": 1,
    }
    original = {
        "name": "omniroute-auto",
        "strategy": "round-robin",
        "models": [member],
        "config": {"maxRetries": 0, "memberContexts": {"codex/gpt-5.5": 272000}},
    }
    saved = deepcopy(original)
    body = module.balanced_combo(original, [])
    assert body["strategy"] == "auto"
    assert body["models"] == [member]
    assert original == saved
    assert body["config"]["maxRetries"] == 0
    assert module.balanced_combo(body, []) == body
    assert module.balanced_combo(body, [], round_robin=True)["strategy"] == "round-robin"


def test_rebalance_upgrades_members_to_newest_catalog_model():
    module = _balance()
    codex = {"id": "c1", "provider": "codex", "name": "a", "isActive": True}
    claude = {
        "id": "p1", "provider": "oc-p", "name": "Claude Code P", "isActive": True,
        "providerSpecificData": {"baseUrl": "http://h:20130/v1"},
    }
    neuralwatt = {"id": "n1", "provider": "oc-n", "name": "neuralwatt", "isActive": True}
    combo = {
        "name": "omniroute-auto",
        "models": [
            {"id": "m1", "model": "codex/gpt-5.5", "providerId": "codex",
             "connectionId": "c1", "weight": 1, "label": "Codex subscription 1"},
            {"id": "m2", "model": "oc-p/sonnet", "providerId": "oc-p",
             "connectionId": "p1", "weight": 1, "label": "Claude Code P"},
            {"id": "m3", "model": "oc-n/glm-5.3-flash", "providerId": "oc-n",
             "connectionId": "n1", "weight": 1, "label": "neuralwatt"},
        ],
        "config": {},
    }
    catalogs = {
        # numeric, not lexicographic: 5.10 beats 5.9; suffixed tiers never win
        "c1": ["gpt-5.5", "gpt-5.9-sol", "gpt-5.10-sol", "gpt-5.10-sol-max", "gpt-6-terra", None],
        "p1": ["haiku", "sonnet", "opus"],
        "n1": ["glm-5.3-flash"],  # newest line not served -> member untouched
    }
    body = module.balanced_combo(combo, [codex, claude, neuralwatt], catalogs)
    assert [m["model"] for m in body["models"]] == [
        "codex/gpt-5.10-sol", "oc-p/opus", "oc-n/glm-5.3-flash",
    ]
    assert body["models"][0]["label"] == "Codex subscription 1"
    assert body["config"]["memberContexts"] == {
        "codex/gpt-5.10-sol": 272000, "oc-p/opus": 200000,
    }
    assert module.balanced_combo(body, [codex, claude, neuralwatt], catalogs) == body
    # No catalog (fetch failed) never downgrades or drops a member.
    assert module.balanced_combo(combo, [codex, claude, neuralwatt])["models"] == combo["models"]


def test_rebalance_never_downgrades_or_drops_members():
    module = _balance()
    codex = {"id": "c1", "provider": "codex", "name": "a", "isActive": True}
    old = {"id": "c2", "provider": "codex", "name": "b", "isActive": True}
    combo = {
        "name": "omniroute-auto",
        "models": [
            {"id": "m1", "model": "codex/gpt-6.1-sol", "providerId": "codex",
             "connectionId": "c1", "weight": 3},
            {"id": "m2", "model": "codex/gpt-5.6-terra", "providerId": "codex",
             "connectionId": "c2", "weight": 0},
        ],
        "config": {},
    }
    catalogs = {"c1": ["gpt-5.6-sol"], "c2": ["gpt-5.6-sol"]}
    body = module.balanced_combo(combo, [codex, old], catalogs)
    # newer pinned model kept; weight-0 member kept and upgraded terra -> sol
    assert body["models"] == [
        combo["models"][0], {**combo["models"][1], "model": "codex/gpt-5.6-sol"},
    ]
    # a failed catalog keeps every member, weight 0 included
    assert module.balanced_combo(combo, [codex, old], {})["models"] == combo["models"]

    # an integer-major spelling is that major's .0 release
    assert module.latest_model(codex, ["gpt-5.10-sol", "gpt-6-sol"])[0] == "codex/gpt-6-sol"
    assert module.version("codex/gpt-6-sol") < module.version("codex/gpt-6.1-sol")


def test_rebalance_changes_only_members_with_a_known_older_model():
    module = _balance()
    codex = {"id": "c1", "provider": "codex", "name": "a", "isActive": True}
    unknown = ["gpt-5.6-unknown", "gpt-5.6.1-sol", "gpt-5.6-sol-preview",
               "gpt-5.6-solgarbage", "gpt-5.6-sol\n", "gpt-5x6-sol", "gpt-5.3-codex-spark"]
    older = ["gpt-5.5", "gpt-5.5-high", "gpt-5.6-luna", "gpt-5.6-terra-max", "gpt-6-sol"]
    for name in unknown + older:
        member = {"id": "m", "model": f"codex/{name}", "connectionId": "c1", "weight": 1}
        combo = {"name": "omniroute-auto", "models": [member], "config": {}}
        [result] = module.balanced_combo(combo, [codex], {"c1": ["gpt-6.1-sol"]})["models"]
        expected = "codex/gpt-6.1-sol" if name in older else f"codex/{name}"
        assert result["model"] == expected, name
