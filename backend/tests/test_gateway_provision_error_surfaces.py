"""A failed main-agent provision surfaces its own HTTPException, not a lazy-load error from logging."""

import asyncio

import pytest
from fastapi import HTTPException

from app.services.openclaw import admin_service


class Expiring:
    """Like an ORM row after a rollback: attribute access fails once the session is unusable."""

    def __init__(self):
        self._id, self.expired = "x", False

    @property
    def id(self):
        if self.expired:
            raise RuntimeError("expired attribute refreshed outside the async context")
        return self._id


def test_provision_failure_keeps_its_http_error(monkeypatch):
    gateway, agent = Expiring(), Expiring()

    async def fail(self, **kwargs):
        gateway.expired = agent.expired = True  # the lifecycle rolled the session back
        raise HTTPException(status_code=502, detail="gateway unreachable")

    monkeypatch.setattr(admin_service.AgentLifecycleOrchestrator, "run_lifecycle", fail)
    service = admin_service.GatewayAdminLifecycleService.__new__(admin_service.GatewayAdminLifecycleService)
    service.session = None
    service.logger = __import__("logging").getLogger("test")
    with pytest.raises(HTTPException) as err:
        asyncio.run(service.provision_main_agent_record(gateway, agent, user=None, action="provision", notify=False))
    assert err.value.detail == "gateway unreachable"
