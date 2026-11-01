"""Synthetic executor caller contracts; no live entrypoints, providers or private files.

Historical Inbox rows are metadata fixtures, NOT the current parent architecture.
Shell-embedded Python/function bodies are taken from source, not reimplemented.
All transports and runtime discovery are intercepted; positive cases are not approval.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.core import private_access
from app.services import model_ranking, team_cycle

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mc_pi_runtime as runtime  # noqa: E402 - trusted source helper only

PRIVATE_BOARD = "00000000-0000-4000-8000-00000000b0a7"
QWEN = "vllm/Qwen3.8-27B-FP8"
HISTORICAL_IDS = (
    "00000000-0000-4000-8000-0000000000c1",
    "00000000-0000-4000-8000-0000000000c2",
)


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(monkeypatch):
    module = load("mc-team-runner.py")
    # Never inspect runtime/profile paths even for directory/access probes.
    monkeypatch.setattr(module.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(module.os, "access", Mock(side_effect=AssertionError("private access")))
    monkeypatch.setattr(module, "BOARD", PRIVATE_BOARD)
    monkeypatch.setattr(module, "api", Mock(return_value={}))
    monkeypatch.setattr(module, "sandbox_command", Mock(side_effect=AssertionError("spawn")))
    return module


def historical(task_id):
    return {
        "id": task_id,
        "board_id": PRIVATE_BOARD,
        "title": "synthetic metadata only",
        "status": "inbox",
        "execution_lane": "harness",
        "execution_mode": "single",
        "harness_provider": "omniroute-api",
        "harness_model": QWEN,
        "workspace_path": None,
        "assigned_agent_id": None,
        "blocked_by_task_ids": [],
        "in_progress_at": None,
        "max_seconds_budget": 1800,
        "max_tokens_budget": None,
        "complexity": 3,
        "updated_at": "2026-01-01T00:00:00",
    }


def ranking(**overrides):
    entry = {
        "provider": "omniroute-api",
        "model": QWEN,
        "execution": "model_endpoint",
        "write_enabled": False,
        "fully_trusted": True,
        "max_complexity": 3,
        "max_concurrency": 1,
        "eligible": False,
        "disabled": False,
        "block_reason": "provider_unregistered",
        "level": 3,
        "rank": 0,
    }
    entry.update(overrides)
    return {"levels": [{"entries": [entry]}], "max_level_for_complexity": {"3": 3}}


def embedded_python(marker):
    source = (SCRIPTS / "mc-claude-runner.sh").read_text()
    blocks = re.findall(r"python3 -c '\n(.*?)\n' ", source, re.S)
    matches = [block for block in blocks if marker in block]
    assert len(matches) == 1, "legacy source block moved; review the actual caller"
    return matches[0]


def run_selection(monkeypatch, marker, data, *args):
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    monkeypatch.setattr(sys, "argv", ["synthetic-selection", *args])
    with contextlib.redirect_stdout(output):
        try:
            exec(compile(embedded_python(marker), "legacy-selection", "exec"), {})
        except SystemExit as exc:
            assert exc.code in (None, 0)
    return output.getvalue().strip()


@pytest.mark.parametrize("task_id", HISTORICAL_IDS)
def test_historical_single_inbox_selection_refusal_and_native_exclusion(monkeypatch, task_id):
    task, board = historical(task_id), ranking()
    before = copy.deepcopy((task, board))
    selected = run_selection(monkeypatch, "def owns(t)", {"items": [task]}, "[agent]")
    fields = selected.split("\t")
    assert fields[:4] == [task_id, "3", "1800", "-"]
    assert fields[5:9] == ["omniroute-api", QWEN, "-", "single"]
    refusal = run_selection(monkeypatch, "matched = False", board, "omniroute-api", QWEN, "3")
    assert refusal == "engine is on read-only probation; legacy execution writes"
    pool = load("mc-team-pool.py")
    assert not pool.schedulable(task) and not pool.eligible(task)
    assert not team_cycle.write_engine_allowed(board, "omniroute-api", QWEN)
    with pytest.raises(team_cycle.TeamError, match="not a supported harness engine"):
        team_cycle.validate_engine(board, "omniroute-api", QWEN)
    assert (
        task,
        board,
    ) == before  # pins, mode, eligibility, budget, lifecycle untouched


@pytest.mark.parametrize("task_id", HISTORICAL_IDS)
def test_historical_dispatch_refuses_with_intercepted_process_and_api(tmp_path, task_id):
    task = historical(task_id)
    before = copy.deepcopy(task)
    source = (SCRIPTS / "mc-claude-runner.sh").read_text()
    function = (
        "run_harness() {" + source.split("run_harness() {", 1)[1].split("\n}\n", 1)[0] + "\n}"
    )
    # Execute ONLY the source-defined dispatch function, not the live entrypoint.
    # python/result-parser, API and executable boundaries are shell-function stubs.
    intercepted = r"""
