from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import model_router, runtime_settings


@pytest.mark.asyncio
async def test_virtual_combo_does_not_depend_on_jetson_and_respects_cooldown(monkeypatch):
    monkeypatch.setattr(model_router, "_online_provider_names", AsyncMock(return_value=set()))
    monkeypatch.setattr(model_router.omniroute_sync, "list_curated_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(runtime_settings, "get_setting", lambda key, default: "omniroute-auto")
    monkeypatch.setattr(model_router, "_is_model_rate_limited", lambda model: False)
    assert model_router._model_id_to_mc_provider("omniroute-auto") == "omniroute"
    result = await model_router.pick_model_for_tier(None, "executor")
    assert result[:2] == ("omniroute", "omniroute-auto")
    monkeypatch.setattr(model_router, "_is_model_rate_limited", lambda model: True)
    assert await model_router.pick_model_for_tier(None, "executor") is None

    # A watchdog tick must not restore the LAN-only combo or capacity overrides.
    source = Path(model_router.__file__).with_name("watchdog_loop.py").read_text()
    assert "await sync_combo_active_only()" not in source
    assert '_cfg["concurrencyPerModel"] = _cpm' not in source
