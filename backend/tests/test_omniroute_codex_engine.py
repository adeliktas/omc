"""Pi can reach codex models through OmniRoute's account pool as an equivalent engine."""

from app.services import model_ranking, model_router
from app.services import team_cycle as svc
from tests.test_backlog_independent_regressions import runner


def test_omniroute_codex_is_a_supported_harness_engine():
    assert svc.engine_supported("omniroute-codex", "gpt-6.1-sol")
    assert svc.engine_supported("omniroute-codex", "gpt-6-luna")
    assert not svc.engine_supported("omniroute-codex", "gpt-5.6-sol")
    assert model_ranking._is_agent_harness_provider("omniroute-codex")
    assert ("omniroute-codex", "gpt-6.1-sol") in model_ranking._AGENT_HARNESS_MODELS
    assert "omniroute-codex" in model_router._NON_ROUTABLE_PROVIDERS


def test_runner_maps_omniroute_codex_to_pi_omniroute_provider():
    argv, env = runner().harness_cmd("omniroute-codex", "gpt-6.1-sol", "high", "brief", "/tmp/s", "abc12345")
    assert argv[argv.index("--provider") + 1] == "omniroute-api"
    assert argv[argv.index("--model") + 1] == "codex/gpt-6.1-sol"
    assert argv[argv.index("--thinking") + 1] == "high"
    assert env == {"PI_CODING_AGENT_DIR": "/root/.pi/agent"}
    assert argv[0] == "/root/.pi/agent/bin/pi"
    luna, _ = runner().harness_cmd("omniroute-codex", "gpt-6-luna", "low", "brief", "/tmp/s", "luna")
    assert luna[luna.index("--model") + 1] == "codex/gpt-6-luna"
    from app.services.owner_reviews import is_reviewer_engine
    assert not is_reviewer_engine("omniroute-codex", "gpt-6-luna")
    assert not is_reviewer_engine("openai-codex-2", "gpt-6-luna")


def test_omniroute_codex_is_the_same_engine_as_direct_codex_for_qa_independence():
    assert svc.engine_identity("omniroute-codex", "gpt-6.1-sol") == svc.engine_identity("openai-codex-2", "gpt-6.1-sol")
    assert svc.engine_identity("omniroute-codex", "gpt-6.1-sol") != svc.engine_identity("claude-code", "opus-5-5")


def test_only_canonical_engine_holds_suppress_the_card_comment():
    r = runner()
    path = "mc: HTTP 422 POST /api/v1/boards/" + "b" * 36 + "/tasks/" + "t" * 36 + "/team/cycles/" + "c" * 36 + "/claims: "
    assert r.engine_on_hold(r.ApiError(422, path + "pi-neuralwatt/glm-5.3-flash is temporarily unavailable"))
    assert r.engine_on_hold(r.ApiError(409, path + "team-pm (admin-assigned): pi-neuralwatt/glm-5.3 is disabled or ineligible"))
    assert not r.engine_on_hold(r.ApiError(503, "mc: HTTP 503 POST /x: database is temporarily unavailable"))
    assert not r.engine_on_hold(r.ApiError(409, "mc: HTTP 409 POST /x: publication service is temporarily unavailable"))
    assert not r.engine_on_hold(RuntimeError("pi-neuralwatt/glm-5.3 is temporarily unavailable"))