set -eu
TASK_ID="$1"; prefix="$2"; HERE="$3"; LOG="$prefix.log"; MC=intercept_mc
LEAD_TIMEOUT=10; TASK_THINKING=low; PONYTAIL_SKILL=""
python3() {
  printf 'parser\n' >>"$prefix.calls"
  case "$*" in
    *effective_rc*) printf '78\n' ;;
    *) printf '{}\n' ;;
  esac
}
intercept_mc() { printf '%s\n' "$*" >>"$prefix.calls"; }
timeout() { echo 'unexpected process spawn' >&2; return 99; }
pi() { echo 'unexpected pi' >&2; return 99; }
claude() { echo 'unexpected claude' >&2; return 99; }
"""
    prefix = tmp_path / "dispatch"
    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            intercepted
            + function
            + '\nrun_harness omniroute-api "vllm/Qwen3.8-27B-FP8" synthetic "$4" "$prefix" 10',
            "synthetic",
            task_id,
            str(prefix),
            str(SCRIPTS),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.stdout == completed.stderr == ""
    assert prefix.with_suffix(".raw").read_text() == (
        "Missing or unsupported harness profile: omniroute-api/" + QWEN + "\n"
    )
    assert prefix.with_suffix(".rc").read_text() == "78\n"
    assert prefix.with_suffix(".calls").read_text().splitlines() == [
        "parser",
        "parser",
        "parser",
        "post /api/v1/ranking/usage {}",
    ]
    assert task == before


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"eligible": True},
            "engine is on read-only probation; legacy execution writes",
        ),
        (
            {
                "write_enabled": True,
                "availability_reason": "synthetic account unavailable",
            },
            "synthetic account unavailable",
        ),
        (
            {"write_enabled": True, "eligible": True, "max_complexity": 2},
            "task exceeds engine complexity ceiling",
        ),
    ],
)
def test_pinned_refusal_order_never_conflates_trust_and_writes(monkeypatch, overrides, expected):
    board = ranking(**overrides)
    before = copy.deepcopy(board)
    assert (
        run_selection(monkeypatch, "matched = False", board, "omniroute-api", QWEN, "3") == expected
    )
    assert board == before


def test_unpinned_legacy_filters_endpoint_and_readonly_without_fallback_to_them(
    monkeypatch,
):
    board = ranking(eligible=True)
    entries = board["levels"][0]["entries"]
    entries.append({**entries[0], "provider": "synthetic-endpoint", "write_enabled": True})
    entries.append({**entries[0], "provider": "synthetic-readonly", "execution": "agent_harness"})
    assert run_selection(monkeypatch, "max_level =", board, "3", "synthetic") == ""
    entries.append(
        {
            **entries[0],
            "provider": "omniroute-codex",
            "model": "gpt-6.1-sol",
            "execution": "agent_harness",
            "write_enabled": True,
        }
    )
    assert run_selection(monkeypatch, "max_level =", board, "3", "synthetic") == (
        "omniroute-codex\tgpt-6.1-sol\tqualified"
    )  # selection only: legacy dispatch support is a separate contract


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, True),
        ({"status": "in_progress"}, True),
        ({"status": "review"}, False),
        ({"status": "paused"}, False),
        ({"execution_lane": "agent"}, False),
        ({"is_blocked": True}, False),
        ({"blocked_by_task_ids": ["upstream"]}, False),
        ({"scheduled_at": "2999-01-01T00:00:00Z"}, False),
        ({"split_count": 2}, False),
    ],
)
def test_pool_team_no_workspace_gates_are_metadata_only(overrides, expected):
    task = {**historical("synthetic-team"), "execution_mode": "team", **overrides}
    before = copy.deepcopy(task)
    pool = load("mc-team-pool.py")
    assert pool.schedulable(task) is expected
    assert task == before


@pytest.mark.parametrize("task_id", HISTORICAL_IDS)
def test_native_admission_refuses_explicit_single_before_roster_or_task_mutation(
    task_id,
):
    row = historical(task_id)
    task = SimpleNamespace(**{**row, "id": UUID(row["id"]), "board_id": UUID(PRIVATE_BOARD)})
    before = copy.deepcopy(vars(task))
    session = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock(), exec=AsyncMock())
    with pytest.raises(team_cycle.TeamError, match="task is not in team mode"):
        asyncio.run(
            team_cycle.start_cycle(session, SimpleNamespace(id=UUID(PRIVATE_BOARD)), task, ranking())
        )
    session.exec.assert_not_called()
    assert vars(task) == before


@pytest.mark.parametrize("model", [QWEN, "vllm/Qwen3.8-27B-FP8/wrong", "Qwen3.8-27B-FP8"])
def test_native_unsupported_executor_before_runtime_credentials_or_spawn(runner, tmp_path, model):
    claim = {
        "board_id": PRIVATE_BOARD,
        "role_kind": "specialist",
        "capability": "read_only",
        "attempt": {
            "id": "synthetic",
            "provider": "omniroute-api",
            "model": model,
            "thinking_level": "low",
        },
    }
    before = copy.deepcopy(claim)
    result = runner._run_harness(claim, "public synthetic", tmp_path, tmp_path, 10)
    assert result["text"] == "private-board transport required"
    assert not result["success"] and not result["retryable"] and not result["usage_observed"]
    assert result["usage"]["success"] is False and "total_tokens" not in result["usage"]
    runner.sandbox_command.assert_not_called()
    runner.os.access.assert_not_called()
    runner.api.assert_not_called()
    assert claim == before and not list(tmp_path.iterdir())


def synthetic_config(tmp_path):
    compat = {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
    }
    provider = {
        "api": "openai-completions",
        "baseUrl": "http://synthetic.invalid/v1",
        "compat": compat,
        "headers": {"X-Preserved": "synthetic"},
        "models": [
            {"id": QWEN, "compat": compat, "contextWindow": 57344},
            {"id": "codex/gpt-6.1-sol", "compat": {"supportsStore": True}},
            {"id": "unrelated-model"},
        ],
    }
    source = tmp_path / "models.json"
    config = {"providers": {"omniroute-api": provider, "unrelated-provider": {"models": []}}}
    source.write_text(json.dumps(config))
    settings = {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16384,
            "keepRecentTokens": 20000,
            "modelOverrides": {
                "omniroute-api/" + QWEN: {"keepRecentTokens": 12000},
                "omniroute-api/Qwen3.8-27B-FP8": {"keepRecentTokens": 17},
            },
        },
        "extensions": ["MUST_NOT_INHERIT"],
        "shellCommandPrefix": "MUST_NOT_RUN",
    }
    source.with_name("settings.json").write_text(json.dumps(settings))
    return source, config, settings


@pytest.mark.parametrize("model,recent", [(QWEN, 12000), ("codex/gpt-6.1-sol", 20000)])
def test_generated_exact_model_auth_compaction_compat_and_provider_exclusion(
    tmp_path, model, recent
):
    source, original, settings = synthetic_config(tmp_path)
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    path = runtime.key_runtime("omniroute-api", model, scratch, source)
    generated = json.loads((path / "models.json").read_text())
    expected = copy.deepcopy(original["providers"]["omniroute-api"])
    expected["models"] = [m for m in expected["models"] if m["id"] == model]
    assert generated == {"providers": {"omniroute-api": expected}}
    assert json.loads((path / "auth.json").read_text()) == {}
    assert json.loads((path / "settings.json").read_text()) == {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16384,
            "keepRecentTokens": recent,
        }
    }
    assert path.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())
    assert json.loads(source.read_text()) == original
    assert json.loads(source.with_name("settings.json").read_text()) == settings


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", 1),
        ("enabled", "true"),
        ("reserveTokens", True),
        ("reserveTokens", -1),
        ("reserveTokens", 1.5),
        ("keepRecentTokens", 2**53),
    ],
)
def test_invalid_resolved_compaction_refuses_before_runtime_creation(tmp_path, field, value):
    source, _, settings = synthetic_config(tmp_path)
    settings["compaction"]["modelOverrides"]["omniroute-api/" + QWEN] = {field: value}
    if field == "enabled":  # only top-level enabled is part of the real contract
        settings["compaction"][field] = value
    source.with_name("settings.json").write_text(json.dumps(settings))
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    with pytest.raises(RuntimeError, match="invalid Pi compaction settings"):
        runtime.key_runtime("omniroute-api", QWEN, scratch, source)
    assert not list(scratch.iterdir())


@pytest.mark.parametrize("fault", ["absent_runtime", "wrong_model", "wrong_provider"])
def test_key_runtime_refuses_missing_exact_configuration_without_artifacts(tmp_path, fault):
    source, _, _ = synthetic_config(tmp_path)
    if fault == "absent_runtime":
        source.unlink()
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    provider = "unsupported" if fault == "wrong_provider" else "omniroute-api"
    model = "Qwen3.8-27B-FP8" if fault == "wrong_model" else QWEN
    with pytest.raises((RuntimeError, OSError)):
        runtime.key_runtime(provider, model, scratch, source)
    assert not list(scratch.iterdir())


def test_real_omniroute_mapping_then_runtime_setup_failure_keeps_unknown(
    runner, tmp_path, monkeypatch
):
    argv, extra = runner.harness_cmd(
        "omniroute-codex", "gpt-6.1-sol", "high", "public", "sessions", "fresh"
    )
    assert argv[argv.index("--provider") + 1] == "omniroute-api"
    assert argv[argv.index("--model") + 1] == "codex/gpt-6.1-sol"
    assert argv[argv.index("--session-id") + 1] == "mc-team-fresh"
    assert argv[-2:] == ["--", "public"]
    source, _, _ = synthetic_config(tmp_path)
    source.unlink()  # absent synthetic runtime, never inspect the default
    real_key_runtime = runtime.key_runtime
    monkeypatch.delenv("FIXTURE_LOG", raising=False)
    monkeypatch.setattr(
        runtime,
        "key_runtime",
        lambda p, m, scratch, **kw: real_key_runtime(p, m, scratch, source, **kw),
    )
    claim = {
        "board_id": PRIVATE_BOARD,
        "role_kind": "specialist",
        "capability": "read_only",
        "attempt": {
            "id": "fresh",
            "provider": "omniroute-codex",
            "model": "gpt-6.1-sol",
            "thinking_level": "high",
        },
    }
    result = runner.run_harness(claim, "public", tmp_path, tmp_path, 10)
    assert not result["success"] and not result["usage_observed"]
    assert result["text"] == "harness runtime setup failed (FileNotFoundError)"
    assert "total_tokens" not in result["usage"]
    runner.sandbox_command.assert_not_called()
    runner.os.access.assert_not_called()
    runner.api.assert_called_once_with("post", "/api/v1/ranking/usage", result["usage"])


def test_real_mapping_generated_private_worker_spec_without_spawning(runner, tmp_path, monkeypatch):
    source, original, _ = synthetic_config(tmp_path)
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    real_key_runtime = runtime.key_runtime
    monkeypatch.delenv("FIXTURE_LOG", raising=False)
    monkeypatch.setattr(
        runtime,
        "key_runtime",
        lambda p, m, scratch, **kw: real_key_runtime(p, m, scratch, source, **kw),
    )
    captured = []

    class BoundaryIntercepted(Exception):
        pass

    def sandbox(argv, *args, **kwargs):
        captured.append((argv, kwargs))
        raise BoundaryIntercepted

    monkeypatch.setattr(runner, "sandbox_command", sandbox)
    claim = {
        "board_id": PRIVATE_BOARD,
        "role_kind": "qa",
        "capability": "write_worktree",
        "attempt": {
            "id": "fresh-qa",
            "provider": "omniroute-codex",
            "model": "gpt-6.1-sol",
            "thinking_level": "high",
        },
    }
    with pytest.raises(BoundaryIntercepted):
        runner._run_harness(claim, "synthetic public", tmp_path, scratch, 10)
    argv, kw = captured[0]
    spec = json.loads(argv[2])
    assert (spec["provider"], spec["model"]) == ("omniroute-api", "codex/gpt-6.1-sol")
    assert spec["session_id"] == "mc-team-fresh-qa" and spec["write_worktree"] is False
    assert not kw["writable"] and kw["native_account"] is None
    assert spec["settings"] == {
        "compaction": {
            "enabled": True,
            "reserveTokens": 16384,
            "keepRecentTokens": 20000,
        }
    }
    assert Path(argv[1]).read_bytes() == (SCRIPTS / "private-pi-worker.mjs").read_bytes()
    assert json.loads(source.read_text()) == original
    runner.api.assert_not_called()  # no request/accounting evidence manufactured


@pytest.mark.parametrize("task_id", HISTORICAL_IDS)
def test_real_ranking_reports_unregistered_not_trust_or_readonly_authority(monkeypatch, task_id):
    task = historical(task_id)
    metadata = {
        "models": {
            "omniroute-api": {
                QWEN: {
                    "level": 3,
                    "rank": 0,
                    "fully_trusted": True,
                    "write_enabled": False,
                    "max_complexity": 3,
                    "max_concurrency": 1,
                }
            }
        }
    }
    before = copy.deepcopy((task, metadata))
    monkeypatch.setattr(model_ranking.mc_sidecar, "load_sidecar", lambda: metadata)
    monkeypatch.setattr(model_ranking, "compute_usage", lambda *a: [])
    board = asyncio.run(model_ranking.get_board([]))
    entry = next(e for level in board["levels"] for e in level["entries"])
    assert entry["provider"] == task["harness_provider"] and entry["model"] == QWEN
    assert entry["provider_status"] == "unregistered" and not entry["eligible"]
    assert (
        entry["execution"] == "model_endpoint" and entry["block_reason"] == "provider_unregistered"
    )
    assert entry["fully_trusted"] is True and entry["write_enabled"] is False
    assert (task, metadata) == before


@pytest.fixture
def local(tmp_path, monkeypatch):
    module = load("private-local-review.py")
    sidecar = tmp_path / "openclaw.mc.json"
    sidecar.write_text(
        json.dumps(
            {
                "models": {
                    module.ROUTER_PROVIDER: {
                        module.ROUTER_MODEL: {
                            "fully_trusted": True,
                            "write_enabled": False,
                        }
                    }
                }
            }
        )
    )
    sidecar.chmod(0o600)
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "QWEN_SLOT", tmp_path / "qwen.lock")
    monkeypatch.setattr(module, "OUTPUT_ROOT", tmp_path / "restricted-output")
    monkeypatch.setattr(module, "ROUTER_CONFIG", tmp_path / "models.json")
    monkeypatch.setattr(
        module, "read_document", Mock(side_effect=AssertionError("protected input"))
    )
    monkeypatch.setattr(
        module.http.client,
        "HTTPConnection",
        Mock(side_effect=AssertionError("network")),
    )
    return module


def local_review(local):
    return local.review(Path("/synthetic-never-read"), local.ROUTER_PROVIDER, local.ROUTER_MODEL)


@pytest.mark.parametrize("trust", [False, None, "true", 1])
def test_local_unapproved_trust_refuses_before_credentials_input_or_network(
    local, tmp_path, monkeypatch, trust
):
    path = tmp_path / "openclaw.mc.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    local.ROUTER_PROVIDER: {
                        local.ROUTER_MODEL: {
                            "fully_trusted": trust,
                            "write_enabled": False,
                        }
                    }
                }
            }
        )
    )
    key = Mock(side_effect=AssertionError("private credentials"))
    monkeypatch.setattr(local, "router_key", key)
    assert local_review(local) == {
        "status": "refused",
        "reason": "local_review_failed_closed",
    }
    key.assert_not_called()
    local.read_document.assert_not_called()
    local.http.client.HTTPConnection.assert_not_called()


def test_local_absent_runtime_refuses_before_protected_input_and_network(local):
    # The trusted flag is synthetic; the pinned synthetic runtime file is absent.
    assert local_review(local) == {
        "status": "refused",
        "reason": "local_review_failed_closed",
    }
    local.read_document.assert_not_called()
    local.http.client.HTTPConnection.assert_not_called()


@pytest.mark.parametrize("model", ["Qwen3.8-27B-FP8", "vllm/other", "codex/gpt-6.1-sol"])
def test_local_wrong_router_model_refuses_before_any_authority_or_input(local, monkeypatch, model):
    gate = Mock(side_effect=AssertionError("must refuse before authority read"))
    monkeypatch.setattr(local, "require_jira_access", gate)
    assert local.review(Path("/never-read"), local.ROUTER_PROVIDER, model) == {
        "status": "refused",
        "reason": "local_model_required",
    }
    gate.assert_not_called()
    local.read_document.assert_not_called()
    local.http.client.HTTPConnection.assert_not_called()


def test_local_exact_router_transport_identity_and_strict_readonly_scope(local, monkeypatch):
    # No key file is accessed: intercept only the credential boundary after admission.
    monkeypatch.setattr(local, "router_key", lambda: "synthetic")
    response = Mock(status=200)
    response.getheader.return_value = "no-store"
    response.read.return_value = json.dumps(
        {
            "model": local.MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "synthetic restricted response",
                    }
                }
            ],
        }
    ).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(local.http.client, "HTTPConnection", Mock(return_value=connection))
    local.call_router_model("synthetic restricted input")
    method, path, body, headers = connection.request.call_args.args
    assert method == "POST" and path == "/v1/private/chat/completions"
    assert json.loads(body)["model"] == headers["X-OMC-Model"] == QWEN
    assert headers["X-OMC-Source-Method"] == "GET" and headers["Cache-Control"] == "no-store"
    assert "tools" not in json.loads(body)
    connection.close.assert_called_once()
    # Neither a trusted parent/cloud engine nor a write method authorizes this leaf.
    for provider, model, endpoint, verb in [
        ("omniroute-codex", "gpt-6.1-sol", local.ROUTER_ENDPOINT, "GET"),
        (local.ROUTER_PROVIDER, QWEN, local.ROUTER_ENDPOINT, "POST"),
        (local.ROUTER_PROVIDER, QWEN, "https://synthetic.invalid/v1", "GET"),
    ]:
        with pytest.raises(PermissionError):
            private_access.require_jira_access(provider, model, endpoint, verb)


@pytest.mark.parametrize("failure", ["unavailable", "later_input_failure", None])
def test_local_preflight_private_result_and_measured_failure_accounting(
    local, monkeypatch, failure
):
    events = []
    measured = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    restricted = "SYNTHETIC_RESTRICTED_PAYLOAD_DO_NOT_PROJECT"

    def request(document=None):
        events.append(document)
        if document is None:
            return {"admitted": True, "model": QWEN, "jira_method": "GET"}
        if failure == "unavailable":
            raise OSError(restricted)
        return {
            "model": local.MODEL,
            "usage": measured,
            "choices": [{"message": {"content": restricted}}],
        }

    monkeypatch.setattr(local, "call_router_model", request)

    def read(path):
        assert events == [None, local.ROUTER_CANARY]
        if failure == "later_input_failure":
            raise OSError(restricted)
        return restricted

    read_spy = Mock(side_effect=read)
    monkeypatch.setattr(local, "read_document", read_spy)
    result = local_review(local)
    assert restricted not in json.dumps(result)
    if failure:
        assert result == {"status": "refused", "reason": "local_review_failed_closed"}
        if failure == "unavailable":
            read_spy.assert_not_called()
        else:
            receipts = list(local.OUTPUT_ROOT.glob("usage-*.json"))
            assert len(receipts) == 1  # measured canary retained despite subsequent failure
            receipt = json.loads(receipts[0].read_text())
            assert receipt["usage"] == measured and receipt["billing"] == "unknown"
            assert restricted not in receipts[0].read_text()
    else:
        assert result["status"] == "completed_awaiting_disclosure_approval"
        assert result["no_cloud_fallback"] and result["no_model_tools"]
        stored = local.OUTPUT_ROOT / (result["job_id"] + ".json")
        assert (
            json.loads(stored.read_text())["classification"] == "restricted_local_only_unapproved"
        )
        assert restricted in stored.read_text() and stored.stat().st_mode & 0o777 == 0o600
    local.http.client.HTTPConnection.assert_not_called()


def test_missing_usage_is_unknown_and_creates_no_zero_receipt(local):
    local.OUTPUT_ROOT.mkdir(mode=0o700)
    local.record_router_usage({"choices": []})
    assert not list(local.OUTPUT_ROOT.iterdir())


def test_acceptance_caller_safe_projection_unknown_evidence_and_fresh_review_lease(
    tmp_path, monkeypatch
):
    review = load("mc-review-agent.py")
    team = review.team
    source, scratch, private = (tmp_path / name for name in ("source", "review", "restricted"))
    for path in (source, scratch, private):
        path.mkdir(mode=0o700)
    restricted = "SYNTHETIC_RESTRICTED_PAYLOAD_MUST_STAY_LOCAL"
    (private / "raw.txt").write_text(restricted)
    monkeypatch.setattr(team, "LOCAL_ONLY_ROOTS", (private,))
    with pytest.raises(RuntimeError, match="local-only source"):
        team.refuse_local_only_projection(private)
    candidate = {
        "id": "synthetic",
        "board_id": PRIVATE_BOARD,
        "kind": "task",
        "status": "review",
        "fingerprint": "synthetic-current",
        "evidence": {
            "stored_reports": ["PUBLIC_SAFE_PROJECTION" + "x" * (review.MAX_EVIDENCE_CHARS + 1)],
            "evidence_files": {"proof.txt": "PUBLIC_SAFE_PROJECTION"},
            "artifact_complete": False,
            "billing": "UNKNOWN",
            "unavailable_reports": ["UNKNOWN"],
            "failed_reports": ["synthetic retained failure"],
        },
    }
    before = copy.deepcopy(candidate)
    monkeypatch.setattr(review, "_review_workspace", lambda *a: (source, []))
    monkeypatch.setattr(review.tempfile, "mkdtemp", lambda **kw: str(scratch))
    monkeypatch.setattr(review, "STOP", review.threading.Event())
    calls, captured = [], []

    def api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/v1/review/automation":
            return {
                "enabled": True,
                "models": [
                    {
                        "eligible": True,
                        "provider": "omniroute-codex",
                        "model": "gpt-6.1-sol",
                    }
                ],
            }
        if path.endswith("/review-lease"):
            return {"lease_id": "synthetic-lease"}
        return {}

    monkeypatch.setattr(team, "api", api)

    def harness(claim, prompt, cwd, tmp, timeout, cancelled):
        captured.append(claim)
        assert claim["board_id"] == PRIVATE_BOARD and claim["role_kind"] == "reviewer"
        assert "Read the COMPLETE evidence bundle" in prompt
        projected = json.loads((tmp / "acceptance-evidence.json").read_text())
        assert projected["evidence"] == candidate["evidence"]
        assert (team.evidence_dir(tmp) / "proof.txt").read_text() == "PUBLIC_SAFE_PROJECTION"
        assert restricted not in json.dumps(projected) + prompt
        assert "UNKNOWN" in json.dumps(projected) and "synthetic retained failure" in json.dumps(
            projected
        )
        return {
            "success": False,
            "usage_observed": False,
            "text": "synthetic startup unavailable",
        }

    monkeypatch.setattr(team, "run_harness", harness)
    state = {}
    assert (
        review.review_candidate(
            candidate,
            [{"provider": "omniroute-codex", "model": "gpt-6.1-sol"}],
            set(),
            state,
        )
        is None
    )
    assert len(captured) == 1 and captured[0]["attempt"]["id"].startswith("review-")
    admission = next(body for method, path, body in calls if path.endswith("/review-lease"))
    assert admission["reviewer_session_id"] in captured[0]["attempt"]["id"]
    assert any(body and body.get("action") == "release" for _, _, body in calls)
    assert not any(path == "/api/v1/review/decisions" for _, path, _ in calls)
    assert candidate == before and "synthetic-current" in state["failed"]
    assert (private / "raw.txt").read_text() == restricted


@pytest.mark.parametrize("fault", [None, "unsafe_evidence", "missing_tool", "failed_namespace"])
def test_source_defined_guarded_worker_projection_with_intercepted_sdk(tmp_path, fault):
    """Evaluate only the source function with mocked SDK/fs, never the managed entrypoint.

    SpiderMonkey is a stdlib-like local JS shell here; no installed Pi SDK, Node
    release selection, provider auth or network is inspected. This tests the
    function contract, NOT the deployment/SDK availability.
    """
    import shutil

    shell = shutil.which("js140")
    if not shell:
        pytest.skip("local JS shell unavailable; managed SDK not probed")
    source = (SCRIPTS / "private-pi-worker.mjs").read_text()
    function = source[source.index("const quote =") : source.index("\nif (process.argv[1]")]
    function = function.replace("export async function", "async function", 1)
    prelude = r"""
