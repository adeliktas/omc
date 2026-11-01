from __future__ import annotations

import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "mc-agent-result.py"


def _parse(tmp_path: Path, raw: str, rc: int = 1, kind: str = "pi", session: str | None = None,
           provider: str = "provider", model: str = "model") -> dict:
    output = tmp_path / "result.jsonl"
    output.write_text(raw)
    result = subprocess.run(
        ["python3", str(SCRIPT), kind, str(output), provider, model, str(rc)] + ([session] if session else []),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_completed_tools_are_observed_events_not_prompt_echoes(tmp_path: Path) -> None:
    events = [
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolCallId": "read-1", "toolName": "read"},
        {"type": "tool_execution_start", "toolCallId": "bash-1", "toolName": "bash"},
        {"type": "tool_execution_start", "toolCallId": "pending", "toolName": "write"},
        {"type": "tool_execution_end", "toolCallId": "read-1", "toolName": "read", "result": "private output"},
        {"type": "tool_execution_end", "toolCallId": "read-1", "toolName": "read"},
        {"type": "tool_execution_end", "toolCallId": "bash-1", "toolName": "bash", "isError": True},
        {"type": "tool_execution_end", "toolCallId": "unknown", "toolName": "private credential"},
        {"type": "message_end", "message": {"content": [{"type": "text", "text": "tool_execution_end"}]}},
    ]
    raw = "\n".join(map(json.dumps, events))
    parsed = _parse(tmp_path, raw)
    assert parsed["tool_counts"] == {"read": 1, "bash": 1}
    assert parsed["tool_activity"] == [
        {"tool": "read", "is_error": None}, {"tool": "bash", "is_error": True},
    ]
    assert "private output" not in json.dumps(parsed["tool_activity"])
    assert _parse(tmp_path, "opaque CLI summary")["tool_activity"] is None
    assert _parse(tmp_path, raw, kind="claude")["tool_activity"] is None
    assert _parse(tmp_path, "error: unknown option --bad\n" + raw, rc=2)["tool_activity"] is None
    assert _parse(tmp_path, "opaque CLI summary")["tool_counts"] is None
    assert _parse(tmp_path, raw, kind="claude")["tool_counts"] is None
    assert _parse(tmp_path, "error: unknown option --bad\n" + raw, rc=2)["tool_counts"] is None
    assert _parse(tmp_path, json.dumps(events[3]))["tool_counts"] is None
    for header in ("error: too many arguments", "RangeError: Maximum call stack size exceeded", "pi: fatal: session dir locked"):
        assert _parse(tmp_path, header + "\n" + raw, rc=2)["tool_counts"] is None
    events.insert(4, {"type": "tool_execution_start", "toolCallId": "read-2", "toolName": "read"})
    events.insert(5, {"type": "tool_execution_end", "toolCallId": "read-2", "toolName": "read", "result": "line\u2028separator"})
    unicode_raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    assert _parse(tmp_path, unicode_raw)["tool_counts"] == {"read": 2, "bash": 1}
    start = {"type": "tool_execution_start", "toolCallId": "same", "toolName": "read"}
    end = {**start, "type": "tool_execution_end"}
    def trace(*items):
        return _parse(tmp_path, "\n".join(json.dumps(item) for item in items))["tool_counts"]
    assert trace({"type": "agent_start"}, end, start) is None
    assert trace({"type": "session"}, start, {"type": "agent_start"}, end) is None
    assert trace({"type": "agent_start"}, start, {"type": "agent_end"}, end) is None
    assert trace({"type": "agent_start"}, start, end, end) == {"read": 1}
    assert trace({"type": "agent_start"}, start, end, {"type": "agent_end"},
                 {"type": "agent_start"}, start, end) == {"read": 2}


def test_native_new_session_warning_requires_host_bound_session_identity(tmp_path: Path) -> None:
    session = "mc-team-f7a577d9-4733-40fb-8921-74e3e0695c61"
    warning = f"Warning: No project session found with id '{session}'; creating a new session with that id."
    events = [
        {"type": "session", "id": session},
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolCallId": "1", "toolName": "bash"},
        {"type": "tool_execution_end", "toolCallId": "1", "toolName": "bash"},
        {"type": "agent_end"},
    ]
    raw = warning + "\n" + "\n".join(map(json.dumps, events))
    assert _parse(tmp_path, raw, session=session)["tool_counts"] == {"bash": 1}
    assert _parse(tmp_path, raw)["tool_counts"] is None
    assert _parse(tmp_path, raw, session="mc-team-different")["tool_counts"] is None
    assert _parse(tmp_path, raw.replace(warning, "Warning: arbitrary echo"), session=session)["tool_counts"] is None
    for extra in ("prompt echo", "{invalid json}", "[]", '{"type":"message_end"}'):
        assert _parse(tmp_path, raw.replace(warning + "\n", warning + "\n" + extra + "\n"), session=session)["tool_counts"] is None
    assert _parse(tmp_path, raw.replace('"id": "' + session, '"id": "wrong'), session=session)["tool_counts"] is None
    assert _parse(tmp_path, raw, kind="claude", session=session)["tool_counts"] is None
    reversed_raw = warning + "\n" + "\n".join(map(json.dumps, [events[0], events[1], events[3], events[2], events[4]]))
    assert _parse(tmp_path, reversed_raw, session=session)["tool_counts"] is None
    echo = {"type": "message_end", "message": {"content": [{"type": "text", "text": raw}]}}
    assert _parse(tmp_path, warning + "\n" + json.dumps(echo), session=session)["tool_counts"] is None


def test_transient_failures_are_retryable(tmp_path: Path) -> None:
    quota = _parse(tmp_path, "usage limit reached", rc=1)
    timeout = _parse(tmp_path, "", rc=124)

    assert quota["retryable"] is True
    assert quota["usage"]["unavailable_until"]
    assert timeout["retryable"] is True


def test_local_cli_option_error_with_echoed_quota_auth_labels_is_not_a_provider_hold(
    tmp_path: Path,
) -> None:
    # A local CLI/setup failure echoes its operands (the prompt), which can
    # contain quota/auth words. That is not provider evidence: no hold.
    raw = (
        "error: unknown option '--quota'\n"
        "Usage: pi [options]\n"
        "operands: - check the usage limit, quota and invalid_grant labels\n"
    )
    result = _parse(tmp_path, raw, rc=1)
    assert result["usage"]["unavailable_until"] is None
    assert result["usage"]["availability_reason"] is None
    assert result["retryable"] is False  # deterministic setup failure


def test_structured_native_provider_quota_error_still_fences(tmp_path: Path) -> None:
    data = {
        "type": "turn_end",
        "message": {
            "content": [],
            "stopReason": "error",
            "errorMessage": "429: you have reached your monthly usage limit",
        },
    }
    result = _parse(tmp_path, json.dumps(data), rc=1)
    assert result["usage"]["availability_reason"] == "quota_exhausted"
    assert result["usage"]["unavailable_until"].startswith("9999-")
    assert result["usage_observed"] is False or result["usage"]["total_tokens"] >= 0


def test_structured_provider_tracebacks_preserve_genuine_holds(tmp_path: Path):
    for message, reason in [
        ("429 monthly usage limit\nTraceback (most recent call last)", "quota_exhausted"),
        ("invalid_grant\nTraceback (most recent call last)", "credential_repair_required"),
    ]:
        raw = json.dumps({"type": "turn_end", "message": {"stopReason": "error", "errorMessage": message}})
        result = _parse(tmp_path, raw)
        assert result["usage"]["availability_reason"] == reason
        assert not result["usage_observed"]


def test_local_error_header_overrides_json_echo_in_its_prompt(tmp_path: Path):
    echoed = json.dumps({"type": "turn_end", "message": {"stopReason": "error", "errorMessage": "quota invalid_grant"}})
    result = _parse(tmp_path, "Error: Unknown option: -\n" + echoed)
    assert result["usage"]["availability_reason"] is None
    assert result["usage"]["unavailable_until"] is None
    assert not result["usage_observed"]


def test_real_weekly_and_monthly_quota_messages_fence_the_account(tmp_path: Path):
    weekly = _parse(tmp_path, "You've hit your weekly limit · resets Oct 7, 11am (Europe/Copenhagen)")
    monthly = _parse(tmp_path, '429: you have reached your monthly usage limit')
    assert weekly['usage']['availability_reason'] == 'quota_exhausted'
    assert '10-07T11:00:00' in weekly['usage']['unavailable_until']
    assert monthly['usage']['unavailable_until'].startswith('9999-')
    assert monthly['usage']['availability_reason'] == 'quota_exhausted'


def test_task_failure_is_not_retryable(tmp_path: Path) -> None:
    assert _parse(tmp_path, "tests failed", rc=1)["retryable"] is False


def test_cli_error_exit_zero_and_cache_creation_accounting(tmp_path: Path) -> None:
    result = _parse(tmp_path, json.dumps({"is_error": True, "result": "usage limit reached",
                                        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 30,
                                                  "cache_read_input_tokens": 50, "output_tokens": 3}}),
                    rc=0, kind="claude")
    assert not result["success"]
    assert result["usage"]["input_tokens"] == 32
    assert result["usage"]["cache_read_tokens"] == 50
    assert result["usage"]["total_tokens"] == 85


