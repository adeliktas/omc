"""Explicit canonical Pi dispatch: account pin, no OAuth copy, narrow in-place sandbox."""

import hashlib
import importlib.util
import json
import sys
import subprocess
import time
from pathlib import Path

import pytest
from tests import test_team_backlog

env = test_team_backlog.env
ROOT = Path(__file__).resolve().parents[2]


def test_normal_admin_selection_persists_through_schema_and_setter(
    tmp_path, monkeypatch
):
    from app.api.model_ranking import RankingEntryMeta
    from app.services import mc_sidecar, model_ranking

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "sidecar.json")
    for selected in (True, False):
        payload = RankingEntryMeta(
            provider="openai-codex-canonical",
            model="gpt-6.1-sol",
            level=1,
            write_enabled=False,
            canonical_pi_selected=selected,
        )
        values = payload.model_dump()
        meta = {
            key: values[key]
            for key in payload.model_fields_set
            if key not in ("provider", "model")
        }
        model_ranking.set_entry_meta(payload.provider, payload.model, meta)
        saved = mc_sidecar.get_model_meta(payload.provider, payload.model)
        assert saved.get("canonical_pi_selected") is selected
        assert model_ranking._availability(saved, payload.provider) == (
            (True, None) if selected else (False, "canonical_pi_unselected")
        )


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0, [], {}])
def test_normal_admin_canonical_selection_refuses_non_boolean_values(invalid):
    from pydantic import ValidationError
    from app.api.model_ranking import RankingEntryMeta

    with pytest.raises(ValidationError):
        RankingEntryMeta(
            provider="openai-codex-canonical",
            model="gpt-6.1-sol",
            canonical_pi_selected=invalid,
        )


async def test_real_ranking_endpoint_selection_audit_and_hold_preservation(
    env, tmp_path, monkeypatch
):
    from uuid import uuid4
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlmodel import select
    from app.api import model_ranking as api
    from app.models.activity_events import ActivityEvent
    from app.models.organization_members import OrganizationMember
    from app.models.organizations import Organization
    from app.models.users import User
    from app.services import mc_sidecar, model_ranking
    from app.services.organizations import OrganizationContext

    s, board, _task = env
    user = User(
        clerk_user_id=str(uuid4()),
        email="selection@invalid",
        active_organization_id=board.organization_id,
    )
    s.add(user)
    await s.flush()
    member = OrganizationMember(
        organization_id=board.organization_id,
        user_id=user.id,
        role="owner",
        all_boards_write=True,
    )
    s.add(member)
    await s.commit()
    organization = await s.get(Organization, board.organization_id)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    # Test-resolved owner identity; real handler, writer and audit commit. Not live auth proof.
    app.dependency_overrides[api.require_org_admin] = lambda: OrganizationContext(
        organization, member
    )

    async def session():
        yield s

    app.dependency_overrides[api._db_dep] = session
    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "sidecar.json")
    provider, model = "openai-codex-canonical", "gpt-6.1-sol"
    mc_sidecar.set_model_meta(provider, model, write_enabled=False, max_complexity=2)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for selected in (True, False):
            response = await client.put(
                "/api/v1/ranking/entry",
                json={
                    "provider": provider,
                    "model": model,
                    "canonical_pi_selected": selected,
                },
            )
            assert response.status_code == 200 and response.json() == {"ok": True}
            meta = mc_sidecar.get_model_meta(provider, model)
            assert meta == {
                "write_enabled": False,
                "max_complexity": 2,
                "canonical_pi_selected": selected,
            }
            assert model_ranking._availability(meta, provider)[0] is selected
        hold = {
            "unavailable_until": "9999-01-01T00:00:00+00:00",
            "availability_reason": "quota_exhausted",
        }
        mc_sidecar.set_model_meta(provider, "gpt-6-luna", **hold)
        response = await client.put(
            "/api/v1/ranking/entry",
            json={"provider": provider, "model": model, "canonical_pi_selected": True},
        )
        assert response.status_code == 200
        assert mc_sidecar.get_model_meta(provider, "gpt-6-luna") == hold
        assert model_ranking._availability(
            mc_sidecar.get_model_meta(provider, model), provider
        ) == (False, "quota_exhausted")
        response = await client.put(
            "/api/v1/ranking/entry",
            json={
                "provider": provider,
                "model": model,
                "canonical_pi_selected": "true",
            },
        )
        assert response.status_code == 422
    audits = list(
        await s.exec(
            select(ActivityEvent).where(ActivityEvent.event_type == "admin.change")
        )
    )
    assert len(audits) == 3 and all(
        "canonical_pi_selected" in row.message for row in audits
    )


