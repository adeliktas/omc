"""OmniRoute → MC webhook receiver (T103 v16, 2026-05-19).

Closes the bi-directional sync loop without polling. Replaces the watchdog's
60s `reverse_sync_to_mc_db` tick with an event-driven path: OmniRoute calls
this endpoint on every provider/connection/node mutation, MC reconciles its
DB on the spot.

Endpoints:
    POST /api/v1/internal/omniroute-webhook   — event receiver

Bootstrap:
    `ensure_omniroute_webhook_registered()` is called from the FastAPI
    lifespan startup hook. It idempotently registers (or refreshes) a
    single webhook in OmniRoute pointing at this receiver. Webhook URL +
    secret are persisted in a sidecar file so a restart doesn't churn the
    OR-side webhook record.

Trust model:
    OR and MC share a docker network — the receiver is not reachable from
    outside the host. We still verify the HMAC-SHA256 signature OR sends
    in the `X-Omniroute-Signature` header (or any header containing
    "signature") against the secret stored at registration time. Missing
    secret → log + accept (degraded mode; happens first request after
    upgrade until the bootstrap runs).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

_WEBHOOK_PATH = "/api/v1/internal/omniroute-webhook"

# Sidecar tracks the webhook id + secret OmniRoute issued so we can verify
# signatures + clean up duplicates on restart. Kept out of openclaw.json
# (gateway's strict schema would reject extra keys).
_SIDECAR = Path(
    os.environ.get(
        "OMNIROUTE_WEBHOOK_SIDECAR",
        "/home/host_openclaw/.omniroute_webhook.json",
    )
)


def _load_sidecar() -> dict[str, Any]:
    try:
        return json.loads(_SIDECAR.read_text()) if _SIDECAR.exists() else {}
    except Exception:
        return {}


def _save_sidecar(data: dict[str, Any]) -> None:
    try:
        _SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SIDECAR.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_SIDECAR)
    except Exception as exc:
        logger.warning("omniroute_webhook.sidecar_write_failed err=%s", exc)


def _verify_signature(raw_body: bytes, headers: dict[str, str]) -> bool:
    """Check HMAC-SHA256 in any 'signature' header against the stored secret.

    Returns True if no secret is known (degraded boot) so we never lose an
    event during initial rollout; logs the suspect headers either way.
    """
    sidecar = _load_sidecar()
    secret = sidecar.get("secret") or ""
    if not secret:
        return True  # degraded — accept until bootstrap finishes
    sig_header = None
    for k, v in headers.items():
        if "signature" in k.lower():
            sig_header = v
            break
    if not sig_header:
        logger.warning("omniroute_webhook.no_signature_header headers=%s",
                       sorted(headers.keys()))
        return False
    # Try both raw-hex and `sha256=<hex>` formats.
    expected = hmac.new(
        secret.encode(), raw_body, hashlib.sha256,
    ).hexdigest()
    candidates = {sig_header, sig_header.split("=", 1)[-1]}
    return any(hmac.compare_digest(expected.encode(), c.encode()) for c in candidates)


@router.post("/omniroute-webhook", summary="OmniRoute event receiver")
async def receive_omniroute_event(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    headers = dict(request.headers)

    if not _verify_signature(raw_body, headers):
        logger.warning(
            "omniroute_webhook.signature_mismatch body_len=%d", len(raw_body),
        )
        raise HTTPException(status_code=401, detail="signature mismatch")

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        payload = {"_raw": raw_body[:500].decode("utf-8", errors="replace")}

    event_name = (
        payload.get("event")
        or payload.get("type")
        or payload.get("eventType")
        or "unknown"
    )
    logger.info(
        "omniroute_webhook.received event=%s body=%s",
        event_name, json.dumps(payload)[:500],
    )

    # Trigger a one-shot reconcile against MC DB. Cheap: ~12 rows. Faster
    # than picking individual fields out of every event shape.
    try:
        from app.services.omniroute_sync import reverse_sync_to_mc_db
        rows = await reverse_sync_to_mc_db()
        logger.info(
            "omniroute_webhook.reconciled event=%s rows=%d", event_name, rows,
        )
    except Exception as exc:
        logger.warning(
            "omniroute_webhook.reconcile_failed event=%s err=%s",
            event_name, exc,
        )

    return {"ok": True, "event": event_name}


# ── Bootstrap: register the webhook with OmniRoute ──────────────────────


async def ensure_omniroute_webhook_registered() -> None:
    """Idempotent: ensures exactly one webhook in OmniRoute points at us.

    Reuses the saved id from the sidecar when possible. On mismatch (URL
    drift, manual deletion, secret rotation) re-creates + persists.
    Called from FastAPI lifespan after the watchdog task starts.
    """
    mgmt_url = os.environ.get("OMNIROUTE_URL", "http://omniroute:20128")
    # 2026-08-25: prefer a freshly self-minted mgmt JWT over the static (30d,
    # silently-expiring) OMNIROUTE_MGMT_TOKEN env — the expired env token was
    # causing register_failed status=401 AUTH_001.
    try:
        from app.services.traffic_etl import _mgmt_token as _mint
        mgmt_token = _mint() or os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
    except Exception:
        mgmt_token = os.environ.get("OMNIROUTE_MGMT_TOKEN", "")
    if not mgmt_token:
        logger.info("omniroute_webhook.skipped reason=no_mgmt_token")
        return
    # Build the URL we want OR to call. Defaults to the compose-DNS name.
    receiver = os.environ.get(
        "OMNIROUTE_WEBHOOK_RECEIVER",
        "http://backend:8000" + _WEBHOOK_PATH,
    )
    origin = mgmt_url.replace("//omniroute:", "//localhost:")
    headers = {
        "Cookie": f"auth_token={mgmt_token}",
        "Origin": origin,
        "Referer": f"{origin}/dashboard",
        "Content-Type": "application/json",
    }

    import httpx
    sidecar = _load_sidecar()

    async with httpx.AsyncClient(timeout=8.0) as c:
        # List existing webhooks to detect: ours / stale / duplicates.
        try:
            r = await c.get(f"{mgmt_url}/api/webhooks", headers=headers)
            existing = (r.json() or {}).get("webhooks", []) or []
        except Exception as exc:
            logger.warning("omniroute_webhook.list_failed err=%s", exc)
            return

        ours = [w for w in existing if w.get("url") == receiver]
        # Drop duplicates (keep the first).
        for dup in ours[1:]:
            try:
                await c.delete(
                    f"{mgmt_url}/api/webhooks/{dup['id']}", headers=headers,
                )
                logger.info(
                    "omniroute_webhook.deduped id=%s", dup["id"],
                )
            except Exception:
                pass

        if ours:
            wh = ours[0]
            # Persist id/secret in case sidecar was lost (no GET endpoint
            # for secret — but POST returned it; we may not have it on
            # disk after a fresh boot).
            if sidecar.get("id") != wh.get("id"):
                sidecar = {
                    "id": wh.get("id"),
                    "url": wh.get("url"),
                    # secret unknown — only POST response exposes it. If we
                    # don't have it cached, we'll DELETE + recreate so the
                    # new POST returns a fresh secret.
                    "secret": sidecar.get("secret") or "",
                }
            if not sidecar.get("secret"):
                logger.info(
                    "omniroute_webhook.recreating_for_secret id=%s",
                    wh.get("id"),
                )
                try:
                    await c.delete(
                        f"{mgmt_url}/api/webhooks/{wh['id']}", headers=headers,
                    )
                except Exception:
                    pass
                ours = []  # fall through to create

        if not ours:
            try:
                r = await c.post(
                    f"{mgmt_url}/api/webhooks",
                    headers=headers,
                    json={"url": receiver, "events": ["*"]},
                )
                if r.status_code in (200, 201):
                    wh = (r.json() or {}).get("webhook") or {}
                    sidecar = {
                        "id": wh.get("id"),
                        "url": wh.get("url"),
                        "secret": wh.get("secret"),
                    }
                    _save_sidecar(sidecar)
                    logger.info(
                        "omniroute_webhook.registered id=%s url=%s",
                        wh.get("id"), wh.get("url"),
                    )
                    return
                logger.warning(
                    "omniroute_webhook.register_failed status=%d body=%s",
                    r.status_code, r.text[:300],
                )
            except Exception as exc:
                logger.warning("omniroute_webhook.register_err err=%s", exc)
                return

        if sidecar:
            _save_sidecar(sidecar)
        logger.info(
            "omniroute_webhook.ready id=%s url=%s",
            sidecar.get("id"), sidecar.get("url"),
        )
