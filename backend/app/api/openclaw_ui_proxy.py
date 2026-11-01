"""T88b (2026-05-16) — Reverse proxy for the openclaw-gateway web UI so
it can be embedded in MC's `/openclaw` iframe.

Why this exists:
  The gateway sends `X-Frame-Options: DENY` and `Content-Security-Policy:
  frame-ancestors 'none'`, which makes any iframe embed fail. Owner
  explicitly does NOT want changes to openclaw itself, so we reverse-proxy
  the UI through MC and strip those headers in flight.

Mount path: `/openclaw-ui/{path:path}` on the bare FastAPI app (NOT
under `/api/v1/...`) so the in-page relative URLs resolve against this
prefix.

HTML assets and the Control UI base path are scoped to this proxy prefix.
HTTP and WebSocket requests retain the browser's Gateway authentication;
Mission Control credentials are never substituted for Gateway credentials.
"""

from __future__ import annotations

import asyncio
import os
import re
from html.parser import HTMLParser

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, ProtocolError, WebSocketException
from websockets.frames import Close
from websockets.typing import Origin

# Mounted at the BARE app root (not /api/v1). Path captures everything.
router = APIRouter(prefix="/openclaw-ui", tags=["openclaw-ui-proxy"])

_GW_HOST_RAW = os.environ.get("OPENCLAW_GATEWAY_HOST", "host.docker.internal")
_GW_PORT = os.environ.get("OPENCLAW_GATEWAY_PORT", "18789")
_GW_BASE = f"http://{_GW_HOST_RAW}" if ":" in _GW_HOST_RAW else f"http://{_GW_HOST_RAW}:{_GW_PORT}"

# Headers we will NEVER forward back to the browser because they're the
# very thing that blocks the iframe embed. CSP keeps the rest of its
# clauses, but the frame-ancestors directive is stripped out.
_BLOCKED_RESP_HEADERS = {
    "x-frame-options",
    "content-security-policy",  # rewritten below — drop the original
    "content-security-policy-report-only",
}

# Hop-by-hop headers we also drop on forward + return.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _strip_frame_ancestors(csp: str) -> str:
    """Remove `frame-ancestors` directive from a CSP header value. Returns
    the rewritten CSP (the rest of the policy stays intact).
    """
    policies = []
    for policy in csp.split(","):
        parts = [p.strip(" \t") for p in policy.split(";") if p.strip(" \t")]
        kept = [p for p in parts if not re.match(r"frame-ancestors(?:[ \t]|$)", p, re.I)]
        if kept:
            policies.append("; ".join(kept))
    return ", ".join(policies)


def _rewrite_html(html: bytes) -> bytes:
    """Change supported start-tag attributes, preserving every other original byte."""
    # ponytail: only simple HTML declarations; qualified HTML5 parsing for broader pages.
    if re.search(rb"<!(?!--|doctype[ \t\n\f\r]+html[ \t\n\f\r]*>)", html, re.I):
        return html
    text = html.decode("latin1")  # Reversible offsets, even for non-UTF8 error pages.
    lines = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    changes: list[tuple[int, int, str]] = []
    unsupported_html = False
    attributes = re.compile(
        r"([ \t\n\f\r/]*)([^ \t\n\f\r=/>]+)"
        r"""(?:([ \t\n\f\r]*=[ \t\n\f\r]*)("[^"]*"|'[^']*'|[^ \t\n\f\r>]+))?"""
    )
    ending = re.compile(r"[ \t\n\f\r]*/?>")

    class Rewrite(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            nonlocal unsupported_html
            if tag in ("svg", "math", "plaintext", "select", "frameset"):
                unsupported_html = True  # No HTML5 foreign/tree-context interpretation.
                return
            raw = self.get_starttag_text()
            assert raw is not None

            def attribute(match: re.Match[str]) -> str:
                prefix, name, equals, token = match.groups()
                if token is None:
                    return match[0]
                quote = token[0] if token[0] in "\"'" else ""
                value = token[1:-1] if quote else token
                if name.lower() == "data-openclaw-control-ui-base-path":
                    value = "/openclaw-ui"
                elif (tag, name.lower()) in (("script", "src"), ("link", "href")):
                    if (
                        value.startswith("/")
                        and not value.startswith(("//", "/openclaw-ui/"))
                        and value != "/openclaw-ui"
                    ):
                        value = "/openclaw-ui" + value
                return prefix + name + equals + quote + value + quote

            head = re.match(r"<[^ \t\n\f\r/>]+", raw)
            if head is None or head[0][1:].lower() != tag:
                unsupported_html = True
                return
            cursor = head.end()
            parts = [head[0]]
            while cursor < len(raw):
                if ending.fullmatch(raw, cursor):
                    parts.append(raw[cursor:])
                    break
                match = attributes.match(raw, cursor)
                if match is None:
                    unsupported_html = True
                    return  # Never search forward into a quoted value.
                parts.append(attribute(match))
                cursor = match.end()
            else:
                unsupported_html = True
                return
            rewritten = "".join(parts)
            if rewritten != raw:
                line, column = self.getpos()
                start = lines[line - 1] + column
                changes.append((start, start + len(raw), rewritten))
            # HTMLParser already treats script/style as raw text.
            if tag in ("textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript"):
                self.set_cdata_mode(tag)  # The Gateway UI requires scripting enabled.

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.handle_starttag(tag, attrs)
            if tag in ("script", "style", "noscript"):
                self.set_cdata_mode(tag)  # HTML ignores a non-void self-closing slash.

        def handle_data(self, data: str) -> None:
            nonlocal unsupported_html
            if self.cdata_elem == "script" and "<!--" in data:
                unsupported_html = True

    parser = Rewrite(convert_charrefs=False)
    parser.feed(text)
    parser.close()
    # ponytail: unsupported lexical syntax stays unchanged; HTML5 tokenizer for broader pages.
    if unsupported_html:
        return html
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in changes:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts).encode("latin1")