def test_canonical_selection_is_opt_in_and_cannot_hide_account_holds(monkeypatch):
    from app.services import model_ranking, mc_sidecar

    monkeypatch.setattr(mc_sidecar, "get_provider_meta", lambda _provider: {})
    provider = "openai-codex-canonical"
    assert model_ranking._availability({}, provider) == (
        False,
        "canonical_pi_unselected",
    )
    assert model_ranking._availability({"canonical_pi_selected": "true"}, provider) == (
        False,
        "canonical_pi_unselected",
    )
    assert model_ranking._availability({"canonical_pi_selected": True}, provider) == (
        True,
        None,
    )
    sibling = {
        "unavailable_until": "9999-01-01T00:00:00+00:00",
        "availability_reason": "quota_exhausted",
    }
    monkeypatch.setattr(
        mc_sidecar, "get_provider_meta", lambda _provider: {"other-model": sibling}
    )
    for meta in ({}, {"canonical_pi_selected": True}):
        assert model_ranking._availability(meta, provider) == (False, "quota_exhausted")


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "canonical_pi_runner", ROOT / "scripts/mc-team-runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canonical_dispatch_keeps_legacy_mapping_and_engine_identity(
    runner, monkeypatch, tmp_path
):
    from app.services import model_ranking, owner_reviews, team_cycle

    monkeypatch.setattr(runner.os, "access", lambda *_: True)
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        argv, env = runner.harness_cmd(
            "openai-codex-canonical", model, "low", "- brief", str(tmp_path), "attempt"
        )
        assert env == {"PI_CODING_AGENT_DIR": "/root/.pi/agent"}
        assert argv[argv.index("--provider") + 1] == "openai-codex"
        assert argv[argv.index("--model") + 1] == model
        assert argv[-2:] == ["--", "- brief"]
        for flag in (
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-mcp",
        ):
            assert flag in argv
        assert team_cycle.engine_supported("openai-codex-canonical", model)
        assert model_ranking._is_agent_harness_provider("openai-codex-canonical")
        assert team_cycle.engine_identity("openai-codex-canonical", model) == (
            "openai-codex",
            model,
        )
    assert owner_reviews.is_reviewer_engine("openai-codex-canonical", "gpt-6.1-sol")
    assert not owner_reviews.is_reviewer_engine("openai-codex-canonical", "gpt-6-luna")
    assert not team_cycle.engine_supported("openai-codex-canonical", "arbitrary-model")
    assert (
        runner.harness_cmd(
            "openai-codex-canonical", "arbitrary-model", "low", "", "", ""
        )
        is None
    )
    assert runner.harness_cmd("openai-codex", "gpt-6.1-sol", "low", "", "", "") is None
    assert (
        runner.harness_cmd("openai-codex-1", "gpt-6.1-sol", "low", "", "", "") is None
    )
    _, primary_env = runner.harness_cmd(
        "openai-codex-2", "gpt-6.1-sol", "low", "", "", ""
    )
    assert primary_env == {"PI_CODING_AGENT_DIR": "/root/.pi/accounts/openai-codex-2"}
    assert "/root/.pi/accounts/openai-codex-canonical" not in runner._pi_dirs(
        "openai-codex-canonical", tmp_path
    )


@pytest.mark.parametrize(
    "bad",
    [
        "absent-pin",
        "wrong-account",
        "empty-account",
        "empty-access",
        "wrong-type",
        "missing-expiry",
        "bool-expiry",
        "nan-expiry",
        "expired",
        "short-window",
        "symlink-auth",
    ],
)
def test_canonical_bad_authority_refuses_before_worker(
    runner, monkeypatch, tmp_path, bad
):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    auth = {
        "openai-codex": {
            "type": "oauth",
            "access": "fixture",
            "accountId": "fixture-account",
            "expires": (time.time() + 5000) * 1000,
        }
    }
    pin = hashlib.sha256(b"fixture-account").hexdigest()
    if bad == "wrong-account":
        pin = hashlib.sha256(b"other-account").hexdigest()
    if bad == "empty-account":
        auth["openai-codex"]["accountId"] = ""
    if bad == "empty-access":
        auth["openai-codex"]["access"] = ""
    if bad == "wrong-type":
        auth["openai-codex"]["type"] = "api_key"
    if bad == "missing-expiry":
        auth["openai-codex"].pop("expires")
    if bad == "bool-expiry":
        auth["openai-codex"]["expires"] = True
    if bad == "nan-expiry":
        auth["openai-codex"]["expires"] = float("nan")
    if bad == "expired":
        auth["openai-codex"]["expires"] = (time.time() - 1) * 1000
    if bad == "short-window":
        auth["openai-codex"]["expires"] = (time.time() + 305) * 1000
    (canonical / "auth.json").write_text(json.dumps(auth))
    if bad == "symlink-auth":
        target = tmp_path / "auth-target.json"
        (canonical / "auth.json").rename(target)
        (canonical / "auth.json").symlink_to(target)
    monkeypatch.setattr(runner, "CANONICAL_PI", canonical)
    monkeypatch.setenv("RUNNER_CANONICAL_PI_ACCOUNT_SHA256", pin)
    if bad == "absent-pin":
        monkeypatch.delenv("RUNNER_CANONICAL_PI_ACCOUNT_SHA256")
    monkeypatch.setattr(runner, "api", lambda *_: {})
    real_popen = runner.subprocess.Popen

    def no_worker(argv, *args, **kwargs):
        assert Path(argv[0]).name != "bwrap", "unauthorized worker started"
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", no_worker)
    claim = {
        "role_kind": "qa",
        "attempt": {
            "id": "a",
            "provider": "openai-codex-canonical",
            "model": "gpt-6.1-sol",
            "thinking_level": "low",
        },
    }
    result = runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert not result["success"] and not result["usage_observed"]
    assert (
        "canonical Pi account pin or access window"
        in (tmp_path / "out.raw").read_text()
    )


