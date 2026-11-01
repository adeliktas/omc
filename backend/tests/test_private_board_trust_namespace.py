"""Regression for failed source review: synthetic namespace, no real input/I/O."""

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import private_access as gate
from app.services import mc_sidecar

ARGS = (gate.LOCAL_PROVIDER, gate.LOCAL_MODEL, gate.LOCAL_ENDPOINT)


@pytest.fixture
def namespace(tmp_path, monkeypatch):
    home = tmp_path / "configured-home"
    home.mkdir(mode=0o700)
    path = home / "openclaw.mc.json"
    data = {"models": {ARGS[0]: {ARGS[1]: {"fully_trusted": True}}}}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv("OPENCLAW_HOME", str(home))
    return home, path, data


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o702])
def test_writable_parent_refused_before_review_input(namespace, monkeypatch, mode):
    home, _, _ = namespace
    home.chmod(mode)
    source = Path(__file__).parents[2] / "scripts/private-local-review.py"
    spec = importlib.util.spec_from_file_location("namespace_reader_test", source)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    read = Mock(side_effect=AssertionError("protected read must not occur"))
    monkeypatch.setattr(reader, "read_document", read)
    assert reader.review(Path("/never-read")) == {
        "status": "refused",
        "reason": "local_review_failed_closed",
    }
    read.assert_not_called()


@pytest.mark.parametrize("before", ["parent", "file"])
def test_parent_swap_does_not_admit_other_trusted_snapshot(
    namespace, tmp_path, monkeypatch, before
):
    home, path, data = namespace
    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    approved = alternate / path.name
    approved.write_text(json.dumps(data))
    approved.chmod(0o600)
    data["models"][ARGS[0]][ARGS[1]]["fully_trusted"] = False
    path.write_text(json.dumps(data))
    real_open = gate.os.open
    switched = []

    def swap(name, flags, *args, **kwargs):
        target = home.name if before == "parent" else path.name
        # Includes the old absolute-path call and the corrected FD-relative call.
        if Path(name).name == target and not switched:
            home.rename(tmp_path / "original-revoked")
            home.symlink_to(alternate, target_is_directory=True)
            switched.append(True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(gate.os, "open", swap)
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)
    assert switched, "probe must exercise actual namespace change"


@pytest.mark.parametrize(
    "raw",
    [
        '{"models":{"local-qwen":{"Qwen3.8-27B-FP8":{"fully_trusted":false,"fully_trusted":true}}}}',
        '{"models":{},"models":{"local-qwen":{"Qwen3.8-27B-FP8":{"fully_trusted":true}}}}',
    ],
)
def test_duplicate_members_fail_closed(namespace, raw):
    _, path, _ = namespace
    path.write_text(raw)
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)


def test_api_reader_default_path_is_same_and_not_home(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    observed = []
    real_open = gate.os.open

    def record(name, flags, *args, **kwargs):
        observed.append(str(name))
        if str(name) in ("home", "/home/host_openclaw/openclaw.mc.json"):
            raise PermissionError("synthetic stop before any host configuration")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(gate.os, "open", record)
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)
    assert gate.DEFAULT_OPENCLAW_HOME == "/home/host_openclaw"
    assert "DEFAULT_OPENCLAW_HOME" in Path(mc_sidecar.__file__).read_text()
    assert "home" in observed or "/home/host_openclaw/openclaw.mc.json" in observed
    assert str(tmp_path) not in " ".join(observed)


def test_home_with_final_filename_as_ancestor_is_fully_walked(namespace, monkeypatch):
    home, path, data = namespace
    nested = home / path.name / "nested"
    path.unlink()
    nested.mkdir(parents=True)
    actual = nested / path.name
    actual.write_text(json.dumps(data))
    actual.chmod(0o600)
    monkeypatch.setenv("OPENCLAW_HOME", str(nested))
    gate.require_jira_access(*ARGS)
    actual.write_text('{"models":{}}')
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)


def test_foreign_or_deep_invalid_configuration_denies(namespace, monkeypatch):
    _, path, _ = namespace
    original_stat = gate.os.fstat

    def foreign(fd):
        value = original_stat(fd)
        if gate.stat.S_ISDIR(value.st_mode):
            from types import SimpleNamespace

            return SimpleNamespace(st_uid=1234567, st_mode=value.st_mode)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(gate.os, "fstat", foreign)
        with pytest.raises(PermissionError):
            gate.require_jira_access(*ARGS)
    path.write_text("[" * 1500 + "]" * 1500)
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)


@pytest.mark.parametrize("operation", ["entry", "usage", "seed", "clear", "rename"])
def test_unrelated_writers_cannot_normalize_ambiguous_trust(
    namespace, monkeypatch, operation
):
    from app.services import model_ranking

    _, path, _ = namespace
    raw = '{"models":{"local-qwen":{"Qwen3.8-27B-FP8":{"fully_trusted":false,"fully_trusted":true,"level":3}}}}'
    path.write_text(raw)
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", path)
    operations = {
        "entry": lambda: model_ranking.set_entry_meta(ARGS[0], ARGS[1], {"rank": 2}),
        "usage": lambda: model_ranking.record_usage(
            "other", "model", {"total_tokens": 1}
        ),
        "seed": model_ranking.seed_default_ranking,
        "clear": lambda: mc_sidecar.clear_model_meta("other", "model"),
        "rename": lambda: mc_sidecar.rename_provider("other", "renamed"),
    }
    with pytest.raises(mc_sidecar.SidecarCorruptError):
        operations[operation]()
    assert path.read_text() == raw
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)


@pytest.mark.parametrize(
    "endpoint,method,payload",
    [
        ("/entry", "PUT", {"provider": "other", "model": "model", "rank": 2}),
        ("/usage", "POST", {"provider": "other", "model": "model", "total_tokens": 1}),
        ("/seed", "POST", None),
    ],
)
async def test_actual_admin_handlers_do_not_promote_ambiguous_trust(
    namespace, monkeypatch, endpoint, method, payload
):
    from types import SimpleNamespace
    from uuid import uuid4
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import deps, model_ranking as api

    _, path, _ = namespace
    raw = '{"models":{"local-qwen":{"Qwen3.8-27B-FP8":{"fully_trusted":false,"fully_trusted":true,"level":3}}}}'
    path.write_text(raw)
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", path)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[deps.require_org_member] = lambda: SimpleNamespace(
        member=SimpleNamespace(role="admin", user_id=uuid4())
    )

    class Audit:
        def __init__(self):
            self.rows = []

        async def get(self, *args):
            return None

        def add(self, row):
            self.rows.append(row)

        async def commit(self):
            pass

    audit = Audit()
    app.dependency_overrides[api._db_dep] = lambda: audit
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic",
    ) as client:
        response = await client.request(method, "/ranking" + endpoint, json=payload)
    assert response.status_code >= 400
    assert not audit.rows, (
        "corrupt metadata must refuse before attributable mutation audit"
    )
    assert path.read_text() == raw
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)


def test_atomic_sidecar_replacement_and_revocation_still_work(namespace):
    _, path, data = namespace
    gate.require_jira_access(*ARGS)
    data["models"][ARGS[0]][ARGS[1]]["fully_trusted"] = False
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    os.replace(temporary, path)
    with pytest.raises(PermissionError):
        gate.require_jira_access(*ARGS)
