"""Synthetic admission only: no real Jira, credentials or inference."""

import importlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.api.model_ranking import RankingEntryMeta
from app.services import mc_sidecar, model_ranking


@pytest.fixture
def trust(tmp_path, monkeypatch):
    module = importlib.import_module("app.core.private_access")
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    path = tmp_path / "openclaw.mc.json"
    data = {
        "models": {
            module.LOCAL_PROVIDER: {
                module.LOCAL_MODEL: {
                    "fully_trusted": True,
                    "level": 3,
                }
            }
        }
    }
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", path)
    return module, path, data


@pytest.mark.parametrize("flag", [None, False, "true", "unknown", 1, [], {}])
def test_flag_missing_or_not_literal_true_denies(trust, flag):
    module, path, data = trust
    meta = data["models"][module.LOCAL_PROVIDER][module.LOCAL_MODEL]
    if flag is None:
        meta.pop("fully_trusted")
    else:
        meta["fully_trusted"] = flag
    path.write_text(json.dumps(data))
    with pytest.raises(PermissionError, match="jira_local_trust_required"):
        module.require_jira_access(
            module.LOCAL_PROVIDER, module.LOCAL_MODEL, module.LOCAL_ENDPOINT
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "omniroute-api"),
        ("provider", "vllm"),
        ("model", "gpt-6.1-sol"),
        ("endpoint", "https://cloud.invalid/v1"),
        ("endpoint", "http://192.0.2.10:8000/v1/"),
        ("method", "POST"),
        ("method", "PUT"),
        ("method", "DELETE"),
    ],
)
def test_flag_does_not_authorize_other_lane_or_writes(trust, field, value):
    module, _, _ = trust
    args = dict(
        provider=module.LOCAL_PROVIDER,
        model=module.LOCAL_MODEL,
        endpoint=module.LOCAL_ENDPOINT,
        method="GET",
    )
    args[field] = value
    with pytest.raises(PermissionError):
        module.require_jira_access(**args)


@pytest.mark.parametrize(
    "damage", ["missing", "malformed", "writable", "symlink", "bad_shape"]
)
def test_unknown_or_unsafe_configuration_denies(trust, damage):
    module, path, _ = trust
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("{")
    elif damage == "writable":
        path.chmod(0o666)
    elif damage == "bad_shape":
        path.write_text('{"models": []}')
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises(PermissionError):
        module.require_jira_access(
            module.LOCAL_PROVIDER, module.LOCAL_MODEL, module.LOCAL_ENDPOINT
        )


def test_cloud_flag_true_is_still_denied(trust):
    module, path, data = trust
    data["models"]["omniroute-api"] = {module.LOCAL_MODEL: {"fully_trusted": True}}
    path.write_text(json.dumps(data))
    with pytest.raises(PermissionError):
        module.require_jira_access(
            "omniroute-api", module.LOCAL_MODEL, module.LOCAL_ENDPOINT
        )


def test_exact_trusted_local_read_is_admitted(trust):
    module, _, _ = trust
    module.require_jira_access(
        module.LOCAL_PROVIDER, module.LOCAL_MODEL, module.LOCAL_ENDPOINT
    )


@pytest.mark.parametrize("flag", [True, False])
async def test_existing_metadata_persists_and_board_exposes_flag(trust, flag):
    module, path, _ = trust
    payload = RankingEntryMeta(
        provider=module.LOCAL_PROVIDER, model=module.LOCAL_MODEL, fully_trusted=flag
    )
    assert payload.fully_trusted is flag
    model_ranking.set_entry_meta(
        payload.provider, payload.model, {"fully_trusted": flag}
    )
    model_ranking.set_entry_meta(payload.provider, payload.model, {"rank": 2})
    assert (
        json.loads(path.read_text())["models"][payload.provider][payload.model][
            "fully_trusted"
        ]
        is flag
    )
    board = await model_ranking.get_board([])
    entries = [entry for row in board["levels"] for entry in row["entries"]]
    assert entries[0]["fully_trusted"] is flag


@pytest.mark.parametrize("flag", [None, "true", 1, [], {}])
def test_api_does_not_coerce_unknown_flags(flag):
    with pytest.raises(ValidationError):
        RankingEntryMeta(provider="synthetic", model="synthetic", fully_trusted=flag)


def test_api_omission_is_not_a_flag_update():
    payload = RankingEntryMeta(provider="synthetic", model="synthetic")
    assert payload.fully_trusted is False
    assert "fully_trusted" not in payload.model_fields_set


@pytest.mark.parametrize("fault", ["unflagged", "endpoint"])
def test_reader_denies_before_input_and_network_when_unflagged(
    trust, monkeypatch, fault
):
    _, path, _ = trust
    if fault == "unflagged":
        path.write_text('{"models": {}}')
    source = Path(__file__).parents[2] / "scripts/private-local-review.py"
    spec = importlib.util.spec_from_file_location("private_trust_reader_test", source)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    if fault == "endpoint":
        monkeypatch.setattr(reader, "HOST", "cloud.invalid")
    read = Mock(side_effect=AssertionError("input must not be read"))
    network = Mock(side_effect=AssertionError("network must not be used"))
    monkeypatch.setattr(reader, "read_document", read)
    monkeypatch.setattr(reader, "call_model", network)
    assert reader.review(Path("/never-read")) == {
        "status": "refused",
        "reason": "local_review_failed_closed",
    }
    read.assert_not_called()
    network.assert_not_called()
