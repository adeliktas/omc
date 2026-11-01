"""Admin runtime changes name their actor (2026-10-06 watchdog drift had none).

The audit row is committed before the change; no change exists without it.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import model_ranking as ranking_api
from app.api import runtime_settings_api
from app.services import mc_sidecar, runtime_settings


class _Session:
    def __init__(self, user=None, fail=None):
        self.user, self.fail, self.pending, self.committed = user, fail, [], []

    async def get(self, _model, _id):
        if self.fail == "get":
            raise RuntimeError("db down")
        return self.user

    def add(self, row):
        self.pending.append(row)

    async def commit(self):
        if self.fail == "commit":
            raise RuntimeError("db down")
        self.committed += [row.message for row in self.pending]
        self.pending = []


def _admin():
    return SimpleNamespace(member=SimpleNamespace(user_id=uuid4()))


@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(runtime_settings, "_load_sidecar", lambda strict=False: dict(data))
    monkeypatch.setattr(
        runtime_settings, "_save_sidecar", lambda s: (data.clear(), data.update(s))
    )
    return data


def _patch(session, value, key="WATCHDOG_ENABLED"):
    return runtime_settings_api.patch_runtime_setting(
        key, {"value": value}, admin=_admin(), session=session
    )


@pytest.mark.asyncio
async def test_setting_change_records_actor_and_values(store):
    session = _Session(SimpleNamespace(preferred_name="Owner", name="x"))
    assert (await _patch(session, False))["value"] is False
    assert (await _patch(session, None))["value"] is True
    assert session.committed == [
        "Runtime setting WATCHDOG_ENABLED: True -> False (by Owner)",
        "Runtime setting WATCHDOG_ENABLED: False -> override cleared (by Owner)",
    ]
    assert store == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["get", "commit"])
async def test_setting_without_committed_audit_never_changes(store, fail):
    store["WATCHDOG_ENABLED"] = "on"  # raw prior override stays byte-for-byte
    with pytest.raises(RuntimeError):
        await _patch(_Session(fail=fail), False)
    assert store == {"WATCHDOG_ENABLED": "on"}


@pytest.mark.asyncio
async def test_failed_or_partial_setting_write_is_recorded_with_actual_state(
    store, monkeypatch
):
    def partial(data):  # writes, then fails (e.g. fsync)
        store.clear()
        store.update(data)
        raise OSError("fsync")

    monkeypatch.setattr(runtime_settings, "_save_sidecar", partial)
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await _patch(session, False)
    assert exc.value.status_code == 503
    who = session.committed[0].rsplit("(by ", 1)[1]
    assert session.committed == [
        f"Runtime setting WATCHDOG_ENABLED: True -> False (by {who}",
        f"FAILED (OSError), now False: Runtime setting WATCHDOG_ENABLED: True -> False (by {who}",
    ]


@pytest.mark.asyncio
async def test_invalid_setting_records_nothing(store):
    session = _Session()
    with pytest.raises(HTTPException):
        await runtime_settings_api.patch_runtime_setting(
            "NOT_A_SETTING", {"value": 1}, admin=_admin(), session=session
        )
    with pytest.raises(HTTPException):
        await _patch(session, "banana")
    assert session.committed == session.pending == [] and store == {}


def _disable():
    return ranking_api.RankingEntryMeta(
        provider="openai-codex-2", model="gpt-5.6-sol", disabled=True,
        disabled_reason="superseded by gpt-6.1-sol",
    )


@pytest.fixture
def ranking(monkeypatch, tmp_path):
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    mc_sidecar.set_model_meta("openai-codex-2", "gpt-5.6-sol", disabled=False, rank=2)


@pytest.mark.asyncio
async def test_ranking_change_records_actor_and_old_values(ranking):
    admin = _admin()
    session = _Session()  # unknown user still leaves an attributable row
    await ranking_api.put_entry(_disable(), admin=admin, session=session)
    assert mc_sidecar.get_model_meta("openai-codex-2", "gpt-5.6-sol")["disabled"] is True
    assert session.committed == [
        "Ranking entry openai-codex-2/gpt-5.6-sol: disabled False -> True, "
        "disabled_reason None -> 'superseded by gpt-6.1-sol' "
        f"(by user {admin.member.user_id})"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["get", "commit"])
async def test_ranking_without_committed_audit_never_changes(ranking, fail):
    with pytest.raises(RuntimeError):
        await ranking_api.put_entry(_disable(), admin=_admin(), session=_Session(fail=fail))
    assert mc_sidecar.get_model_meta("openai-codex-2", "gpt-5.6-sol") == {
        "disabled": False, "rank": 2,
    }


@pytest.mark.asyncio
async def test_silently_unsaved_ranking_change_is_reported(ranking, monkeypatch):
    monkeypatch.setattr(mc_sidecar, "save_sidecar", lambda _data: None)  # logs+swallows
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await ranking_api.put_entry(_disable(), admin=_admin(), session=session)
    assert exc.value.status_code == 503
    assert session.committed[1].startswith(
        "FAILED (OSError), now {'disabled': False, 'disabled_reason': None}: "
        "Ranking entry openai-codex-2/gpt-5.6-sol: disabled False -> True"
    )


@pytest.fixture
def runtime_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_settings.json"
    monkeypatch.setattr(runtime_settings, "_SIDECAR_PATH", path)
    return path


@pytest.mark.asyncio
async def test_unreadable_store_is_never_audited_with_invented_values(runtime_file, ranking):
    runtime_file.write_text('{"WATCHDOG_ENABLED": false, "WATCHDOG_INTERVAL": 999,')
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await _patch(session, True)
    assert exc.value.status_code == 503 and session.committed == session.pending == []
    # an unreadable file is never overwritten with only the new key
    assert runtime_file.read_text().endswith('"WATCHDOG_INTERVAL": 999,')

    mc_sidecar._SIDECAR_PATH.write_text("{not json")
    with pytest.raises(HTTPException) as exc:
        await ranking_api.put_entry(_disable(), admin=_admin(), session=session)
    assert exc.value.status_code == 503 and session.committed == []
    assert mc_sidecar._SIDECAR_PATH.read_text() == "{not json"


@pytest.mark.asyncio
async def test_failed_row_never_claims_a_fallback_state(runtime_file, monkeypatch):
    runtime_file.write_text('{"WATCHDOG_ENABLED": true}')

    def write_then_corrupt(data):
        runtime_file.write_text("{torn")  # the new state cannot be observed
        raise ValueError("writer failed")  # not an OSError: still a 503

    monkeypatch.setattr(runtime_settings, "_save_sidecar", write_then_corrupt)
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await _patch(session, False)
    assert exc.value.status_code == 503
    assert session.committed[1].startswith(
        "FAILED (ValueError), now unreadable (JSONDecodeError): "
        "Runtime setting WATCHDOG_ENABLED: True -> False"
    )


@pytest.mark.asyncio
async def test_symlink_loop_is_unreadable_not_missing(runtime_file, ranking):
    session = _Session()
    for path, change in (
        (runtime_file, lambda: _patch(session, True)),
        (mc_sidecar._SIDECAR_PATH,
         lambda: ranking_api.put_entry(_disable(), admin=_admin(), session=session)),
    ):
        path.unlink(missing_ok=True)
        path.symlink_to(path)  # exists() is False, but it is not absent
        with pytest.raises(HTTPException) as exc:
            await change()
        assert exc.value.status_code == 503 and path.is_symlink()
    assert session.committed == session.pending == []


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [
    '{"models": null}', '{"models": []}', '{"models": {"openai-codex-2": null}}',
    '{"models": {"openai-codex-2": {"gpt-5.6-sol": false}}}',
    '{"models": {"openai-codex-2": {"gpt-5.6-sol": []}}}',
])
async def test_corrupt_nested_ranking_store_fails_before_row(ranking, store):
    mc_sidecar._SIDECAR_PATH.write_text(store)
    session = _Session()
    with pytest.raises(HTTPException) as exc:
        await ranking_api.put_entry(_disable(), admin=_admin(), session=session)
    assert exc.value.status_code == 503 and session.committed == []
    assert mc_sidecar._SIDECAR_PATH.read_text() == store


@pytest.mark.asyncio
async def test_failed_row_never_reads_corrupt_nesting_as_unset(ranking, monkeypatch):
    def corrupting_write(*_args, **_kw):
        mc_sidecar._SIDECAR_PATH.write_text('{"models": []}')
        raise OSError("disk")

    monkeypatch.setattr(ranking_api.model_ranking, "set_entry_meta", corrupting_write)
    session = _Session()
    with pytest.raises(HTTPException):
        await ranking_api.put_entry(_disable(), admin=_admin(), session=session)
    assert session.committed[1].startswith("FAILED (OSError), now unreadable (SidecarCorruptError)")
