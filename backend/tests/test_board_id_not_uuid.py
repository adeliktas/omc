# ruff: noqa: INP001, S101
"""A non-UUID board id must not escape as HTTP 500.

Regression: board-scoped deps typed ``board_id: str`` passed the raw value to
``Board.objects.by_id`` which let Postgres raise ``DataError`` (invalid input
syntax for type uuid) -> unhandled 500. Typing ``board_id: UUID`` makes FastAPI
reject a malformed id with 422 before the board is ever loaded.
"""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_non_uuid_board_id_is_422_not_500() -> None:
    from app.api.deps import ActorContext, require_user_or_agent
    from app.main import app

    # Authenticate so the request reaches board-id validation (auth runs first).
    app.dependency_overrides[require_user_or_agent] = lambda: ActorContext(
        actor_type="user", user=None
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/boards/re-laboratory/tasks")
        assert resp.status_code == 422, resp.status_code
    finally:
        app.dependency_overrides.pop(require_user_or_agent, None)
