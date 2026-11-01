"""Large gateway prompts must not serialize every eligible provider behind one lease."""
from pathlib import Path

import yaml


def test_router_has_two_bounded_shared_heavy_slots_and_waits_for_transient_bursts():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yml").read_text())
    router = compose["services"]["omniroute"]
    env = router["environment"]
    assert router["image"] == "omniroute:v3.8.50-smart-teams-routing"  # no unreviewed router rebuild
    assert int(env["OMNIROUTE_CHAT_MAX_HEAVY_IN_FLIGHT"]) == 2
    assert int(env["OMNIROUTE_CHAT_ADMISSION_HEALTHY_HEADROOM"]) == 0
    assert int(env["OMNIROUTE_CHAT_ADMISSION_QUEUE_MS"]) == 60_000
    assert env["NODE_OPTIONS"] == "--max-old-space-size=2048"
    assert "OMNIROUTE_CHAT_HARD_MAX_BODY_BYTES" not in env
    assert "OMNIROUTE_CHAT_ADMISSION_MAX_QUEUED_BYTES" not in env
    assert "OMNIROUTE_API_KEY" not in env  # no admission bypass credential
