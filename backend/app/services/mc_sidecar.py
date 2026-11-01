"""T89 (2026-05-16) — MC-only metadata sidecar for openclaw.json.

The openclaw gateway has a strict zod schema for `openclaw.json` and
rejects any unrecognised key inside `models.providers.<name>.models[].*`.
MC was adding `iq_score`, `iq_detail`, `iq_evaluated_at`, `disabled`,
`disabled_reason`, `disabled_at` directly to those model entries (T76, T80),
which broke the gateway with `Unrecognized keys` errors.

Fix: keep openclaw.json strictly to what the gateway expects, and persist
MC-only metadata in a SIDECAR file `~/.openclaw/openclaw.mc.json`.

Sidecar shape:
{
  "models": {
    "<provider_name>": {
      "<model_id>": {
        "iq_score": int,
        "iq_detail": {test_id: points},
        "iq_evaluated_at": unix_seconds,
        "disabled": bool,
        "disabled_reason": str,
        "disabled_at": unix_seconds
      }
    }
  },
  "schema": "openclaw.mc.v1"
}

This file is owned entirely by MC. Gateway never reads it. Owner can
delete it freely — IQ scores will recompute on next eval, disabled state
will recompute on next benchmark cycle.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from app.core.logging import get_logger
from app.core.private_access import DEFAULT_OPENCLAW_HOME, unique_json_members

logger = get_logger(__name__)

_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", DEFAULT_OPENCLAW_HOME))
_SIDECAR_PATH = _OPENCLAW_HOME / "openclaw.mc.json"

# In-process reentrancy (threads). Cross-process serialization is provided by
# an advisory flock on a lock file next to the sidecar path.
_lock = threading.RLock()
_lock_fd: int | None = None
_lock_depth = 0

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


class SidecarCorruptError(RuntimeError):
    """Raised when a mutating read/modify/write finds unparsable sidecar JSON.

    Failing closed prevents a corrupt file from being silently replaced with
    an empty document, which would reset manual disabled flags, ranking/pins,
    and quota windows.
    """


@contextlib.contextmanager
def update_lock() -> Iterator[None]:
    """Serialize a full sidecar read/modify/write cycle across threads AND
    processes.

    Holds the in-process RLock for reentrancy and, on the outermost
    acquisition, an exclusive advisory flock on `<sidecar>.lock`. Nested
    acquisitions (e.g. record_usage -> set_model_meta -> save_sidecar) reuse
    the already-held file lock. Degrades to thread-only locking when fcntl is
    unavailable.
    """
    global _lock_fd, _lock_depth
    with _lock:
        if fcntl is not None:
            if _lock_depth == 0:
                lock_path = Path(_SIDECAR_PATH).with_suffix(".json.lock")
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                _lock_fd = fd
            _lock_depth += 1
        try:
            yield
        finally:
            if fcntl is not None:
                _lock_depth -= 1
                if _lock_depth == 0 and _lock_fd is not None:
                    fd, _lock_fd = _lock_fd, None
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)


def _empty() -> dict[str, Any]:
    return {"models": {}, "schema": "openclaw.mc.v1"}


def load_sidecar() -> dict[str, Any]:
    return _load_sidecar(strict=False)


def _load_sidecar(strict: bool) -> dict[str, Any]:
    """Read the sidecar. Read-only callers (strict=False) get an empty doc on
    corruption; mutating callers (strict=True) fail closed so they never
    persist an empty doc over real metadata.
    """
    try:  # only a missing file is empty; exists() is False for a symlink loop too
        d = json.loads(
            Path(_SIDECAR_PATH).read_text(), object_pairs_hook=unique_json_members
        )
        if not isinstance(d, dict):
            raise ValueError("sidecar root is not a JSON object")
        d.setdefault("models", {})
        d.setdefault("schema", "openclaw.mc.v1")
        return d
    except FileNotFoundError:
        return _empty()
    except Exception as exc:
        if strict:
            logger.error("mc_sidecar.read_failed_strict err=%s", exc)
            raise SidecarCorruptError(
                f"sidecar corrupt; refusing read-modify-write: {exc}"
            ) from exc
        logger.warning("mc_sidecar.read_failed err=%s", exc)
        return _empty()


def save_sidecar(data: dict[str, Any]) -> None:
    """Atomic write via a unique per-call temp file (concurrent writers never
    collide on a fixed .tmp name). Caller should hold update_lock() for a
    read/modify/write cycle; this call itself is thread-safe."""
    with update_lock():
        path = Path(_SIDECAR_PATH)
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2))
            os.replace(tmp_path, path)
            tmp_path = None
        except Exception as exc:
            logger.warning("mc_sidecar.write_failed err=%s", exc)
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)


def get_model_meta(
    provider: str, model_id: str, strict: bool = False
) -> dict[str, Any]:
    """Return the MC-only metadata for a (provider, model). Empty dict
    when nothing recorded yet; strict raises SidecarCorruptError when unreadable.
    """
    data = _load_sidecar(strict=strict)
    if not strict:
        return ((data.get("models") or {}).get(provider) or {}).get(model_id) or {}
    node: Any = data
    for key in ("models", provider, model_id):  # a corrupt level is not "unset"
        node = node.get(key, {})
        if not isinstance(node, dict):
            raise SidecarCorruptError(f"sidecar {key!r} is not an object")
    meta: dict[str, Any] = node
    return meta


def get_provider_meta(provider: str) -> dict[str, dict[str, Any]]:
    """All MC-only metadata for a provider, keyed by model_id."""
    return (load_sidecar().get("models") or {}).get(provider) or {}


def set_model_meta(provider: str, model_id: str, **updates: Any) -> None:
    """Patch the MC-only metadata for one (provider, model). Removes any
    key whose value is `None` (so callers can clear fields by passing
    None). Atomic.
    """
    with update_lock():
        data = _load_sidecar(strict=True)
        models = data.setdefault("models", {})
        prov = models.setdefault(provider, {})
        entry = prov.setdefault(model_id, {})
        for k, v in updates.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        # Prune empty entries so the file stays tidy.
        if not entry:
            prov.pop(model_id, None)
            if not prov:
                models.pop(provider, None)
        save_sidecar(data)


def clear_model_meta(provider: str, model_id: str) -> None:
    """Remove all MC-only metadata for one (provider, model)."""
    with update_lock():
        data = _load_sidecar(strict=True)
        prov = (data.get("models") or {}).get(provider) or {}
        if model_id in prov:
            prov.pop(model_id, None)
            save_sidecar(data)


def rename_provider(old_name: str, new_name: str) -> None:
    """Move all MC-only metadata when a provider is renamed."""
    with update_lock():
        data = _load_sidecar(strict=True)
        models = data.get("models") or {}
        if old_name in models:
            models[new_name] = models.pop(old_name)
            save_sidecar(data)


def merge_into_model_entry(provider: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `entry` with MC-only metadata merged in. Used by
    GET endpoints so the UI sees a unified shape without needing to know
    about the sidecar.
    """
    if not isinstance(entry, dict) or not entry.get("id"):
        return entry
    meta = get_model_meta(provider, entry["id"])
    if not meta:
        return entry
    merged = dict(entry)
    for k, v in meta.items():
        if k not in merged:
            merged[k] = v
    return merged