const fault = FAULT;
const assert = (value, message) => { if (!value) throw Error(message); };
const calls = [], reads = [];
let optionsSeen, disposed = false;
const resolve = p => p;
const homedir = () => '/root';
const randomBytes = () => ({toString: () => 'synthetic'});
const lstatSync = path => {
  reads.push(path);
  return {isFile: () => fault !== 'unsafe_evidence', size: 20};
};
const createExtensionRuntime = () => ({});
const SessionManager = {create: (...args) => ({args})};
const createBashTool = (cwd, options) => {
  const built = options.spawnHook({command: 'synthetic public command', cwd, env: {}});
  calls.push(built.command);
  return {execute: async (id, args) => {
    assert(id === 'private-startup', 'startup must precede prompting');
    assert(args.command.includes('test ! -e /proc/self/status'), 'namespace preflight absent');
    return {content: [{type: 'text', text: fault === 'failed_namespace' ? 'bad' : 'PRIVATE_NAMESPACE_READY'}]};
  }};
};
const createAgentSession = async options => {
  optionsSeen = options;
  return {session: {
    getActiveToolNames: () => fault === 'missing_tool' ? [] : ['bash'],
    agent: {state: {tools: options.customTools}},
    dispose: () => { disposed = true; },
  }};
};
""".replace("FAULT", json.dumps(fault))
    check = r"""
