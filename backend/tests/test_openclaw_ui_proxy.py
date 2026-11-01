"""The embedded Gateway keeps its paths, CSP, credentials and live protocol."""

import asyncio
import gzip
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect as LibraryConnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response as WebSocketResponse

from app.api import openclaw_ui_proxy as proxy


@pytest.mark.parametrize(
    "csp_fields, expected_policies",
    [
        (
            ["default-src 'self'; frame-ancestors 'none'", "script-src 'none'"],
            ["default-src 'self'", "script-src 'none'"],
        ),
        (
            ["default-src 'self'; frame-ancestors 'none', script-src 'none'"],
            ["default-src 'self', script-src 'none'"],
        ),
        (["frame-ancestors 'none'", "script-src 'none'"], ["script-src 'none'"]),
        (["FRAME-ANCESTORS\t'none'; script-src 'none'"], ["script-src 'none'"]),
        (
            ["frame-ancestors-extra 'self'; script-src 'none'"],
            ["frame-ancestors-extra 'self'; script-src 'none'"],
        ),
    ],
)
async def test_http_preserves_policies_repeated_cookies_and_encoding(
    monkeypatch, csp_fields, expected_policies
):
    cookies = [
        "access=SYNTHETIC; Path=/; HttpOnly",
        "refresh=; Expires=Wed, 21 Oct 2015 07:28:00 GMT; Max-Age=0; Path=/; HttpOnly",
    ]
    payload = gzip.compress(b"synthetic compressed request", mtime=0)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=gzip.compress(b"synthetic response", mtime=0),
            headers=[("content-encoding", "gzip")]
            + [("content-security-policy", value) for value in csp_fields]
            + [("set-cookie", value) for value in cookies],
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kw: client_class(**kw, trust_env=False, transport=httpx.MockTransport(respond)),
    )

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = proxy.Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/openclaw-ui/api",
            "query_string": b"",
            "headers": [(b"content-encoding", b"gzip")],
        },
        receive,
    )
    result = await proxy.proxy_openclaw_ui("api", request)
    assert len(seen) == 1 and seen[0].content == payload
    assert seen[0].headers["content-encoding"] == "gzip"
    assert result.headers.getlist("set-cookie") == cookies
    assert result.headers.getlist("content-security-policy") == expected_policies
    assert result.body == b"synthetic response"
    assert "content-encoding" not in result.headers
    assert result.headers["content-length"] == str(len(result.body))


def test_proxy_scopes_assets_without_changing_inline_scripts_or_auth(monkeypatch):
    html = b"""<html data-openclaw-control-ui-base-path=""><head>
<script>const path = "/assets/unchanged.js";</script>
<script src="/assets/index.js"></script><link href="/favicon.svg">
<a href="//example.test">external</a></head></html>"""
    csp = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; script-src 'sha256-test'"
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/control-ui-config.json":
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(
            200,
            content=html,
            headers={
                "content-type": "text/html",
                "content-security-policy": csp,
                "x-frame-options": "DENY",
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kw: client_class(
            **kw,
            transport=httpx.MockTransport(respond),
        ),
    )
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as client:
        result = client.get(
            "/openclaw-ui/agents?test=1", headers={"Authorization": "Bearer browser-token"}
        )
        assert result.status_code == 200
        assert 'data-openclaw-control-ui-base-path="/openclaw-ui"' in result.text
        assert 'src="/openclaw-ui/assets/index.js"' in result.text
        assert 'href="/openclaw-ui/favicon.svg"' in result.text
        assert 'href="//example.test"' in result.text
        assert '<script>const path = "/assets/unchanged.js";</script>' in result.text
        assert "<base" not in result.text
        assert "x-frame-options" not in result.headers
        assert "frame-ancestors" not in result.headers["content-security-policy"]
        assert "base-uri 'none'" in result.headers["content-security-policy"]
        assert "script-src 'sha256-test'" in result.headers["content-security-policy"]
        assert requests[0].url.path == "/agents"
        assert requests[0].url.query == b"test=1"
        assert requests[0].headers["authorization"] == "Bearer browser-token"
        assert client.get("/openclaw-ui/control-ui-config.json").status_code == 401
        assert "authorization" not in requests[-1].headers


@pytest.mark.parametrize("path", ["/openclaw-ui", "/openclaw-ui/"])
def test_websocket_relays_text_binary_and_browser_auth(monkeypatch, path):
    calls = []

    class Upstream:
        close_code = None
        close_reason = None

        async def __aenter__(self):
            self.queue = asyncio.Queue()
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, message):
            await self.queue.put(message)

        async def close(self, code=1000, reason=""):
            self.close_code, self.close_reason = code, reason

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

    def connect(url, **kwargs):
        calls.append(SimpleNamespace(url=url, kwargs=kwargs))
        return Upstream()

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as client:
        with client.websocket_connect(
            path + "?test=1",
            headers={
                "origin": "http://mc.test:8000",
                "authorization": "Bearer browser-token",
            },
        ) as socket:
            socket.send_text('{"type":"req","method":"connect"}')
            assert socket.receive_text() == '{"type":"req","method":"connect"}'
            socket.send_bytes(b"binary")
            assert socket.receive_bytes() == b"binary"
    assert calls[0].url == proxy._GW_BASE.replace("http://", "ws://", 1) + "/?test=1"
    assert calls[0].kwargs["origin"] == "http://mc.test:8000"
    assert calls[0].kwargs["additional_headers"] == {"authorization": "Bearer browser-token"}


