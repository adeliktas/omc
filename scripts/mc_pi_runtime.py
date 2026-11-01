"""API-key-only Pi runtime: never load or copy an OAuth credential store."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # omc_paths when loaded by path
from omc_paths import HOME, lock_path  # noqa: E402

QWEN_SLOT = lock_path("mc-local-qwen-slot")


@contextlib.contextmanager
def qwen_slot(timeout: float, stopped=lambda: False):
    """One managed Jetson job across native workers and restricted local review.

    This host lock is not a backend slot or a guard for unrelated manual clients.
    Wait consumes the caller's existing deadline; cancellation never starts work.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("local Qwen capacity deadline")
    deadline = time.monotonic() + timeout
    fd = os.open(QWEN_SLOT, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        stat = os.fstat(fd)
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("local Qwen capacity lock refused")
        while True:
            if stopped() or time.monotonic() >= deadline:
                raise TimeoutError("local Qwen capacity cancelled or expired")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0 or stopped():
            raise TimeoutError("local Qwen capacity cancelled or expired")
        yield remaining
    finally:
        os.close(fd)


def compaction_settings(provider: str, model: str, source: Path) -> dict:
    """Resolved token controls only, shared by key-only and in-place native runtimes."""
    settings = json.loads(source.read_text()) if source.exists() else {}
    compaction = settings.get("compaction", {})
    override = compaction.get("modelOverrides", {}).get(f"{provider}/{model}", {})
    safe = {
        "enabled": compaction.get("enabled", True),
        "reserveTokens": override.get("reserveTokens", compaction.get("reserveTokens", 16384)),
        "keepRecentTokens": override.get("keepRecentTokens", compaction.get("keepRecentTokens", 20000)),
    }
    if type(safe["enabled"]) is not bool or any(
        type(safe[key]) is not int or not 0 <= safe[key] <= 2**53 - 1
        for key in ("reserveTokens", "keepRecentTokens")
    ):
        raise RuntimeError("invalid Pi compaction settings")
    return {"compaction": safe}


def key_runtime(
    provider: str,
    model: str,
    scratch: Path,
    source: Path = HOME / ".pi/agent/models.json",
    *, session_tag: str | None = None,
) -> Path:
    config = json.loads(source.read_text())["providers"].get(provider)
    if not isinstance(config, dict):
        raise RuntimeError("native API provider configuration missing")
    models = [m for m in config.get("models", []) if m.get("id") == model]
    if not models:
        raise RuntimeError("native API model not configured")
    settings = compaction_settings(provider, model, source.with_name("settings.json"))
    if provider == "omniroute-api" and session_tag is not None:
        # A correlation label, never account selection or eligibility authority.
        headers = {k: v for k, v in (config.get("headers") or {}).items()
                   if k.lower() != "x-omniroute-session-id"}
        config = {**config, "headers": {**headers, "X-OmniRoute-Session-Id": session_tag}}
    # Static API keys/model config are copied into attempt scratch; rotating OAuth
    # credentials are never copied. The empty auth file locks in scratch only.
    runtime = scratch / "api-runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "models.json").write_text(
        json.dumps({"providers": {provider: {**config, "models": models}}})
    )
    (runtime / "auth.json").write_text("{}")
    # Only resolved token controls: never inherit host packages, extensions or commands.
    (runtime / "settings.json").write_text(json.dumps(settings))
    for path in runtime.iterdir():
        path.chmod(0o600)
    return runtime