const spec = {cwd: '/synthetic/source', private_dir: '/synthetic/private',
  agent_dir: '/synthetic/private/api-runtime', session_dir: '/synthetic/private/sessions',
  session_id: 'synthetic-fresh', write_worktree: false};
(async () => {
  let refused = false;
  try { await createPrivateSession(spec); } catch { refused = true; }
  assert(refused === (fault !== null), 'wrong admission result');
  assert(reads.every(p => /^\/synthetic\/private\/(qa-evidence.json|qa-changes.diff|acceptance-evidence.json)$/.test(p)), 'unexpected filesystem access');
  if (fault === 'unsafe_evidence') {
    assert(calls.length === 0 && !optionsSeen, 'unsafe evidence reached SDK');
  } else {
    assert(optionsSeen.noTools === true && optionsSeen.customTools.length === 1, 'tool set widened');
    const resources = optionsSeen.resourceLoader;
    assert(resources.getExtensions().extensions.length === 0, 'host extensions inherited');
    assert(resources.getSkills().skills.length === 0 && resources.getAgentsFiles().agentsFiles.length === 0, 'host context inherited');
    const command = calls[0];
    for (const flag of ['--unshare-all', '--cap-drop', '--clearenv', '--tmpfs']) assert(command.includes(flag), 'isolation flag missing');
    assert(command.includes("'--ro-bind' '/synthetic/source' '/synthetic/source'"), 'worktree not read-only');
    assert(command.includes("'--tmpfs' '/root/.pi'"), 'credential namespace not masked');
    assert(command.includes("'--tmpfs' '/synthetic/private'"), 'private result namespace not masked');
    assert(command.includes("'--tmpfs' '/synthetic/private/api-runtime'"), 'runtime namespace not masked');
    assert(command.includes("'--ro-bind' '/synthetic/private/acceptance-evidence.json'"), 'whole public evidence not reopened');
    assert(disposed === (fault !== null), 'session cleanup wrong');
  }
  print(JSON.stringify({ok: true, function_contract_only: true}));
})().catch(error => { print(String(error)); quit(1); });
drainJobQueue();
"""
    # Public synthetic probe file, no live imports or provider payloads.
    probe = tmp_path / "worker-function-contract.js"
    probe.write_text(prelude + function + check)
    completed = subprocess.run([shell, str(probe)], text=True, capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"ok": True, "function_contract_only": True}


@pytest.mark.parametrize("task_id", HISTORICAL_IDS)
def test_inbox_projection_never_claims_running_without_a_live_worker(task_id):
    from app.models.tasks import Task
    from app.services.task_execution import task_execution_state

    task = Task.model_validate(historical(task_id))
    before = task.model_dump()
    session = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    projected = asyncio.run(task_execution_state(session, board_id=UUID(PRIVATE_BOARD), tasks=[task]))
    assert projected[task.id]["work_state"] is None
    assert projected[task.id]["work_started_at"] is None
    assert projected[task.id]["work_expires_at"] is None
    assert task.model_dump() == before


def test_all_raw_projection_consumers_refuse_before_inspection_or_spawn(
    runner, tmp_path, monkeypatch
):
    private, safe = tmp_path / "restricted", tmp_path / "safe"
    private.mkdir()
    safe.mkdir()
    monkeypatch.setattr(runner, "LOCAL_ONLY_ROOTS", (private,))
    inspect = Mock(side_effect=AssertionError("must refuse before host inspection"))
    monkeypatch.setattr(runner.shutil, "which", inspect)
    monkeypatch.setattr(Path, "rglob", inspect)
    monkeypatch.setattr(runner, "REPO", private)
    cycle = runner.Cycle.__new__(runner.Cycle)
    cycle.budget_ready = lambda: True
    with pytest.raises(RuntimeError, match="local-only"):
        cycle._execute({"attempt": {"id": "synthetic"}, "role_kind": "pm"}, safe, None)
    # Use the actual sandbox function, with only synthetic roots.
    team = load("mc-team-runner.py")
    monkeypatch.setattr(team, "LOCAL_ONLY_ROOTS", (private,))
    with pytest.raises(RuntimeError, match="local-only"):
        team.sandbox_command(["/usr/bin/true"], private, safe, writable=False)
    with pytest.raises(RuntimeError, match="local-only"):
        runner.collect_evidence(private)
    argv = []
    with pytest.raises(RuntimeError, match="local-only"):
        runner._ro_bind(argv, private / "synthetic", [])
    assert argv == []
    inspect.assert_not_called()


def test_source_defined_sandbox_mount_contract_without_runtime_inspection(tmp_path, monkeypatch):
    team = load("mc-team-runner.py")
    cwd, scratch, repo = (tmp_path / name for name in ("source", "private", "repo"))
    for path in (cwd, scratch, repo):
        path.mkdir()
    team.evidence_dir(scratch).mkdir()
    marker = cwd / ".env.synthetic"
    marker.write_text("SYNTHETIC_RESTRICTED_SENTINEL")
    monkeypatch.setattr(team, "REPO", repo)
    monkeypatch.setattr(team, "PONYTAIL", str(tmp_path / "absent"))
    monkeypatch.setattr(team, "MANAGED_RO", ())
    monkeypatch.setattr(team, "SYSTEM_RO", ("/usr",))
    monkeypatch.setattr(team, "_hidden_host_paths", lambda: [])
    monkeypatch.setattr(team, "_mount_program", Mock())
    monkeypatch.setattr(team, "fixtures_enabled", lambda: False)
    monkeypatch.setattr(team.shutil, "which", lambda _: "/usr/bin/true")
    argv = team.sandbox_command(["/usr/bin/true"], cwd, scratch, writable=False)
    triples = [argv[i : i + 3] for i in range(len(argv) - 2)]
    assert ["--ro-bind", str(cwd), str(cwd)] in triples
    assert ["--ro-bind", "/dev/null", str(marker)] in triples
    assert ["--bind", str(team.evidence_dir(scratch)), "/tmp"] in triples
    assert ["--ro-bind", "/", "/"] not in triples
    assert not any("auth.json" in arg or "models.json" in arg for arg in argv)


@pytest.mark.parametrize(
    "explicit,expected", [(None, "team"), ("single", "single"), ("team", "team")]
)
def test_create_mode_omission_and_explicit_choice_at_actual_validation_caller(
    monkeypatch, tmp_path, explicit, expected
):
    monkeypatch.setenv("HOME", str(tmp_path))
    from app.api import tasks as tasks_api
    from app.schemas.tasks import TaskCreate

    values = {
        "title": "synthetic create, never persisted",
        "execution_lane": "harness",
        "harness_provider": "omniroute-api",
        "harness_model": QWEN,
    }
    if explicit is not None:
        values["execution_mode"] = explicit
    payload = TaskCreate(**values)
    before = payload.model_dump()
    original_check = tasks_api._check_team_mode

    class ValidationObserved(Exception):
        pass

    def observe(mode, lane, workspace, assignee):
        original_check(mode, lane, workspace, assignee)
        assert (mode, lane, workspace, assignee) == (expected, "harness", None, None)
        raise ValidationObserved  # intercept BEFORE Task construction or any DB authority

    monkeypatch.setattr(tasks_api, "_check_team_mode", observe)
    session = SimpleNamespace(flush=AsyncMock(), add=Mock(), commit=AsyncMock())
    with pytest.raises(ValidationObserved):
        asyncio.run(
            tasks_api.create_task(
                payload,
                board=SimpleNamespace(id=UUID(PRIVATE_BOARD)),
                session=session,
                auth=SimpleNamespace(),
            )
        )
    assert payload.model_dump() == before
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("mode", ["single", "team"])
def test_edit_uses_same_mode_validation_without_implicit_conversion_or_dispatch(
    monkeypatch, tmp_path, mode
):
    monkeypatch.setenv("HOME", str(tmp_path))
    from app.api import tasks as tasks_api
    from app.models.tasks import Task
    from app.schemas.tasks import TaskUpdate

    task = Task.model_validate(historical(HISTORICAL_IDS[0]))
    before = task.model_dump()
    original_check = tasks_api._check_team_mode

    class ValidationObserved(Exception):
        pass

    def observe(selected, lane, workspace, assignee):
        original_check(selected, lane, workspace, assignee)
        assert (selected, lane, workspace, assignee) == (mode, "harness", None, None)
        raise ValidationObserved

    monkeypatch.setattr(tasks_api, "_check_team_mode", observe)
    monkeypatch.setattr(tasks_api, "_require_task_user_write_access", AsyncMock())
    monkeypatch.setattr(team_cycle, "active_cycle", AsyncMock(return_value=None))
    session = SimpleNamespace(
        refresh=AsyncMock(), add=Mock(), flush=AsyncMock(), commit=AsyncMock()
    )
    actor = SimpleNamespace(
        actor_type="user", user=SimpleNamespace()
    )  # synthetic only, no approval assertion
    with pytest.raises(ValidationObserved):
        asyncio.run(
            tasks_api.update_task(
                TaskUpdate(execution_mode=mode), task=task, session=session, actor=actor
            )
        )
    assert task.model_dump() == before
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    "provider,model", [("omniroute-codex", "unknown-model"), ("omniroute-api", QWEN)]
)
def test_wrong_native_model_or_executor_refuses_at_admission_before_runtime(
    runner, provider, model
):
    board = ranking(
        provider=provider, model=model, eligible=True, write_enabled=True, execution="agent_harness"
    )
    before = copy.deepcopy(board)
    with pytest.raises(team_cycle.TeamError, match="not a supported harness engine"):
        team_cycle.validate_engine(board, provider, model)
    runner.os.access.assert_not_called()
    runner.sandbox_command.assert_not_called()
    assert board == before