def test_pi_usage_is_all_turns_not_only_final_reply(tmp_path: Path):
    turns = [dict(input=10, output=3, cacheRead=100, cacheWrite=5, totalTokens=118, cost={'total': .1}),
             dict(input=2, output=4, cacheRead=200, cacheWrite=0, totalTokens=206, cost={'total': .2})]
    raw = '\n'.join(json.dumps({'type': 'turn_end', 'message': {'role': 'assistant',
                   'content': [{'type': 'text', 'text': 'OK'}], 'usage': u}}) for u in turns)
    result = _parse(tmp_path, raw, rc=0)
    assert result['success']
    assert result['usage']['input_tokens'] == 17
    assert result['usage']['output_tokens'] == 7
    assert result['usage']['cache_read_tokens'] == 300
    assert result['usage']['total_tokens'] == 324
    assert abs(result['usage']['cost_usd'] - .3) < 1e-9


def test_permanent_auth_failure_pauses_account_without_retry(tmp_path: Path):
    result = _parse(tmp_path, '401 refresh_token_reused', rc=1)
    assert not result['retryable'] and not result['success']
    assert result['usage']['availability_reason'] == 'credential_repair_required'
    assert result['usage']['unavailable_until'].startswith('9999-')


def test_thinking_is_not_a_plan_or_public_verdict(tmp_path: Path):
    data = {'type':'turn_end','message':{'content':[{'type':'thinking','thinking':'```json bad plan```\nVERDICT: FAIL'}, {'type':'text','text':'VERDICT: PASS'}]}}
    result = _parse(tmp_path,json.dumps(data),rc=0)
    assert result['success'] and result['text'] == 'VERDICT: PASS'
    data['message']['content'] = [{'type':'thinking','thinking':'VERDICT: PASS'}]
    result = _parse(tmp_path,json.dumps(data),rc=0)
    assert not result['success'] and result['text'] == 'empty model reply'