def test_canonical_sandbox_only_exposes_pinned_auth_and_safe_controls(
    runner, monkeypatch, tmp_path
):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    auth = canonical / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "fixture",
                    "accountId": "fixture-account",
                    "expires": (time.time() + 5000) * 1000,
                }
            }
        )
    )
    before = auth.read_bytes()
    (canonical / "settings.json").write_text(
        json.dumps(
            {
                "packages": ["must-not-load"],
                "compaction": {
                    "enabled": True,
                    "reserveTokens": 16384,
                    "keepRecentTokens": 12000,
                },
            }
        )
    )
    (canonical / "models.json").write_text(
        '{"providers":{"secret-provider":{"apiKey":"fixture"}}}'
    )
    (canonical / "unrelated.json").write_text("private")
    (canonical / "extensions").mkdir()
    cwd = tmp_path / "source"
    cwd.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(runner, "CANONICAL_PI", canonical)
    monkeypatch.setenv(
        "RUNNER_CANONICAL_PI_ACCOUNT_SHA256",
        hashlib.sha256(b"fixture-account").hexdigest(),
    )
    assert runner.canonical_pi_authorized(30)
    with pytest.raises(RuntimeError):
        runner.sandbox_command(
            ["true"], cwd, scratch, writable=False, native_account=canonical
        )
    command = runner.sandbox_command(
        ["true"],
        cwd,
        scratch,
        writable=False,
        native_account=canonical,
        canonical_pi=True,
    )
    assert ["--bind", str(canonical), str(canonical)] in [
        command[i : i + 3] for i in range(len(command))
    ]
    assert ["--ro-bind", "/dev/null", str(canonical / "unrelated.json")] in [
        command[i : i + 3] for i in range(len(command))
    ]
    assert ["--tmpfs", str(canonical / "extensions")] in [
        command[i : i + 2] for i in range(len(command))
    ]
    assert ["--ro-bind", str(cwd), str(cwd)] in [
        command[i : i + 3] for i in range(len(command))
    ]
    assert json.loads((scratch / "canonical-settings.json").read_text()) == {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16384,
            "keepRecentTokens": 12000,
        }
    }
    assert json.loads((scratch / "canonical-models.json").read_text()) == {
        "providers": {}
    }
    assert auth.read_bytes() == before and not list(scratch.glob("*auth*"))
    subprocess.run(command, check=True)
    monkeypatch.delenv("RUNNER_CANONICAL_PI_ACCOUNT_SHA256")
    with pytest.raises(RuntimeError):
        runner.sandbox_command(
            ["true"],
            cwd,
            scratch,
            writable=False,
            native_account=canonical,
            canonical_pi=True,
        )


@pytest.mark.parametrize("after_valid", [True, False])
def test_canonical_success_uses_native_account_and_never_key_runtime(
    runner, monkeypatch, tmp_path, after_valid
):
    import mc_pi_runtime

    authority = iter([True, after_valid])
    monkeypatch.setattr(
        runner, "canonical_pi_authorized", lambda _timeout: next(authority)
    )
    monkeypatch.setattr(
        mc_pi_runtime,
        "key_runtime",
        lambda *_a: pytest.fail("OAuth sent through API-key runtime"),
    )
    frame = {
        "type": "agent_end",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "fixture"}],
                "usage": {"input": 5, "output": 2, "totalTokens": 7},
            }
        ],
    }
    program = f"print({json.dumps(frame)!r})"
    monkeypatch.setattr(
        runner,
        "harness_cmd",
        lambda *_a: (
            [sys.executable, "-c", program],
            {"PI_CODING_AGENT_DIR": str(runner.CANONICAL_PI)},
        ),
    )
    seen = []

    def sandbox(argv, *_a, **kw):
        seen.append(kw)
        return argv

    monkeypatch.setattr(runner, "sandbox_command", sandbox)
    monkeypatch.setattr(runner, "api", lambda *_a: {})
    claim = {
        "role_kind": "qa",
        "attempt": {
            "id": "a",
            "provider": "openai-codex-canonical",
            "model": "gpt-6.1-sol",
            "thinking_level": "low",
        },
    }
    result = runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert result["success"] is after_valid
    assert result["usage_observed"] and result["usage"]["total_tokens"] == 7
    if not after_valid:
        assert (
            result["text"]
            == "canonical Pi account pin or access window changed; output discarded"
        )
    assert seen == [
        {"writable": False, "native_account": runner.CANONICAL_PI, "canonical_pi": True}
    ]
