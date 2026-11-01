"""Pin the model re-enable contract on ModelEntryUpdate.

The owners page re-enables a benchmark-auto-disabled model by PATCHing
`{disabled: false, disabled_reason: null, disabled_at: null}`. The schema
must accept all three so the endpoint can route them to the MC sidecar
(`set_model_meta` clears None-valued keys, sets the rest). This was the gap:
the fields existed in the sidecar and the endpoint's `_MC_KEYS` filter, but
`ModelEntryUpdate` silently dropped them, so the UI had no way to clear the
flag short of editing openclaw.mc.json.
"""

from __future__ import annotations

from app.api.provider_capacity import ModelEntryUpdate


def test_model_update_accepts_disable_bookkeeping_fields() -> None:
    payload = ModelEntryUpdate(
        disabled=False,
        disabled_reason=None,
        disabled_at=None,
    )
    dumped = payload.model_dump(exclude_unset=True)
    assert dumped["disabled"] is False
    # None values must survive exclude_unset so set_model_meta clears them.
    assert "disabled_reason" in dumped and dumped["disabled_reason"] is None
    assert "disabled_at" in dumped and dumped["disabled_at"] is None


def test_model_update_omits_unset_disable_fields() -> None:
    # A plain name edit must not touch the disable bookkeeping.
    payload = ModelEntryUpdate(name="renamed")
    dumped = payload.model_dump(exclude_unset=True)
    assert "disabled" not in dumped
    assert "disabled_reason" not in dumped
    assert "disabled_at" not in dumped
