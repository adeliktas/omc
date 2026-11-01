"""Query and restart one OMC user service, whichever init supervises it.

The fleet scripts assumed OpenRC: on a systemd host `rc-service` does not exist, so the
watchdog's health check died with FileNotFoundError (2026-11-01) and never reported or
repaired the pool. Keep the init choice in this one helper.
"""

from __future__ import annotations

import os
import subprocess


def _systemd() -> bool:
    return os.path.isdir("/run/systemd/system")


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        return subprocess.run(args, capture_output=True, text=True, env=env)
    except OSError:  # no such init command on this host
        return None


def running(name: str) -> bool:
    """True when the user service `name` is up."""
    if _systemd():
        proc = _run(["systemctl", "--user", "is-active", "--quiet", name])
        return bool(proc and proc.returncode == 0)
    proc = _run(["rc-service", "-U", name, "status"])
    return bool(proc and "started" in (proc.stdout or ""))


def restart(name: str) -> bool:
    """Restart the user service `name`; False when the init refused or is missing."""
    if _systemd():
        proc = _run(["systemctl", "--user", "restart", name])
    else:
        proc = _run(["rc-service", "-U", name, "restart"])
    return bool(proc and proc.returncode == 0)
