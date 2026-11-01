"""Strata (owner 2026-10-26): a local llama.cpp engine whose reliability is unproven drafts
only. draft_only keeps it out of normal routing until the owner clears the flag on /models."""

import pytest

from app.services import team_cycle as svc
from tests.test_backlog_independent_regressions import runner
from tests.test_team_backlog import entry

MODEL = "qwen3.8-flash-next-huihui-abliterated-ud-iq4_xs"
STRATA = entry("strata", MODEL, level=3, cost_class="local", write_enabled=True, max_complexity=3,
               max_concurrency=1, draft_only=True)


def board(*entries):
    return {"max_level_for_complexity": {"1": 5, "2": 4, "3": 3, "4": 2, "5": 1},
            "levels": [{"level": 3, "entries": list(entries)}]}


def test_draft_only_engine_drafts_but_is_never_routed():
    with pytest.raises(svc.TeamError):
        svc.pick_engine(board(STRATA), 1, write=True)  # normal routing skips it, even for easy work
    assert svc.pick_engine(board(STRATA), 5, write=True, draft=True) == ("strata", MODEL)
    assert svc.pick_engine(board({**STRATA, "draft_only": False}), 1, write=True) == ("strata", MODEL)


def test_strata_runs_through_its_pi_provider():
    assert "strata" in svc.LOCAL_PROVIDERS and svc.engine_supported("strata", MODEL)
    argv, _env = runner().harness_cmd("strata", MODEL, "high", "prompt", "/tmp/s", "attempt")
    assert argv[argv.index("--provider") + 1] == "strata-gfx1030"
    assert argv[argv.index("--model") + 1] == MODEL
