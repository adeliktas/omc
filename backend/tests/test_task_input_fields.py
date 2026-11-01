"""Task write routes reject misspelled budget fields rather than ignoring them."""

import pytest

from app.api import tasks as api
from tests.test_autonomous_review import AUTH, _client, _seed_review_task
from tests.test_autonomous_review import env, sidecar  # noqa: F401


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("field", ["budget_seconds", "budget_tokens"])
async def test_task_routes_reject_unknown_budget_fields(env, method, field):
    env["app"].include_router(api.router, prefix="/api/v1")
    url = f"/api/v1/boards/{env['board_id']}/tasks"
    payload: dict[str, object] = {field: 1200}
    if method == "PATCH":
        task = await _seed_review_task(env, with_cycle=False)
        url += f"/{task.id}"
    else:
        payload["title"] = "Task with an unknown budget field"

    async with _client(env) as client:
        response = await client.request(method, url, headers=AUTH, json=payload)

    assert response.status_code == 422, response.text
    assert any(
        error["loc"] == ["body", field] and error["type"] == "extra_forbidden"
        for error in response.json()["detail"]
    ), response.json()