def test_host_storage_failure_is_named_not_an_empty_model_reply(tmp_path: Path) -> None:
    # 2026-10-06: /tmp filled during a QA run; Node printed ENOSPC on stderr
    # (merged into the raw stream) and the JSON events ended without a reply.
    events = "\n".join([
        json.dumps({"type": "agent_start"}),
        "Error: ENOSPC: no space left on device, write",
        json.dumps({"type": "agent_end", "messages": []}),
    ])
    result = _parse(tmp_path, events, rc=1)
    assert result["text"].startswith("storage exhausted (ENOSPC")
    assert result["retryable"] is False  # same input on a full disk fails again
    assert result["usage"]["unavailable_until"] is None  # not a provider hold


def test_prompt_echo_of_enospc_in_json_is_not_a_storage_failure(tmp_path: Path) -> None:
    # Only stderr lines count: a task that discusses ENOSPC must not be misread.
    data = {"type": "turn_end", "message": {"content": [], "errorMessage": "",
            "stopReason": "stop"}, "prompt": "investigate ENOSPC on /tmp"}
    result = _parse(tmp_path, json.dumps(data), rc=1)
    assert result["text"] == "empty model reply"


def test_missing_cli_and_import_errors_are_setup_failures(tmp_path: Path) -> None:
    missing = _parse(tmp_path, "timeout: failed to run command 'pi': No such file or directory",
                     rc=127)
    module = _parse(tmp_path, "ModuleNotFoundError: No module named 'mc_pi_runtime'", rc=1)
    for result in (missing, module):
        assert result["text"].startswith("harness setup failed (")
        assert result["retryable"] is False