def _close_status(code: int, reason: str) -> tuple[int, str]:
    """ASGI/library sentinel statuses cannot appear on the WebSocket wire."""
    try:
        if len(Close(code, reason).serialize()) <= 125:
            return code, reason
    except (ProtocolError, UnicodeError):
        pass
    return (1000 if code == 1005 else 1011), ""


@router.websocket("")
@router.websocket("/{path:path}")
async def proxy_openclaw_websocket(websocket: WebSocket, path: str = "") -> None:
    """Relay the Gateway protocol; authentication remains owned by OpenClaw."""
    upstream_url = f"{_GW_BASE.replace('http://', 'ws://', 1)}/{path}"
    if websocket.url.query:
        upstream_url += f"?{websocket.url.query}"
    headers = {
        name: websocket.headers[name]
        for name in ("authorization", "cookie")
        if name in websocket.headers
    }
    origin = websocket.headers.get("origin")
    await websocket.accept()
    try:
        connection = websockets.connect(
            upstream_url,
            origin=Origin(origin) if origin is not None else None,
            additional_headers=headers,
            max_size=25 * 1024 * 1024,
            proxy=None,
        )
        # Fixed Gateway means no second handshake, even for a same-origin redirect.
        connection.process_redirect = lambda exc: exc  # type: ignore[method-assign]
        async with connection as upstream:
            browser_disconnected = False

            async def to_gateway() -> None:
                nonlocal browser_disconnected
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        browser_disconnected = True
                        code, reason = _close_status(
                            message.get("code", 1000), message.get("reason") or ""
                        )
                        await upstream.close(code=code, reason=reason)
                        return
                    await upstream.send(
                        message["bytes"] if message.get("bytes") is not None else message["text"]
                    )

            async def to_browser() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except ConnectionClosed:
                    pass
                if upstream.close_code is not None and not browser_disconnected:
                    code, reason = _close_status(upstream.close_code, upstream.close_reason or "")
                    await websocket.close(code=code, reason=reason)

            tasks = [asyncio.create_task(to_gateway()), asyncio.create_task(to_browser())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except (OSError, WebSocketException):
        await websocket.close(code=1011, reason="OpenClaw gateway connection failed")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    summary="T88b — Reverse proxy for the openclaw gateway web UI",
)
async def proxy_openclaw_ui(path: str, request: Request) -> Response:
    upstream_path = path or ""
    upstream_url = f"{_GW_BASE}/{upstream_path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Forward headers minus hop-by-hop + host (must let httpx set its own).
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    }
    # httpx decodes upstream responses and we strip their content-encoding,
    # so only offer what httpx can decode: a browser's `br` came back as
    # raw brotli bytes and the /openclaw iframe rendered garbage.
    fwd_headers = {k: v for k, v in fwd_headers.items() if k.lower() != "accept-encoding"}
    fwd_headers["accept-encoding"] = "gzip, deflate"
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            r = await client.request(
                method=request.method,
                url=upstream_url,
                headers=fwd_headers,
                content=body,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(502, f"openclaw gateway unreachable: {exc}")
    except httpx.TimeoutException:
        raise HTTPException(504, "openclaw gateway timeout")

    content = r.content
    ct = (r.headers.get("content-type") or "").lower()

    if "text/html" in ct:
        content = _rewrite_html(content)

    # Raw fields preserve repeated cookies and conjunctive CSP policy boundaries.
    response = Response(content=content, status_code=r.status_code)
    for name, value in r.headers.raw:
        lk = name.decode("ascii").lower()
        if lk == "content-security-policy":
            value = _strip_frame_ancestors(value.decode("latin1")).encode("latin1")
            if value:
                response.raw_headers.append((name.lower(), value))
        elif lk not in _HOP_BY_HOP | _BLOCKED_RESP_HEADERS | {"content-encoding"}:
            response.raw_headers.append((name.lower(), value))
    return response
