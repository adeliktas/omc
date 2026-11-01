"""Task creation remains usable without the optional model and split stages."""

import json
from unittest.mock import AsyncMock

import pytest

from app.models.tasks import Task
from app.services import complexity_assessor as assessor
from app.services import runtime_settings


@pytest.mark.asyncio
async def test_difficulty_bridge_and_live_switch(monkeypatch):
    llm = AsyncMock(return_value=(4, "Cross-subsystem", "model", "local"))
    monkeypatch.setattr(assessor, "_ask_llm", llm)
    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: {})

    task = Task(title="Rename button", description="Change label")
    await assessor.populate_task_complexity(None, task)
    rationale = json.loads(task.complexity_rationale)
    assert task.complexity == rationale["keyword_floor"]
    assert rationale["llm_level"] is None
    assert "disabled" in rationale["notes"][0]
    llm.assert_not_awaited()

    monkeypatch.setattr(
        runtime_settings,
        "_load_sidecar",
        lambda: {
            "TASK_COMPLEXITY_LLM_ENABLED": True,
        },
    )
    task = Task(title="Rename button", description="Change label")
    await assessor.populate_task_complexity(None, task)
    assert task.complexity == 4
    assert json.loads(task.complexity_rationale)["llm_model"] == "model"
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_test_task_stays_trivial_without_an_llm_or_history(monkeypatch):
    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: {})
    task = Task(title="Test2026Oct01043900", description="Reply OK.")
    await assessor.populate_task_complexity(None, task)
    assert task.complexity == 1
    rationale = json.loads(task.complexity_rationale)
    assert rationale["keyword_score"] == 0
    assert rationale["llm_level"] is None
    assert not any("Historical:" in note for note in rationale["notes"])


@pytest.mark.asyncio
async def test_explicit_level_and_error_bridge(monkeypatch):
    call = AsyncMock(side_effect=RuntimeError("assessor down"))
    monkeypatch.setattr(assessor, "assess_complexity", call)
    task = Task(title="Explicit", complexity=5)
    await assessor.populate_task_complexity(None, task)
    assert task.complexity == 5
    call.assert_not_awaited()

    task = Task(title="Fix kernel module")
    await assessor.populate_task_complexity(None, task)
    rationale = json.loads(task.complexity_rationale)
    assert task.complexity == rationale["keyword_floor"]
    assert rationale["llm_level"] is None
    assert "failed" in rationale["notes"][0].lower()


@pytest.mark.asyncio
async def test_persisted_keyword_verdict_is_not_labeled_llm():
    from app.api.router import complexity_rationale

    class Db:
        async def execute(self, *_args, **_kwargs):
            return type(
                "Row",
                (),
                {
                    "first": lambda _self: (
                        "Rename button",
                        "Change label",
                        1,
                        json.dumps({"level": 1, "keyword_floor": 1, "llm_level": None}),
                    )
                },
            )()

    body = await complexity_rationale("sample", Db())
    assert body["method"] == "keyword_fallback"
    assert body["llm_verdict"]["level"] is None


def test_router_and_assessor_share_keyword_rules():
    from app.api.router import _complexity_score as route_score
    from app.services.task_complexity_rules import _complexity_score

    assert route_score is _complexity_score
    hard = _complexity_score("IDA Pro binary cracking crackme", "rename functions")
    assert hard["score"] >= 6 and hard["is_complex"]
    assert _complexity_score("rename label", "typo")["score"] < 0


def test_split_gate_never_fans_out_harness_or_child_tasks(monkeypatch):
    from uuid import uuid4

    from app.services.task_splitter import should_auto_split

    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: {})
    assert not should_auto_split(Task(title="Large", complexity=5, execution_lane="agent"))
    monkeypatch.setattr(
        runtime_settings,
        "_load_sidecar",
        lambda: {
            "TASK_AUTO_SPLIT_ENABLED": True,
        },
    )
    assert should_auto_split(Task(title="Large", complexity=5, execution_lane="agent"))
    assert not should_auto_split(Task(title="Small", complexity=1, execution_lane="agent"))
    assert not should_auto_split(Task(title="[codex] Legacy", complexity=5))
    assert not should_auto_split(Task(title="Large", complexity=5, execution_lane="harness"))
    assert not should_auto_split(Task(title="Large", complexity=5, parent_task_id=uuid4()))


@pytest.mark.asyncio
async def test_only_org_admins_can_change_global_switches():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.deps import require_org_admin
    from app.api.runtime_settings_api import router

    route = next(r for r in router.routes if r.path.endswith("/runtime/{key}"))
    assert require_org_admin in [dep.call for dep in route.dependant.dependencies]
    with pytest.raises(HTTPException) as exc:
        await require_org_admin(SimpleNamespace(member=SimpleNamespace(role="viewer")))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_settings_api_fails_closed_on_bad_value_or_write(monkeypatch):
    from fastapi import HTTPException

    from types import SimpleNamespace

    from app.api.runtime_settings_api import patch_runtime_setting

    class _Session:  # audit rows only; the sidecar outcome is what is asserted
        async def get(self, *_):
            return None

        def add(self, _row):
            pass

        async def commit(self):
            pass

    def patch(key, body):
        return patch_runtime_setting(
            key, body, admin=SimpleNamespace(member=SimpleNamespace(user_id=None)),
            session=_Session(),
        )

    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: {})
    with pytest.raises(HTTPException) as exc:
        await patch("TASK_COMPLEXITY_LLM_ENABLED", {})
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await patch("TASK_COMPLEXITY_LLM_ENABLED", {"value": "banana"})
    assert exc.value.status_code == 422

    def fail(_data):
        raise OSError("read-only mount")

    monkeypatch.setattr(runtime_settings, "_save_sidecar", fail)
    with pytest.raises(HTTPException) as exc:
        await patch("TASK_COMPLEXITY_LLM_ENABLED", {"value": True})
    assert exc.value.status_code == 503


def test_invalid_sidecar_value_does_not_misreport_disabled(monkeypatch):
    monkeypatch.setattr(
        runtime_settings, "_load_sidecar", lambda strict=False: {"TASK_COMPLEXITY_LLM_ENABLED": "banana"}
    )
    monkeypatch.delenv("TASK_COMPLEXITY_LLM_ENABLED", raising=False)
    assert runtime_settings.get_setting("TASK_COMPLEXITY_LLM_ENABLED") is False
    row = next(
        s for s in runtime_settings.list_settings() if s["key"] == "TASK_COMPLEXITY_LLM_ENABLED"
    )
    assert row["value"] is False and row["source"] == "default"


def test_optional_stages_default_off_and_live():
    by_key = {setting["key"]: setting for setting in runtime_settings.list_settings()}
    assert by_key["TASK_COMPLEXITY_LLM_ENABLED"]["default"] is False
    assert by_key["TASK_AUTO_SPLIT_ENABLED"]["default"] is False
    assert by_key["TASK_COMPLEXITY_LLM_ENABLED"]["applies"] == "live"
    assert by_key["TASK_AUTO_SPLIT_ENABLED"]["applies"] == "live"