@pytest.mark.parametrize(
    "html, expected",
    [
        (b"<script>const s = '<script src=\"/a.js\">';</script>", None),
        (b"<script>const s = '<link href=\"/a.css\">';</script>", None),
        (b"<script>const s = 'data-openclaw-control-ui-base-path=\"\"';</script>", None),
        (b'<!-- <script src="/a.js"> -->', None),
        (b'<style>/* <link href="/a.css"> */</style>', None),
        (b'<textarea><script src="/a.js"></textarea>', None),
        (
            b'<script data-template=\'src="/fake.js"\' src="/a.js"></script>',
            b'<script data-template=\'src="/fake.js"\' src="/openclaw-ui/a.js"></script>',
        ),
        (b"<SCRIPT SRC = '/a.js'></SCRIPT>", b"<SCRIPT SRC = '/openclaw-ui/a.js'></SCRIPT>"),
        (b'<link title="a > b" href=/a.css>', b'<link title="a > b" href=/openclaw-ui/a.css>'),
        (
            b"<html data-openclaw-control-ui-base-path = ''>",
            b"<html data-openclaw-control-ui-base-path = '/openclaw-ui'>",
        ),
        (b'<script src="//example.test/a.js"></script>\xff', None),
    ],
)
def test_html_rewrite_preserves_raw_content_and_attribute_syntax(html, expected):
    result = proxy._rewrite_html(html)
    assert result == (html if expected is None else expected)
    assert proxy._rewrite_html(result) == result


@pytest.mark.parametrize(
    "separator", [b"\x0b", b"\x1c", b"\x1d", b"\x1e", b"\x1f", b"\x85", b"\xa0", b"\xc2\xa0"]
)
@pytest.mark.parametrize(
    "tag, name, value",
    [
        (b"script", b"src", b"/real.js"),
        (b"link", b"href", b"/real.css"),
        (b"html", b"data-openclaw-control-ui-base-path", b""),
    ],
)
def test_non_html_whitespace_stays_inside_unquoted_attribute_values(separator, tag, name, value):
    untouched = b"data-state=foo" + separator + name + b"=/not-an-attribute"
    html = b"<" + tag + b" " + untouched + b" " + name + b'="' + value + b'">'
    expected = html.replace(b'="' + value + b'"', b'="/openclaw-ui' + value + b'"')
    assert proxy._rewrite_html(html) == expected
    assert proxy._rewrite_html(expected) == expected


@pytest.mark.parametrize("space", [b" ", b"\t", b"\n", b"\f", b"\r"])
def test_actual_html_whitespace_delimits_attributes(space):
    html = b"<script" + space + b"src" + space + b"=" + space + b"/real.js></script>"
    assert proxy._rewrite_html(html) == html.replace(b"/real.js", b"/openclaw-ui/real.js")


@pytest.mark.parametrize("opening", [b"<noscript>", b"<noscript/>"])
@pytest.mark.parametrize(
    "raw", [b"textarea", b"title", b"style", b"xmp", b"iframe", b"noembed", b"noframes"]
)
def test_scripting_enabled_noscript_preserves_following_script(opening, raw):
    script = b'const end = "</' + raw + b'>"; const text = \'<link href="/inside.css">\';\r\n'
    html = (
        b'<html><body><link href="/before.css">' + opening + b"<" + raw + b"></noscript>"
        b"<script>" + script + b'</script><link href="/after.css"></body></html>'
    )
    expected = html.replace(b'="/before.css"', b'="/openclaw-ui/before.css"').replace(
        b'="/after.css"', b'="/openclaw-ui/after.css"'
    )
    assert proxy._rewrite_html(html) == expected
    assert proxy._rewrite_html(expected) == expected


