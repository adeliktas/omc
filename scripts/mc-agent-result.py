#!/usr/bin/env python3
"""Normalize Claude/Pi print-mode output for the unattended runner."""

from __future__ import annotations

import json
import re
from collections import Counter
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") != "thinking" and item.get("text")
    )


# Local CLI/setup failures (unknown options, usage output, tracebacks, a
# missing CLI). Their text can echo prompt content — including quota/auth
# labels — so it is never provider-error evidence: no availability hold may
# be inferred from it. Genuine structured/native-provider limit and auth
# errors do not contain these signatures and still fence the account.
LOCAL_CLI_FAILURE = re.compile(
    r"unknown (?:option|command|flag)|missing required (?:argument|option)|"
    r"invalid (?:option|argument|value)|unexpected (?:argument|flag|option)|"
    r"unrecognized option|bad option|command not found|"
    r"(?:^|\n)\s*usage: |usage error|"
    r"traceback \(most recent call last\)|harness cli missing",
    re.I,
)


# Host failures reach the merged stderr while the CLI's JSON events end without a
# reply. Reported as "empty model reply" they read as model inadequacy (a full /tmp
# raised ENOSPC during QA, 2026-10-06), so they are named and never retried
# unchanged. Only a line shaped like a host diagnostic counts — an error token at the
# start of the line, optionally after a "[tag]" — so prompt echoes ("Request prompt:
# investigate ENOSPC") and pretty-printed JSON continuation lines cannot.
_HOST_DIAGNOSTIC = re.compile(
    r"^(?:\[[\w.-]+\]\s*)?"
    r"(?:[\w.]*(?:Error|Exception)|E[A-Z]{3,}|[Ee]rror|[Ff]atal|FATAL ERROR|timeout|node|bash|sh)"
    r"(?:\s*\[[^\]]*\])?:\s"
)
INFRA_FAILURES = (
    ("storage exhausted",
     re.compile(r"ENOSPC|EDQUOT|no space left on device|disk quota exceeded", re.I)),
    ("memory exhausted", re.compile(r"ENOMEM|cannot allocate memory|heap out of memory", re.I)),
    ("harness setup failed",
     re.compile(r"ModuleNotFoundError|failed to run command|exec format error", re.I)),
)
_PERMANENT_AUTH = re.compile(r"refresh_token_reused|invalid_grant|credential store read failed", re.I)
# Actual concurrency-cap evidence only (QA: vocabulary alone must not erase a quota hold).
_CONCURRENCY_CAP = (r"concurrent_budget_exceeded|concurren(?:t|cy) limit reached|"
                    r"\b(?P<used>[1-9]\d*)/(?P=used) slots? in use")
_CONCURRENCY = re.compile(_CONCURRENCY_CAP, re.I)
# Any genuine limit wording keeps the hold even beside cap evidence (space/hyphen/underscore spellings).
_GENUINE_LIMIT = re.compile(
    r"quota|usage[\s_-]*limit|session[\s_-]*limit|(?:hourly|daily|weekly|monthly)[\s_-]*(?:rate[\s_-]*)?limit", re.I
)
_TRANSIENT = re.compile(
    r"overloaded|capacity|temporarily unavailable|service unavailable|"
    + _CONCURRENCY_CAP + r"|"
    r"timed?\s*out|connection reset|http[^0-9]*5(?:02|03|04|29)",
    re.I,
)
# An on-prem engine whose server is down (2026-10-26: metis vLLM refused :8000 while codex
# was held, and 33 drafts failed in 10 min). A short hold is its health gate: the claim
# path skips it, and the first attempt after the window probes it again.
LOCAL_ENGINES = ("vllm", "metis", "strata")
_UNREACHABLE = re.compile(r"^connection error\.?$|ECONNREFUSED|connection refused", re.I)
UNREACHABLE_HOLD = timedelta(minutes=10)


