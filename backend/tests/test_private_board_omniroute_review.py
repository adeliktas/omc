"""Synthetic only: no real credentials, Jira, protected files or model traffic."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import mc_pi_runtime as shared
from app.core import private_access


@pytest.fixture
def router(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "private_router_test", Path(__file__).parents[2] / "scripts/private-local-review.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    monkeypatch.setattr(shared, "QWEN_SLOT", tmp_path / "qwen.lock")
    module.INPUT_ROOTS = (tmp_path,)
    module.OUTPUT_ROOT = tmp_path / "private-output"
    module.ROUTER_CONFIG = tmp_path / "models.json"
    config = {
        "providers": {
            module.ROUTER_PROVIDER: {
                "baseUrl": module.ROUTER_ENDPOINT,
                "api": "openai-completions",
                "apiKey": "synthetic-not-a-real-secret",
                "models": [{"id": module.ROUTER_MODEL}],
            }
        }
    }
    module.ROUTER_CONFIG.write_text(json.dumps(config))
    module.ROUTER_CONFIG.chmod(0o600)
    sidecar = tmp_path / "openclaw.mc.json"
    sidecar.write_text(
        json.dumps(
            {
                "models": {
                    module.ROUTER_PROVIDER: {
                        module.ROUTER_MODEL: {"fully_trusted": True}
                    }
                }
            }
        )
    )
    sidecar.chmod(0o600)
    return module


def review(router):
    return router.review(
        Path("/not-a-real-document"), router.ROUTER_PROVIDER, router.ROUTER_MODEL
    )


@pytest.mark.parametrize("flag", [False, None, "true", 1, {}, []])
def test_router_denied_before_private_config_input_or_network(
    router, monkeypatch, flag
):
    p = Path(router.os.environ["OPENCLAW_HOME"]) / "openclaw.mc.json"
    p.write_text(
        json.dumps(
            {
                "models": {
                    router.ROUTER_PROVIDER: {
                        router.ROUTER_MODEL: {"fully_trusted": flag}
                    }
                }
            }
        )
    )
    key = Mock(side_effect=AssertionError("must not inspect credentials"))
    read = Mock(side_effect=AssertionError("must not read protected input"))
    monkeypatch.setattr(router, "router_key", key)
    monkeypatch.setattr(router, "read_document", read)
    assert review(router)["status"] == "refused"
    key.assert_not_called()
    read.assert_not_called()


def test_router_flag_admits_only_exact_authorized_tuple(router):
    private_access.require_jira_access(
        router.ROUTER_PROVIDER, router.ROUTER_MODEL, router.ROUTER_ENDPOINT
    )
    for args in [
        ("cloud", router.ROUTER_MODEL, router.ROUTER_ENDPOINT, "GET"),
        (router.ROUTER_PROVIDER, "vllm/cloud-model", router.ROUTER_ENDPOINT, "GET"),
        (
            router.ROUTER_PROVIDER,
            router.ROUTER_MODEL,
            "https://cloud.invalid/v1",
            "GET",
        ),
        (router.ROUTER_PROVIDER, router.ROUTER_MODEL, router.ROUTER_ENDPOINT, "POST"),
    ]:
        with pytest.raises(PermissionError):
            private_access.require_jira_access(*args)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "public",
        "symlink",
        "endpoint",
        "headers",
        "missing_key",
        "duplicate_model",
    ],
)
def test_router_private_config_refuses_before_input(router, monkeypatch, damage):
    p = router.ROUTER_CONFIG
    cfg = json.loads(p.read_text())
    c = cfg["providers"][router.ROUTER_PROVIDER]
    if damage == "duplicate":
        p.write_text(
            '{"providers":{},"providers":' + json.dumps(cfg["providers"]) + "}"
        )
    elif damage == "public":
        p.chmod(0o644)
    elif damage == "symlink":
        p.rename(p.with_suffix(".original"))
        p.symlink_to(p.with_suffix(".original"))
    else:
        if damage == "endpoint":
            c["baseUrl"] = "https://cloud.invalid/v1"
        elif damage == "headers":
            c["headers"] = {"x-route-model": "cloud"}
        elif damage == "missing_key":
            c.pop("apiKey")
        elif damage == "duplicate_model":
            c["models"] *= 2
        p.write_text(json.dumps(cfg))
    read = Mock(side_effect=AssertionError("must not read input"))
    network = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(router, "read_document", read)
    monkeypatch.setattr(router.http.client, "HTTPConnection", network)
    assert review(router)["status"] == "refused"
    read.assert_not_called()
    network.assert_not_called()


@pytest.mark.parametrize("where", ["admission", "synthetic_inference"])
def test_unavailable_router_or_local_canary_refuses_before_protected_input(
    router, monkeypatch, where
):
    def request(document=None):
        if where == "synthetic_inference" and document is None:
            return {
                "admitted": True,
                "model": router.ROUTER_MODEL,
                "jira_method": "GET",
            }
        raise OSError("SYNTHETIC_PRIVATE_ERROR")

    read = Mock(side_effect=AssertionError("must not read input"))
    monkeypatch.setattr(router, "call_router_model", request)
    monkeypatch.setattr(router, "read_document", read)
    assert review(router) == {
        "status": "refused",
        "reason": "local_review_failed_closed",
    }
    read.assert_not_called()


def test_router_result_private_and_actual_input_only_after_synthetic_preflight(
    router, monkeypatch
):
    events = []

    def request(document=None):
        events.append(document)
        if document is None:
            return {
                "admitted": True,
                "model": router.ROUTER_MODEL,
                "jira_method": "GET",
            }
        return {
            "model": router.MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "SYNTHETIC_PRIVATE_RESULT",
                        "reasoning_content": "SYNTHETIC_PRIVATE_REASONING",
                    }
                }
            ],
        }

    def read(path):
        assert len(events) == 2 and events[0] is None
        return "SYNTHETIC_PRIVATE_INPUT"

    monkeypatch.setattr(router, "call_router_model", request)
    monkeypatch.setattr(router, "read_document", read)
    result = review(router)
    assert result["status"] == "completed_awaiting_disclosure_approval"
    assert (
        result["provider"] == router.ROUTER_PROVIDER
        and result["model"] == router.ROUTER_MODEL
    )
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    path = router.OUTPUT_ROOT / (result["job_id"] + ".json")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "SYNTHETIC_PRIVATE_REASONING" in path.read_text()
    assert events[-1] == "SYNTHETIC_PRIVATE_INPUT"


@pytest.mark.parametrize("document", [None, "synthetic"])
def test_fixed_router_transport_no_environment_proxy_or_extra_headers(
    router, monkeypatch, document
):
    result = (
        {"admitted": True, "model": router.ROUTER_MODEL, "jira_method": "GET"}
        if document is None
        else {
            "model": router.MODEL,
            "choices": [{"message": {"role": "assistant", "content": "synthetic"}}],
        }
    )
    response = Mock(status=200)
    response.read.return_value = json.dumps(result).encode()
    response.getheader.return_value = "no-store"
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(router.http.client, "HTTPConnection", factory)
    monkeypatch.setenv("HTTP_PROXY", "https://cloud.invalid")
    assert router.call_router_model(document) == result
    factory.assert_called_once_with("192.0.2.20", 20129, timeout=90)
    method, path, body, headers = connection.request.call_args.args
    assert path == "/v1/private/chat/completions"
    assert method == ("GET" if document is None else "POST")
    assert (
        headers["X-OMC-Model"] == router.ROUTER_MODEL
        and headers["X-OMC-Source-Method"] == "GET"
    )
    assert headers["Authorization"] == "Bearer synthetic-not-a-real-secret"
    if document is not None:
        parsed = json.loads(body)
        assert (
            json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode()
            == body
        )
        assert parsed["model"] == router.ROUTER_MODEL and parsed["stream"] is False
        assert parsed["messages"][1]["content"] == document
    connection.close.assert_called_once()


@pytest.mark.parametrize("admitted", [1, 1.0, False, "true", None, [], {}])
def test_router_admission_requires_literal_true(router, monkeypatch, admitted):
    response = Mock(status=200)
    response.getheader.return_value = "no-store"
    response.read.return_value = json.dumps(
        {"admitted": admitted, "model": router.ROUTER_MODEL, "jira_method": "GET"}
    ).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        router.http.client, "HTTPConnection", Mock(return_value=connection)
    )
    with pytest.raises(RuntimeError, match="local_inference_refused"):
        router.call_router_model()
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "fault",
    [
        "redirect",
        "unavailable",
        "no_store",
        "wrong_model",
        "tools",
        "legacy_function",
        "function_finish",
        "tools_finish",
        "oversize",
        "duplicate_json",
    ],
)
def test_router_response_refuses_without_fallback_or_diagnostic_leak(
    router, monkeypatch, fault
):
    response = Mock(
        status=302 if fault == "redirect" else 503 if fault == "unavailable" else 200
    )
    response.getheader.return_value = "public" if fault == "no_store" else "no-store"
    message = {"role": "assistant", "content": "SYNTHETIC_PRIVATE"}
    if fault == "tools":
        message["tool_calls"] = [{"function": {"name": "untrusted"}}]
    elif fault == "legacy_function":
        message["function_call"] = {"name": "untrusted", "arguments": "{}"}
    data = {
        "model": "cloud" if fault == "wrong_model" else router.MODEL,
        "choices": [{"message": message}],
    }
    if fault in ("function_finish", "tools_finish"):
        data["choices"][0]["finish_reason"] = (
            "function_call" if fault == "function_finish" else "tool_calls"
        )
    response.read.return_value = (
        b"x" * (router.MAX_RESPONSE + 1)
        if fault == "oversize"
        else b'{"model":"cloud","model":"Qwen3.8-27B-FP8"}'
        if fault == "duplicate_json"
        else json.dumps(data).encode()
    )
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(router.http.client, "HTTPConnection", factory)
    with pytest.raises((ValueError, RuntimeError)) as caught:
        router.call_router_model("synthetic")
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert factory.call_count == connection.request.call_count == 1
    connection.close.assert_called_once()


def test_router_trust_revoked_after_preflight_denies_before_private_input(
    router, monkeypatch
):
    calls = []

    def request(document=None):
        calls.append(document)
        if document is None:
            p = Path(router.os.environ["OPENCLAW_HOME"]) / "openclaw.mc.json"
            p.write_text('{"models":{}}')
            return {
                "admitted": True,
                "model": router.ROUTER_MODEL,
                "jira_method": "GET",
            }
        router.router_key()
        raise AssertionError("revoked trust must refuse")

    read = Mock(side_effect=AssertionError("must not read input"))
    monkeypatch.setattr(router, "call_router_model", request)
    monkeypatch.setattr(router, "read_document", read)
    assert review(router)["status"] == "refused"
    read.assert_not_called()
    assert calls == [None, router.ROUTER_CANARY]


def test_observed_usage_survives_later_input_failure_without_raw_payload(
    router, monkeypatch
):
    usage = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}

    def request(document=None):
        if document is None:
            return {
                "admitted": True,
                "model": router.ROUTER_MODEL,
                "jira_method": "GET",
            }
        return {"usage": usage, "SYNTHETIC_PRIVATE": "must never be in usage receipt"}

    monkeypatch.setattr(router, "call_router_model", request)
    monkeypatch.setattr(
        router, "read_document", Mock(side_effect=OSError("SYNTHETIC_PRIVATE"))
    )
    assert review(router)["status"] == "refused"
    receipts = list(router.OUTPUT_ROOT.glob("usage-*.json"))
    assert len(receipts) == 1 and receipts[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(receipts[0].read_text())["usage"] == usage
    assert "SYNTHETIC_PRIVATE" not in receipts[0].read_text()
    assert not [
        p for p in router.OUTPUT_ROOT.glob("*.json") if not p.name.startswith("usage-")
    ]