def test_provider_diagnostic_outranks_a_host_keyword_in_echoed_text(tmp_path: Path) -> None:
    # Independent QA 2026-10-06: a plain 429 plus a prompt echo mentioning ENOSPC lost
    # its quota hold; 503/timeout lost retryability; invalid_grant its repair hold.
    echo = "\nRequest prompt: investigate ENOSPC on the test fixture"
    quota = _parse(tmp_path, "HTTP 429 quota exceeded" + echo)
    assert quota["retryable"] is True and quota["usage"]["availability_reason"] == "quota_exhausted"
    auth = _parse(tmp_path, "invalid_grant refresh_token_reused" + echo)
    assert auth["usage"]["unavailable_until"].startswith("9999-")
    for transient in ("HTTP 503 overloaded", "connection timed out"):
        assert _parse(tmp_path, transient + echo)["retryable"] is True
    # Even a real host line does not erase genuine provider evidence beside it.
    both = _parse(tmp_path, "HTTP 503 overloaded\nError: ENOSPC: no space left on device")
    assert both["retryable"] is True


def test_tagged_host_lines_count_and_json_fragments_do_not(tmp_path: Path) -> None:
    assert _parse(tmp_path, "[node] Error: ENOSPC: no space left on device")["text"].startswith(
        "storage exhausted")
    assert _parse(tmp_path, "[stderr] ModuleNotFoundError: No module named 'x'")["text"].startswith(
        "harness setup failed")
    pretty = _parse(tmp_path, '{\n  "prompt": "ENOSPC on /tmp",\n  "x": 1\n}')
    assert not pretty["text"].startswith("storage exhausted")


def test_provider_evidence_on_the_host_line_itself_still_wins(tmp_path: Path) -> None:
    # Second independent review: one line carrying both provider and host words.
    same = _parse(tmp_path, "Error: HTTP 429 quota exceeded; scratch ENOSPC on /tmp")
    assert same["retryable"] is True and same["usage"]["availability_reason"] == "quota_exhausted"
    auth = _parse(tmp_path, "Error: invalid_grant while writing cache: ENOSPC")
    assert auth["usage"]["unavailable_until"].startswith("9999-")
    # Plain text cannot tell an echoed provider word from a real one; the ambiguity
    # resolves to the provider's semantics, exactly as before host classification.
    echoed = _parse(tmp_path, "Error: ENOSPC: no space left on device\nprompt: check the quota")
    assert echoed["retryable"] is True
    # The coreutils "timeout:" prefix of a missing CLI is not a timed-out request.
    missing = _parse(
        tmp_path, "timeout: failed to run command 'pi': No such file or directory", rc=127
    )
    assert missing["text"].startswith("harness setup failed") and missing["retryable"] is False


def test_json_prompt_echo_does_not_override_a_real_host_failure(tmp_path: Path) -> None:
    events = "\n".join([
        json.dumps({"type": "agent_start", "prompt": "check the quota and timed out jobs"}),
        "[node] Error: ENOSPC: no space left on device, write",
        json.dumps({"type": "agent_end", "messages": []}),
    ])
    assert _parse(tmp_path, events)["text"].startswith("storage exhausted")


def test_disk_quota_is_storage_not_a_provider_quota(tmp_path: Path) -> None:
    # Third independent review: EDQUOT text contains "quota" and was read as a
    # provider quota, posting a false one-hour hold and retrying a host failure.
    for raw in ("Error: EDQUOT: disk quota exceeded, write",
                "OSError: [Errno 122] Disk quota exceeded"):
        result = _parse(tmp_path, raw)
        assert result["text"].startswith("storage exhausted")
        assert result["retryable"] is False and result["usage"]["unavailable_until"] is None


def test_bare_json_array_line_is_not_a_cli_event(tmp_path: Path) -> None:
    assert _parse(tmp_path, '["a", "b"]')["success"] is False  # no crash


def test_repeated_disk_quota_lines_stay_storage(tmp_path: Path) -> None:
    raw = "Error: EDQUOT: disk quota exceeded, write\nOSError: [Errno 122] Disk quota exceeded"
    result = _parse(tmp_path, raw)
    assert result["text"].startswith("storage exhausted")
    assert result["usage"]["unavailable_until"] is None and result["retryable"] is False