def _infra_failure(raw: str) -> tuple[str | None, str]:
    """Name a host failure, unless the output also holds provider evidence.

    Returns (host failure or None, the non-JSON output with every host phrase removed).
    The second value is what provider evidence must be read from.

    Plain text cannot tell a genuine provider error from an echoed prompt, so any
    quota/auth/transient evidence in non-JSON output — on another line or on the
    host line itself — keeps the provider's hold/retry semantics, as before this
    classification existed. JSON event lines are skipped: they echo the prompt, and
    a structured provider error never reaches this path. Only the host line's own
    diagnostic token is ignored: the coreutils "timeout:" prefix of a missing CLI
    is not a timed-out request.
    """
    found, lines = None, []
    for line in raw.split("\n"):
        try:  # JSON events echo the prompt; real provider errors there are parsed
            if isinstance(json.loads(line), (dict, list)):  # structurally, not here
                continue
        except ValueError:
            pass
        token = _HOST_DIAGNOSTIC.match(line)
        if token:
            for label, pattern in INFRA_FAILURES:
                match = pattern.search(line)
                if match:
                    found = found or f"{label} ({match.group(0)})"
                    # Every host line loses its diagnostic token and host phrase:
                    # "disk quota exceeded" is storage, not a provider quota.
                    line = pattern.sub("", line[token.end():])
                    break
        lines.append(line)
    other = "\n".join(lines)
    if found and (_quota_reset(other) or _PERMANENT_AUTH.search(other) or _TRANSIENT.search(other)):
        return None, other
    return found, other


def _router_cooldown(text: str, model: str) -> str | None:
    """A reported router retry time is back-pressure, not an upstream quota reset."""
    match = re.fullmatch(r"omniroute-api API error \(429\):\s*(\{.*\})", text, re.S)
    if not match or _GENUINE_LIMIT.search(text):
        return None  # never erase a genuine quota beside cooldown evidence
    try:
        pairs = json.loads(match[1], object_pairs_hook=list)
        data = dict(pairs)
        if (len(pairs) != len(data)  # duplicate members are ambiguous, even if values match
                or set(data) != {"message", "type", "code", "model", "reset_seconds",
                          "retry_after", "credentials_cooling"}
                or data.get("message") != f"All credentials for model {model} are cooling down"
                or data.get("type") != "rate_limit_error" or data.get("code") != "model_cooldown"
                or data.get("model") != model
                or type(data.get("reset_seconds")) is not int
                or not 0 <= data["reset_seconds"] <= 86400
                or type(data.get("credentials_cooling")) is not int
                or data["credentials_cooling"] < 1):
            return None
        until = datetime.fromisoformat(data["retry_after"].replace("Z", "+00:00"))
        if until.tzinfo is not None and until <= datetime.now(timezone.utc) + timedelta(days=1):
            return until.isoformat()
    except (ValueError, KeyError, AttributeError, TypeError):
        pass  # malformed evidence keeps the existing conservative classification
    return None


def _router_quota_reset(text: str, model: str) -> str | None:
    """Keep the router's explicit account reset instead of guessing a one-hour hold."""
    match = re.fullmatch(r"(?:omniroute-api API error \(429\):|429:)\s*(\{.*\})", text.strip(), re.S)
    if not match:
        return None
    try:
        pairs = json.loads(match[1], object_pairs_hook=list)
        if len(pairs) != 1 or pairs[0][0] != "message" or not isinstance(pairs[0][1], str):
            return None  # extra/duplicate fields could contain stronger quota/auth evidence
        reset = re.fullmatch(
            r"\[codex/" + re.escape(model) + r"\] (?:All )?codex accounts reached configured "
            r"quota threshold \(reset after (?:(?P<hours>\d{1,2})h )?(?P<minutes>\d{1,4})m "
            r"(?P<seconds>\d{1,2})s\)", pairs[0][1],
        )
        if reset:
            hours, minutes, seconds = (
                int(reset["hours"] or 0), int(reset["minutes"]), int(reset["seconds"])
            )
            duration = hours * 3600 + minutes * 60 + seconds
            # A well-formed duration keeps every unit in its canonical range; that,
            # not a one-day cap, is the malformed guard. The router also reports
            # multi-day resets ("reset after 81h 59m 44s"), which must be preserved.
            if minutes < 60 and seconds < 60 and duration > 0:
                return (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
    except (ValueError, TypeError, IndexError):
        pass  # unknown/malformed diagnostics retain the conservative quota fallback
    return None


def _quota_reset(text: str) -> str | None:
    dated = re.search(r"resets?\s+([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", text, re.I)
    if dated:
        try:
            zone = ZoneInfo(dated[6])
            now = datetime.now(zone)
            hour = int(dated[3]) % 12 + (12 if dated[5].lower() == 'pm' else 0)
            reset = datetime.strptime(f'{dated[1]} {dated[2]} {now.year}', '%b %d %Y').replace(hour=hour, minute=int(dated[4] or 0), tzinfo=zone)
            if reset <= now: reset = reset.replace(year=now.year + 1)
            return reset.isoformat()
        except (ValueError, KeyError):
            return '9999-01-01T00:00:00+00:00'
    if re.search(r'(?:weekly|monthly)\s+(?:usage\s+)?limit', text, re.I):
        return '9999-01-01T00:00:00+00:00'  # no reset evidence: manual quota recheck, not guessed replay
    match = re.search(
        r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",  # Claude Code: "resets 5pm (Europe/Berlin)"
        text,
        re.IGNORECASE,
    )
    if match:
        hour, minute = int(match[1]), int(match[2] or 0)
        if match[3].lower() == "pm" and hour != 12:
            hour += 12
        if match[3].lower() == "am" and hour == 12:
            hour = 0
        try:
            zone = ZoneInfo(match[4])
        except Exception:
            zone = timezone.utc
        now = datetime.now(zone)
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=1)
        return reset.isoformat()
    # 2026-10-07: a concurrency cap ("Concurrent limit reached ... 5/5 slots in use") is
    # momentary back-pressure; the generic rate-limit fallback below held the whole
    # provider for an hour. It stays retryable via _TRANSIENT.
    if _CONCURRENCY.search(text) and not _GENUINE_LIMIT.search(text):
        return None
    if re.search(
        r"rate.?limit|usage.?limit|session limit|quota|http[^0-9]*429|hit your (?:usage )?limit|limit reached", text, re.I
    ):
        return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return None