@pytest.mark.parametrize(
    "html, old, new",
    [
        (
            b"""<script /data-x=" src='/fake.js'" src="/real.js"></script>""",
            b'src="/real.js"',
            b'src="/openclaw-ui/real.js"',
        ),
        (
            b"""<link /title=" href='/fake.css'" href="/real.css">""",
            b'href="/real.css"',
            b'href="/openclaw-ui/real.css"',
        ),
        (
            b"""<div /title=' data-openclaw-control-ui-base-path="fake"' """
            b'data-openclaw-control-ui-base-path=""></div>',
            b'data-openclaw-control-ui-base-path=""',
            b'data-openclaw-control-ui-base-path="/openclaw-ui"',
        ),
        (
            b"""<script a/b=" src='/fake.js'" src="/real.js"></script>""",
            b'src="/real.js"',
            b'src="/openclaw-ui/real.js"',
        ),
        (b'<script/src="/real.js">', b"/real.js", b"/openclaw-ui/real.js"),
        (b'<link/href="/real.css">', b"/real.css", b"/openclaw-ui/real.css"),
        (b'<script data-x="x"src="/real.js">', b"/real.js", b"/openclaw-ui/real.js"),
        (
            b'<html data-x="x"data-openclaw-control-ui-base-path="">',
            b'data-openclaw-control-ui-base-path=""',
            b'data-openclaw-control-ui-base-path="/openclaw-ui"',
        ),
    ],
)
def test_attribute_recovery_never_rescans_inside_quoted_values(html, old, new):
    expected = html.replace(old, new)
    assert proxy._rewrite_html(html) == expected
    assert proxy._rewrite_html(expected) == expected


@pytest.mark.parametrize("unsupported", [b"<div =broken>", b'<div ok="" =broken>'])
def test_unconsumed_attribute_syntax_preserves_entire_response(unsupported):
    html = b'<link href="/before.css">' + unsupported + b'<script src="/after.js"></script>'
    assert proxy._rewrite_html(html) == html


@pytest.mark.parametrize("unsupported", [b"svg", b"math", b"plaintext"])
def test_unsupported_tree_contexts_preserve_entire_response(unsupported):
    html = (
        b'<link href="/before.css"><' + unsupported + b">"
        b"<noscript><textarea></noscript><script>const text = '<link href=\"/inside.css\">';"
        b'</script><link href="/after.css">'
    )
    assert proxy._rewrite_html(html) == html


@pytest.mark.parametrize(
    "declaration",
    [
        b"<![CDATA[>",
        b'<!DOCTYPE html SYSTEM "synthetic> <textarea>">',
        b'<!DOCTYPE html PUBLIC "synthetic> <textarea>" "synthetic">',
        b"<!unknown>",
    ],
)
def test_unsupported_declarations_preserve_whole_response_and_script(declaration):
    script = b"const end = ']]></textarea>'; const text = '<link href=\"/inside.css\">';"
    html = (
        b'<link href="/before.css">'
        + declaration
        + b"<script>"
        + script
        + b'</script><link href="/after.css">'
    )
    assert proxy._rewrite_html(html) == html


@pytest.mark.parametrize("slash", [b">", b"/>"])
@pytest.mark.parametrize(
    "raw", [b"iframe", b"title", b"style", b"xmp", b"noembed", b"noframes", b"noscript"]
)
def test_select_tree_context_preserves_entire_response(raw, slash):
    script = b'const end = "</' + raw + b'>"; const text = \'<link href="/inside.css">\';'
    html = (
        b'<link href="/before.css"><select><'
        + raw
        + slash
        + b"</select><script>"
        + script
        + b'</script><link href="/after.css">'
    )
    assert proxy._rewrite_html(html) == html


@pytest.mark.parametrize("slash", [b">", b"/>"])
@pytest.mark.parametrize(
    "raw", [b"iframe", b"title", b"style", b"xmp", b"noembed", b"textarea", b"noscript"]
)
def test_frameset_recovery_preserves_entire_noframes_response(raw, slash):
    text = b'const end = "</' + raw + b'>"; const text = \'<link href="/inside.css">\';\r\n'
    html = (
        b'<link href="/before.css"><html><head></head><frameset><'
        + raw
        + slash
        + b"</frameset><noframes>"
        + text
        + b'</noframes></html><link href="/after.css">'
    )
    assert proxy._rewrite_html(html) == html