def test_disk_quota_beside_a_real_transient_error_is_not_a_quota_hold(tmp_path: Path) -> None:
    # Fifth review (interrupted by a usage limit) checkpoint: 503/timeout kept their
    # retry, but the hold was read from raw text where "disk quota" means storage.
    for provider in ("HTTP 503 overloaded", "connection timed out"):
        result = _parse(tmp_path, provider + "\nError: EDQUOT: disk quota exceeded, write")
        assert result["retryable"] is True
        assert result["usage"]["availability_reason"] is None
    genuine = _parse(tmp_path, "HTTP 429 quota exceeded\nError: EDQUOT: disk quota exceeded")
    assert genuine["usage"]["availability_reason"] == "quota_exhausted"


def test_concurrency_cap_is_transient_not_a_quota_hold(tmp_path: Path) -> None:
    # neuralwatt 2026-10-07: this held the whole provider for an hour as "quota_exhausted".
    raw = ('429: {"type":"rate_limit_error","code":"concurrent_budget_exceeded","message":'
           '"Concurrent limit reached for zai-org/GLM-5.3-Flash: 5/5 slots in use. Wait for a request to complete."}')
    result = _parse(tmp_path, raw, rc=1)
    assert result["retryable"] is True
    assert result["usage"]["unavailable_until"] is None
    assert result["usage"]["availability_reason"] is None


def test_concurrency_vocabulary_never_erases_a_genuine_quota_hold(tmp_path: Path) -> None:
    # Independent QA 2026-10-07: wording alone, negated or with zero slots, is not a cap.
    for raw in (
        "HTTP 429 quota exceeded\nRequest prompt: investigate concurrent requests",
        "HTTP 429 usage limit reached; no concurrent requests are active",
        "HTTP 429 quota exceeded; 0 slots in use",
        "HTTP 429 quota exceeded; Concurrent limit reached: 5/5 slots in use",
        "HTTP 429 rate_limit_error code=session_limit_exceeded; Concurrent limit reached: 5/5 slots in use",
        "HTTP 429 session-limit reached; Concurrent limit reached: 5/5 slots in use",
        "HTTP 429 monthly-limit reached; Concurrent limit reached: 5/5 slots in use",
        "HTTP 429 monthly rate limit reached; Concurrent limit reached: 5/5 slots in use",
    ):
        result = _parse(tmp_path, raw, rc=1)
        assert result["usage"]["availability_reason"] == "quota_exhausted", raw
        assert result["usage"]["unavailable_until"], raw


def test_router_quota_threshold_retains_reported_reset_and_account_hold(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.api.model_ranking import RankingUsageEvent
    from app.services import mc_sidecar, model_ranking

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "metadata.json")
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        mc_sidecar.set_model_meta("omniroute-codex", model, write_enabled=False)
    error = ('omniroute-api API error (429): {"message":"[codex/gpt-6.1-sol] '
             'All codex accounts reached configured quota threshold (reset after 30m 49s)"}')
    for raw in (error, error + "\n", error + "\r\n", "\n \t" + error + "\r\n \t",
                json.dumps({"type": "turn_end", "message": {
                    "stopReason": "error", "errorMessage": error,
                }})):
        before = datetime.now(timezone.utc)
        result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6.1-sol")
        after = datetime.now(timezone.utc)
        until = datetime.fromisoformat(result["usage"]["unavailable_until"])
        assert before + timedelta(seconds=1849) <= until <= after + timedelta(seconds=1849)
        assert result["text"] == error
        assert not result["success"] and result["retryable"]
        assert not result["usage_observed"]
        event = RankingUsageEvent.model_validate(result["usage"])
        model_ranking.record_usage(event.provider, event.model, event.model_dump())
        for model in ("gpt-6.1-sol", "gpt-6-luna"):
            meta = mc_sidecar.get_model_meta(event.provider, model)
            assert meta["unavailable_until"] == until.isoformat()
            assert meta["availability_reason"] == "quota_exhausted"
            assert not model_ranking._availability(meta, event.provider)[0]
            assert meta["write_enabled"] is False


def test_router_short_quota_threshold_retains_reset_for_raw_and_native_errors(tmp_path):
    from datetime import datetime, timedelta, timezone

    error = ('429: {"message":"[codex/gpt-6-luna] '
             'All codex accounts reached configured quota threshold (reset after 44m 42s)"}')
    native = json.dumps({"type": "turn_end", "message": {
        "stopReason": "error", "errorMessage": error,
    }})
    for raw in (error, native):
        before = datetime.now(timezone.utc)
        result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6-luna")
        after = datetime.now(timezone.utc)
        until = datetime.fromisoformat(result["usage"]["unavailable_until"])
        assert before + timedelta(seconds=2682) <= until <= after + timedelta(seconds=2682)
        assert result["usage"]["availability_reason"] == "quota_exhausted"
        assert result["retryable"] is True


