# ruff: noqa: INP001
"""Pytest configuration shared across backend tests."""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests should fail fast if auth-mode wiring breaks, but still need deterministic
# defaults during import-time settings initialization, regardless of shell env.
os.environ["AUTH_MODE"] = "local"
os.environ["LOCAL_AUTH_TOKEN"] = "test-local-token-0123456789-0123456789-0123456789x"
os.environ["BASE_URL"] = "http://localhost:8000"
# The team runner honours FIXTURE_* hooks only in this explicit test mode.
os.environ["RUNNER_FIXTURES"] = "1"
# Most team tests exercise the in-cycle multi-leaf path (still used past the split
# ancestry bound); test_team_split.py covers plans splitting into child tasks.
os.environ["TEAM_SPLIT_MIN_WORKERS"] = "99"
os.environ["INTAKE_LOG"] = "/nonexistent/omc-intake.log"  # host scripts never log into, or read, the live intake log
# Host scripts resolve state, logs, locks and fleet config under OMC_DATA: tests never touch the host's.
os.environ["OMC_DATA"] = tempfile.mkdtemp(prefix="omc-test-data-")
PRIVATE_BOARD = "00000000-0000-4000-8000-00000000b0a7"  # the one private (local-models-only) test board
os.environ["PRIVATE_BOARD_IDS"] = PRIVATE_BOARD
for _sub in ("etc/mc-team", "state", "log", "run"):
    Path(os.environ["OMC_DATA"], _sub).mkdir(parents=True, exist_ok=True)
Path(os.environ["OMC_DATA"], "etc/mc-team/fleet.json").write_text(json.dumps({
    "private_board_ids": [PRIVATE_BOARD],
    "private_lane": {"local_provider": "local-qwen", "local_model": "Qwen3.8-27B-FP8",
                     "local_endpoint": "http://192.0.2.10:8000/v1", "router_model": "vllm/Qwen3.8-27B-FP8",
                     "router_endpoint": "http://192.0.2.20:20129/v1"},
    "github_poll": {"repo": "example/private-repo", "repo_id": 1000, "actor_id": 2000,
                    "actor_login": "example-owner", "pr": 148},
}))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_host_model_pins(monkeypatch):
    """Per-model thinking pins live in the host's sidecar file; tests never read the live one.
    A test of the pins calls monkeypatch.undo() and points mc_sidecar at a temporary file."""
    from app.services import team_cycle

    monkeypatch.setattr(team_cycle, "_model_effort", lambda provider, model: None)
