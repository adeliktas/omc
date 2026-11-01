# ruff: noqa: S101
"""Patch 1 (2026-07-16): typed classification of the free-provider
`/models` probe. Alive semantics must exactly match the original probe
(HTTP < 500 = alive); only `last_error` becomes actionable. No real HTTP —
the classifier is pure.
"""

from __future__ import annotations

import json

from app.services.watchdog_loop import _classify_models_probe


def _body(model_ids: list[str]) -> str:
    return json.dumps({"object": "list", "data": [{"id": m} for m in model_ids]})


def test_connection_failure() -> None:
    alive, err = _classify_models_probe(None, None, ConnectionError("refused"))
    assert alive is False
    assert err == "conn-fail: ConnectionError"


def test_http_500_offline() -> None:
    alive, err = _classify_models_probe(502, "bad gateway", None)
    assert alive is False
    assert err == "http-502"


def test_auth_codes_alive_but_typed() -> None:
    # Preserves original semantics: <500 counted alive. Now visible.
    for code in (401, 403):
        alive, err = _classify_models_probe(code, "", None)
        assert alive is True
        assert err is not None and err.startswith(f"auth-{code}")


def test_quota_429_alive_but_typed() -> None:
    alive, err = _classify_models_probe(429, "", None)
    assert alive is True
    assert err is not None and err.startswith("quota-429")


def test_other_4xx_typed() -> None:
    alive, err = _classify_models_probe(404, "", None)
    assert alive is True
    assert err == "http-404"


def test_malformed_json_alive_but_typed() -> None:
    alive, err = _classify_models_probe(200, "<html>proxy error</html>", None)
    assert alive is True
    assert err is not None and err.startswith("malformed-response")


def test_empty_model_list_alive_but_typed() -> None:
    alive, err = _classify_models_probe(200, _body([]), None)
    assert alive is True
    assert err is not None and err.startswith("empty-model-list")


def test_healthy_response_is_clean() -> None:
    alive, err = _classify_models_probe(200, _body(["Qwen3.6-35B-A3B-FP8"]), None)
    assert alive is True
    assert err is None


def test_alive_semantics_match_original_probe() -> None:
    """Original: `r.status_code < 500` == alive; exception == dead."""
    for code, expected_alive in [(200, True), (401, True), (429, True),
                                 (404, True), (500, False), (503, False)]:
        alive, _ = _classify_models_probe(code, "", None)
        assert alive is expected_alive, f"code {code}"


def test_local_url_scoping() -> None:
    """Patch 1.2: autorecover may only probe loopback/RFC-1918 hosts —
    keyless cloud endpoints (kilocode incident) stay owner-managed."""
    from app.services.watchdog_loop import _is_probeable_local_url as ok

    assert ok("http://127.0.0.1:8000/v1")
    assert ok("http://localhost:1234/v1")
    assert ok("http://192.168.1.126:8000/v1")
    assert ok("http://10.0.0.5/v1")
    assert ok("http://172.16.0.1/v1")
    assert ok("http://172.31.255.1/v1")
    assert not ok("http://172.32.0.1/v1")  # outside RFC-1918 172 range
    assert not ok("https://api.kilo.ai/api/gateway")  # the incident
    assert not ok("https://api.deepseek.com/v1")
    assert not ok("")
    assert not ok("not a url")


def test_local_url_extra_hosts_allowlist(monkeypatch) -> None:
    from app.services.watchdog_loop import _is_probeable_local_url as ok

    monkeypatch.setenv("WATCHDOG_AUTORECOVER_EXTRA_HOSTS", "my-nas.lan, other.host")
    assert ok("http://my-nas.lan:8000/v1")
    assert ok("http://OTHER.HOST/v1")
    assert not ok("https://api.kilo.ai/api/gateway")


def test_autorecover_skips_cloud_urls_in_source() -> None:
    import inspect

    from app.services import watchdog_loop as wl

    src = inspect.getsource(wl._autorecover_providers)
    assert "_is_probeable_local_url" in src


def test_autorecover_executes_with_empty_provider_table(monkeypatch) -> None:
    """Regression: run the real `_autorecover_providers` body end-to-end
    with a stubbed session — catches broken local imports (a dropped
    `sa_select` import produced a runtime-only NameError on first deploy)
    that pure classifier tests cannot see.
    """
    import asyncio

    import app.services.watchdog_loop as wl

    class _Scalars:
        def all(self) -> list:
            return []

    class _Result:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _FakeDb:
        async def execute(self, _stmt):
            return _Result()

        async def commit(self) -> None:
            return None

    class _FakeCtx:
        async def __aenter__(self) -> _FakeDb:
            return _FakeDb()

        async def __aexit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr(wl, "async_session_maker", lambda: _FakeCtx())
    flips = asyncio.run(wl._autorecover_providers())
    assert flips == 0