def test_router_hour_bearing_quota_threshold_retains_reported_reset_and_account_hold(
    tmp_path, monkeypatch,
):
    # 2026-10-22: the router reports sub-day codex quota resets in hours too
    # ("reset after 1h 37m 5s"); that was dropped and fell back to a one-hour hold.
    from datetime import datetime, timedelta, timezone

    from app.api.model_ranking import RankingUsageEvent
    from app.services import mc_sidecar, model_ranking

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "metadata.json")
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        mc_sidecar.set_model_meta("omniroute-codex", model, write_enabled=False)
    error = ('omniroute-api API error (429): {"message":"[codex/gpt-6.1-sol] '
             'All codex accounts reached configured quota threshold (reset after 1h 37m 5s)"}')
    for raw in (error, error + "\n", error + "\r\n",
                json.dumps({"type": "turn_end", "message": {
                    "stopReason": "error", "errorMessage": error,
                }})):
        before = datetime.now(timezone.utc)
        result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6.1-sol")
        after = datetime.now(timezone.utc)
        until = datetime.fromisoformat(result["usage"]["unavailable_until"])
        assert before + timedelta(seconds=5825) <= until <= after + timedelta(seconds=5825)
        assert result["usage"]["availability_reason"] == "quota_exhausted"
        assert result["text"] == error
        assert not result["success"] and result["retryable"]
        assert not result["usage_observed"]
    event = RankingUsageEvent.model_validate(result["usage"])
    model_ranking.record_usage(event.provider, event.model, event.model_dump())
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        meta = mc_sidecar.get_model_meta(event.provider, model)
        assert meta["unavailable_until"] == until.isoformat()
        assert meta["availability_reason"] == "quota_exhausted"
        assert not model_ranking._availability(meta, event.provider)[0]
        assert meta["write_enabled"] is False


def test_router_multi_day_quota_threshold_retains_reported_reset_and_account_hold(
    tmp_path, monkeypatch,
):
    # 2026-10-24: the router also reports multi-day codex quota resets
    # ("reset after 81h 59m 44s"); the old one-day cap dropped it and fell back to a
    # one-hour hold. The reported deadline must be preserved in full.
    from datetime import datetime, timedelta, timezone

    from app.api.model_ranking import RankingUsageEvent
    from app.services import mc_sidecar, model_ranking

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "metadata.json")
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        mc_sidecar.set_model_meta("omniroute-codex", model, write_enabled=False)
    duration = 81 * 3600 + 59 * 60 + 44  # 295184 seconds, the handoff diagnostic
    error = ('omniroute-api API error (429): {"message":"[codex/gpt-6.1-sol] '
             'All codex accounts reached configured quota threshold (reset after 81h 59m 44s)"}')
    native = json.dumps({"type": "turn_end", "message": {
        "stopReason": "error", "errorMessage": error,
    }})
    for raw in (error, native):
        before = datetime.now(timezone.utc)
        result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6.1-sol")
        after = datetime.now(timezone.utc)
        until = datetime.fromisoformat(result["usage"]["unavailable_until"])
        assert before + timedelta(seconds=duration) <= until <= after + timedelta(seconds=duration)
        assert result["usage"]["availability_reason"] == "quota_exhausted"
        assert result["text"] == error
        assert not result["success"] and result["retryable"]
        assert not result["usage_observed"]
    event = RankingUsageEvent.model_validate(result["usage"])
    model_ranking.record_usage(event.provider, event.model, event.model_dump())
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        meta = mc_sidecar.get_model_meta(event.provider, model)
        assert meta["unavailable_until"] == until.isoformat()
        assert meta["availability_reason"] == "quota_exhausted"
        assert not model_ranking._availability(meta, event.provider)[0]
        assert meta["write_enabled"] is False