def parse(kind: str, raw: str, provider: str, model: str, rc: int, expected_session: str | None = None) -> dict:
    """Normalize one harness run (Claude JSON or Pi NDJSON); also imported by the fleet watchdog."""
    text = ""
    usage: dict = {}
    cost = 0.0
    model_ok = True

    objects = []
    for line in raw.split("\n"):  # NDJSON uses LF; Unicode separators may occur inside JSON strings.
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):  # a bare JSON array/scalar line is not a CLI event
            objects.append(parsed)

    if kind == "pi":
        turns = [obj for obj in objects if obj.get("type") == "turn_end"]
        if turns:
            message = turns[-1].get("message") or {}
            text = _content_text(message.get("content"))
            turn_usages = [t.get("message", {}).get("usage") or {} for t in turns]
            usage = (
                {
                    k: sum(int(u.get(k, 0) or 0) for u in turn_usages)
                    for k in (
                        "input",
                        "output",
                        "cacheRead",
                        "cacheWrite",
                        "totalTokens",
                    )
                }
                if any(turn_usages)
                else {}
            )
            cost = sum(
                float((u.get("cost") or {}).get("total") or 0) for u in turn_usages
            )
            model_ok = message.get("stopReason") != "error" and not message.get(
                "errorMessage"
            )
            if not model_ok:
                text = str(message.get("errorMessage") or text)
        else:
            ends = [obj for obj in objects if obj.get("type") == "agent_end"]
            messages = (ends[-1].get("messages") or []) if ends else []
            assistants = [
                message for message in messages if message.get("role") == "assistant"
            ]
            if assistants:
                message = assistants[-1]
                text = _content_text(message.get("content"))
                usage = message.get("usage") or {}
                cost = float((usage.get("cost") or {}).get("total") or 0)
                model_ok = message.get("stopReason") != "error" and not message.get(
                    "errorMessage"
                )
                if not model_ok:
                    text = str(message.get("errorMessage") or text)
            else:
                model_ok = False
    elif objects:
        result = objects[-1]
        text = str(result.get("result") or "")
        usage = result.get("usage") or {}
        model_ok = not result.get("is_error", False)
        cost = float(result.get("total_cost_usd") or 0)

    success = rc == 0 and model_ok and bool(text.strip())
    infra, sanitized = (None, None) if success or (text.strip() and objects) else _infra_failure(raw)
    text = infra or text.strip() or ("empty model reply" if model_ok and objects else raw.strip())
    # When the raw output is the diagnostic, read provider evidence with host phrases
    # removed: "disk quota exceeded" beside a genuine 503 is not a provider quota.
    evidence = sanitized if sanitized is not None and not objects and not infra else text
    # A local CLI/setup failure is not provider evidence, even when its echoed
    # text mentions quota/auth labels: it must not fence the provider.
    # Native structured error events remain provider evidence even if their
    # diagnostic includes a traceback. A local parser can echo JSON prompts,
    # so an explicit non-JSON CLI-error header still overrides those objects.
    header = raw.lstrip().splitlines()[0] if raw.strip() else ""
    local_header = not header.startswith(("{", "[")) and bool(LOCAL_CLI_FAILURE.search(header))
    local_failure = not success and (
        bool(infra) or local_header or (not objects and bool(LOCAL_CLI_FAILURE.search(text)))
    )
    permanent_auth = not success and not local_failure and bool(_PERMANENT_AUTH.search(evidence))
    cooldown = (_router_cooldown(evidence, model)
                if provider == "omniroute-codex" and not (success or local_failure or permanent_auth)
                else None)
    router_reset = (_router_quota_reset(evidence, model)
                    if provider == "omniroute-codex" and not (success or local_failure or permanent_auth)
                    else None)
    unreachable = (provider in LOCAL_ENGINES and not (success or local_failure)
                   and bool(_UNREACHABLE.search(evidence.strip())))
    reset = (
        "9999-01-01T00:00:00+00:00"
        if permanent_auth
        else (datetime.now(timezone.utc) + UNREACHABLE_HOLD).isoformat()
        if unreachable
        else (cooldown or router_reset or _quota_reset(evidence)
              if not success and not local_failure else None)
    )
    retryable = not success and not permanent_auth and not infra and bool(
        reset or rc in (124, 137, 143) or _TRANSIENT.search(evidence)
    )
    tools = Counter()
    tool_activity = []
    pending, completed, active = set(), set(), False
    native_prefix = header.startswith("{")
    # Pi emits this exact stderr warning before the new session's JSON stream.
    # Trust it only when the host-supplied attempt identity matches the native frame.
    warning = f"Warning: No project session found with id '{expected_session}'; creating a new session with that id.\n"
    if expected_session and raw.startswith(warning):
        try:
            first = json.loads(raw[len(warning):].split("\n", 1)[0])
        except json.JSONDecodeError:
            first = {}
        if isinstance(first, dict) and first.get("type") == "session" and first.get("id") == expected_session:
            native_prefix = True
    if (kind == "pi" and not (local_failure or infra or local_header) and native_prefix
            and objects and objects[0].get("type") in ("session", "agent_start")):
        for obj in objects:
            event = obj.get("type")
            if event == "agent_start":
                pending.clear()
                completed.clear()
                active = True
            elif event == "agent_end":
                pending.clear()
                active = False
            if not active or not isinstance(obj.get("toolCallId"), str):
                continue
            name = obj.get("toolName")
            if name not in ("read", "bash", "edit", "write", "grep", "find", "ls"):
                continue
            key = (obj["toolCallId"], name)
            if event == "tool_execution_start":
                pending.add(key)
            elif event == "tool_execution_end" and key in pending and key not in completed:
                tools[name] += 1
                tool_activity.append({"tool": name, "is_error": (
                    obj["isError"] if type(obj.get("isError")) is bool else None
                )})
                pending.remove(key)
                completed.add(key)
    normalized = {
        "text": text,
        "success": success,
        "retryable": retryable,
        "effective_rc": 0 if success else (rc or 1),
        # False when the CLI printed no usage object: consumers must treat the
        # zeros below as "not reported", never as "used nothing".
        "usage_observed": bool(usage),
        # Only actual completed built-in tool events; never prompts, arguments or outputs.
        # CLI summaries without these events mean unknown, not zero calls.
        "tool_counts": dict(tools) or None,
        "tool_activity": tool_activity or None,
        "usage": {
            "provider": provider,
            "model": model,
            "input_tokens": int(usage.get("input_tokens", usage.get("input", 0)) or 0)
            + int(
                usage.get("cache_creation_input_tokens", usage.get("cacheWrite", 0))
                or 0
            ),
            "output_tokens": int(
                usage.get("output_tokens", usage.get("output", 0)) or 0
            ),
            "cache_read_tokens": int(
                usage.get("cache_read_input_tokens", usage.get("cacheRead", 0)) or 0
            ),
            "total_tokens": int(usage.get("totalTokens", 0) or 0),
            "cost_usd": cost,
            "source": f"{kind}_cli",
            "success": success,
            "unavailable_until": reset,
            "availability_reason": ("credential_repair_required" if permanent_auth else "router_cooldown"
                                    if cooldown else "endpoint_unreachable" if unreachable
                                    else "quota_exhausted" if reset else None),
        },
    }
    if not normalized["usage"]["total_tokens"]:
        normalized["usage"]["total_tokens"] = (
            normalized["usage"]["input_tokens"]
            + normalized["usage"]["output_tokens"]
            + normalized["usage"]["cache_read_tokens"]
        )
    return normalized


def main() -> None:
    kind, raw_path, provider, model, rc_raw = sys.argv[1:6]
    (expected_session,) = sys.argv[6:] or [None]
    raw = Path(raw_path).read_text(errors="replace")
    print(json.dumps(parse(kind, raw, provider, model, int(rc_raw), expected_session)))


if __name__ == "__main__":
    main()