@pytest.mark.parametrize("declaration", [b"<!doctype html>", b"<!DOCTYPE HTML\t>"])
def test_simple_html_doctype_and_comments_still_allow_asset_rewrite(declaration):
    html = declaration + b'<!-- normal comment --><link href="/real.css">'
    assert proxy._rewrite_html(html) == html.replace(b"/real.css", b"/openclaw-ui/real.css")


@pytest.mark.parametrize("from_browser", [False, True])
@pytest.mark.parametrize(
    "code, expected", [(1005, 1000), (1006, 1011), (1015, 1011), (1008, 1008), (4001, 4001)]
)
async def test_websocket_preserves_legal_close_semantics(monkeypatch, from_browser, code, expected):
    reason = "synthetic close" if code in (1008, 4001) else ""
    closed = []
    closing = asyncio.Event()

    class Upstream:
        close_code = code
        close_reason = reason

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def close(self, *, code, reason):
            closed.append((code, reason))
            closing.set()
            await asyncio.sleep(0)  # Receiver finishes while close handshake is pending.

        def __aiter__(self):
            return self

        async def __anext__(self):
            if from_browser:
                await closing.wait()
            raise StopAsyncIteration

    async def receive():
        if from_browser:
            return {"type": "websocket.disconnect", "code": code, "reason": reason}
        await asyncio.Event().wait()

    browser = SimpleNamespace(
        url=SimpleNamespace(query=""),
        headers={},
        accept=AsyncMock(),
        receive=receive,
        close=AsyncMock(
            side_effect=AssertionError("browser already disconnected") if from_browser else None
        ),
    )
    monkeypatch.setattr(proxy.websockets, "connect", lambda *args, **kwargs: Upstream())
    await asyncio.wait_for(proxy.proxy_openclaw_websocket(browser), 1)
    if not from_browser:
        closed = [
            (browser.close.call_args.kwargs["code"], browser.close.call_args.kwargs["reason"])
        ]
    assert closed == [(expected, reason)]
    assert len(Close(*closed[0]).serialize()) <= 125


@pytest.mark.parametrize(
    "code, reason", [(1005, ""), (1006, ""), (1015, ""), (4001, "é" * 62), (1000, "\ud800")]
)
def test_close_status_is_wire_valid_and_bounded(code, reason):
    status = proxy._close_status(code, reason)
    assert status == ((1000 if code == 1005 else 1011), "")
    assert len(Close(*status).serialize()) <= 125


@pytest.mark.parametrize("opening", [b"<script>", b"<script/>"])
@pytest.mark.parametrize("escaped", [False, True])
def test_script_context_and_conservative_legacy_escape_fallback(opening, escaped):
    script = b"""const template = '<link href="/inside.css">';"""
    if escaped:
        script = b"""<!-- const x = "<script>"; const y = "</script>";""" + script + b"-->"
    html = b'<link href="/before.css">' + opening + script + b'</script><link href="/after.css">'
    expected = (
        html
        if escaped
        else html.replace(b'="/before.css"', b'="/openclaw-ui/before.css"').replace(
            b'="/after.css"', b'="/openclaw-ui/after.css"'
        )
    )
    assert proxy._rewrite_html(html) == expected


@pytest.mark.parametrize(
    "location",
    ["/same-origin", "ws://other.synthetic.invalid/path", "wss://other.synthetic.invalid/path"],
)
async def test_actual_websocket_driver_refuses_every_redirect(monkeypatch, location):
    attempts = []

    async def create_connection(instance):
        async def handshake(headers, user_agent):
            attempts.append((instance.uri, dict(headers)))
            raise InvalidStatus(WebSocketResponse(302, "Found", Headers({"Location": location})))

        return SimpleNamespace(handshake=handshake, transport=SimpleNamespace(abort=lambda: None))

    async def receive():
        await asyncio.Event().wait()

    monkeypatch.setattr(LibraryConnect, "create_connection", create_connection)
    browser = SimpleNamespace(
        url=SimpleNamespace(query=""),
        headers={"authorization": "Bearer SYNTHETIC", "cookie": "gateway=SYNTHETIC"},
        accept=AsyncMock(),
        receive=receive,
        close=AsyncMock(),
    )
    await asyncio.wait_for(proxy.proxy_openclaw_websocket(browser), 1)
    assert attempts == [
        (proxy._GW_BASE.replace("http://", "ws://", 1) + "/", dict(browser.headers))
    ]
    assert browser.close.call_args.kwargs == {
        "code": 1011,
        "reason": "OpenClaw gateway connection failed",
    }