def test_router_quota_threshold_does_not_shorten_ambiguous_holds(tmp_path):
    from datetime import datetime, timedelta, timezone

    message = ("[codex/gpt-6.1-sol] All codex accounts reached configured quota threshold "
               "(reset after 30m 49s)")
    prefix = "omniroute-api API error (429): "
    error = prefix + json.dumps({"message": message})
    cases = [
        (error, "other-provider", "gpt-6.1-sol"),
        (error, "omniroute-codex", "other-model"),
        (error + "\nHTTP 429 monthly usage limit", "omniroute-codex", "gpt-6.1-sol"),
        (error + "\nadditional diagnostic\n", "omniroute-codex", "gpt-6.1-sol"),
    ]
    for body in (
        json.dumps({"message": message, "detail": "weekly usage limit"}),
        '{"message":"weekly usage limit","message":' + json.dumps(message) + '}',
        json.dumps({"message": message.replace("30m 49s", "30m 99s")}),
        json.dumps({"message": message.replace("30m 49s", "9999m 49s")}),
        json.dumps({"message": message.replace("30m 49s", "0m 0s")}),
    ):
        cases.append((prefix + body, "omniroute-codex", "gpt-6.1-sol"))
    for raw, provider, model in cases:
        before = datetime.now(timezone.utc)
        result = _parse(tmp_path, raw, provider=provider, model=model)
        until = datetime.fromisoformat(result["usage"]["unavailable_until"])
        assert until >= before + timedelta(hours=1), raw
        assert result["usage"]["availability_reason"] == "quota_exhausted"


def test_router_cooldown_retains_reported_retry_time_not_hour_quota(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(seconds=22)).isoformat()
    body = {"message": "All credentials for model gpt-6.1-sol are cooling down",
            "type": "rate_limit_error", "code": "model_cooldown", "model": "gpt-6.1-sol",
            "reset_seconds": 22, "retry_after": until, "credentials_cooling": 1}
    error = "omniroute-api API error (429): " + json.dumps(body)
    raw = json.dumps({"type": "turn_end", "message": {"stopReason": "error", "errorMessage": error}})
    result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6.1-sol")
    assert result["success"] is False and result["retryable"] is True
    assert result["usage"]["unavailable_until"] == until
    assert result["usage"]["availability_reason"] == "router_cooldown"
    assert result["text"] == error  # whole original outcome retained


def test_router_cooldown_cannot_erase_quota_auth_or_other_provider(tmp_path: Path) -> None:
    body = {"message": "All credentials for model gpt-6.1-sol are cooling down",
            "type": "rate_limit_error", "code": "model_cooldown",
            "model": "gpt-6.1-sol", "reset_seconds": 22,
            "retry_after": "2026-10-12T14:57:01.288+00:00", "credentials_cooling": 1}
    for message, reason in (("weekly usage limit reached", "quota_exhausted"),
                            ("invalid_grant", "credential_repair_required")):
        error = "omniroute-api API error (429): " + json.dumps({**body, "message": message})
        result = _parse(tmp_path, error, provider="omniroute-codex", model="gpt-6.1-sol")
        assert result["usage"]["availability_reason"] == reason
        assert result["usage"]["unavailable_until"].startswith("9999-")
    error = "omniroute-api API error (429): " + json.dumps(body)
    assert _parse(tmp_path, error)["usage"]["availability_reason"] == "quota_exhausted"


