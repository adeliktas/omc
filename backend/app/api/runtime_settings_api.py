"""API surface for runtime-editable settings (T103 v35)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import SESSION_DEP, require_org_admin, require_user_or_agent
from app.services.activity_log import AdminChangeFailed, audited_admin_change
from app.services.organizations import OrganizationContext
from app.services.runtime_settings import (
    SETTINGS,
    coerce_setting,
    list_settings,
    update_setting,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/runtime", summary="List all runtime-editable settings with current values")
async def get_runtime_settings(
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Return registered knobs grouped by section."""
    settings = list_settings()
    sections: dict[str, list[dict[str, Any]]] = {}
    for s in settings:
        sections.setdefault(s["section"], []).append(s)
    return {"settings": settings, "sections": sections}


@router.patch("/runtime/{key}", summary="Update a runtime setting")
async def patch_runtime_setting(
    key: str,
    payload: dict[str, Any],
    admin: OrganizationContext = Depends(require_org_admin),
    session: AsyncSession = SESSION_DEP,
) -> dict[str, Any]:
    """Body: `{"value": <new value>}` or `{"value": null}` to revert."""
    if key not in {setting["key"] for setting in SETTINGS}:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if "value" not in payload:
        raise HTTPException(status_code=422, detail="Missing value")
    value = payload["value"]
    new = None if value is None else coerce_setting(key, value)
    if value is not None and new is None:
        raise HTTPException(status_code=422, detail="Invalid setting value")

    def effective() -> Any:
        return next(s for s in list_settings(strict=True) if s["key"] == key)["value"]

    try:
        old = effective()
    except Exception:
        raise HTTPException(
            status_code=503, detail="Runtime setting could not be read; nothing changed"
        )
    try:
        result = await audited_admin_change(
            session,
            admin.member,
            f"Runtime setting {key}: {old!r} -> "
            + ("override cleared" if value is None else repr(new)),
            apply=lambda: update_setting(key, value),
            state=lambda: repr(effective()),
        )
    except AdminChangeFailed:
        raise HTTPException(status_code=503, detail="Runtime setting could not be saved")
    if result is None:
        raise HTTPException(status_code=422, detail="Invalid setting value")
    return result