def test_router_cooldown_diagnostic_boundary_and_accounting(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    from app.api.model_ranking import RankingUsageEvent
    from app.services import mc_sidecar, model_ranking

    until = (datetime.now(timezone.utc) + timedelta(seconds=22)).isoformat()
    body = {"message": "All credentials for model gpt-6.1-sol are cooling down",
            "type": "rate_limit_error", "code": "model_cooldown", "model": "gpt-6.1-sol",
            "reset_seconds": 22, "retry_after": until, "credentials_cooling": 1}
    patches = [{"message": value} for value in (
        None, 17, {"error": "invalid_api_key"}, "HTTP 401 Unauthorized: invalid_api_key",
        "cooling down", "All credentials for model other are cooling down",
        body["message"] + "; HTTP 401 Unauthorized: invalid_api_key",
    )] + [{"detail": "HTTP 401 Unauthorized: invalid_api_key"}, {"unexpected": None}, {}]
    cases = [(json.dumps({**body, **patch}), "quota_exhausted" if patch else "router_cooldown")
             for patch in patches]
    # Keep the raw duplicate members; dict construction would already erase the defect.
    for key, value in body.items():
        for first in (None, 17, "HTTP 401 Unauthorized: invalid_api_key", value):
            cases.append((json.dumps({**body, key: first})[:-1]
                          + "," + json.dumps(key) + ":" + json.dumps(value) + "}",
                          "quota_exhausted"))
            cases.append((json.dumps(body)[:-1]
                          + "," + json.dumps(key) + ":" + json.dumps(first) + "}",
                          "quota_exhausted"))
    for index, (encoded, reason) in enumerate(cases):
        monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / f"metadata-{index}.json")
        for model in ("gpt-6.1-sol", "gpt-6-luna"):
            mc_sidecar.set_model_meta("omniroute-codex", model, write_enabled=False)
        error = "omniroute-api API error (429): " + encoded
        raw = json.dumps({"type": "turn_end", "message": {
            "stopReason": "error", "errorMessage": error,
            "usage": {"input": 10, "output": 2, "cacheRead": 3, "totalTokens": 15},
        }})
        result = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6.1-sol")
        assert result["text"] == error and result["usage_observed"] is True
        assert result["success"] is False
        event = RankingUsageEvent.model_validate(result["usage"])
        model_ranking.record_usage(event.provider, event.model, event.model_dump())
        assert result["usage"]["availability_reason"] == reason, encoded
        assert result["usage"]["unavailable_until"]
        if reason == "router_cooldown":
            assert result["usage"]["unavailable_until"] == until
        sol = mc_sidecar.get_model_meta(event.provider, event.model)
        assert sol["observed_usage"]["calls"] == 1
        assert sol["observed_usage"]["total_tokens"] == 15
        for model in ("gpt-6.1-sol", "gpt-6-luna"):
            meta = mc_sidecar.get_model_meta(event.provider, model)
            assert meta["availability_reason"] == reason, encoded
            assert meta["unavailable_until"] == result["usage"]["unavailable_until"]
            assert meta["write_enabled"] is False


def test_invalid_router_cooldown_fields_keep_conservative_hold(tmp_path: Path) -> None:
    body = {"message": "All credentials for model gpt-6.1-sol are cooling down",
            "type": "rate_limit_error", "code": "model_cooldown",
            "model": "gpt-6.1-sol", "reset_seconds": 22,
            "retry_after": "2026-10-12T14:57:01.288+00:00", "credentials_cooling": 1}
    for patch in ({"retry_after": "not-a-date"}, {"retry_after": "2026-10-12T14:57:01"},
                  {"model": "other-model"}, {"code": "quota_exhausted"},
                  {"reset_seconds": True}, {"reset_seconds": -1}, {"reset_seconds": 86401},
                  {"credentials_cooling": 0}, {"type": "other_error"}):
        error = "omniroute-api API error (429): " + json.dumps({**body, **patch})
        result = _parse(tmp_path, error, provider="omniroute-codex", model="gpt-6.1-sol")
        assert result["usage"]["availability_reason"] != "router_cooldown", patch
        if patch != {"type": "other_error"}:  # without a rate/quota label, old behavior is unknown
            assert result["usage"]["unavailable_until"], patch
            assert result["usage"]["availability_reason"] == "quota_exhausted", patch
    # An appended genuine diagnostic is not just the scoped JSON cooldown.
    error = "omniroute-api API error (429): " + json.dumps(body) + "\nHTTP 429 monthly usage limit"
    assert _parse(tmp_path, error, provider="omniroute-codex", model="gpt-6.1-sol")["usage"]["unavailable_until"].startswith("9999-")


def test_an_unreachable_local_engine_is_held_briefly_and_a_cloud_one_is_not(tmp_path: Path) -> None:
    # 2026-10-26: metis' vLLM refused connections; with no hold, every draft went there.
    from datetime import datetime, timedelta, timezone
    raw = json.dumps({"type": "turn_end", "message": {
        "role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Connection error."}})
    usage = _parse(tmp_path, raw, provider="metis", model="qwen3.8")["usage"]
    assert usage["availability_reason"] == "endpoint_unreachable"
    until = datetime.fromisoformat(usage["unavailable_until"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=9) < until <= timedelta(minutes=10)
    cloud = _parse(tmp_path, raw, provider="omniroute-codex", model="gpt-6-luna")["usage"]
    assert (cloud["unavailable_until"], cloud["availability_reason"]) == (None, None)
