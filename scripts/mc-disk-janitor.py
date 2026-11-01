#!/usr/bin/env python3
"""Reclaim disk the fleet leaks (owner request 2026-10-23: a full disk crashed Postgres).

Conservative by design. It prunes Docker build cache older than 24h and unused
anonymous volumes (per-attempt test databases), and removes attempt worktrees
older than 2 days, plus the runners' mc-*/pi-* temp dirs in /tmp older than 2 days
(/tmp is tmpfs: 8 GiB of leaked temp dirs left 3 GiB of RAM free on 2026-10-24). Images are kept (mc-deploy rollbacks), as are named volumes
and anything in use. Runs daily from cron and from the watchdog when the disk is
over 85% full.
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import HOME, OMC_HOME, etc_path, log_path  # noqa: E402

LOG = log_path("mc-disk-janitor")
WORK = HOME
TMP = Path("/tmp")
try:  # the MC checkout plus every board's target repo clone
    REPOS = [OMC_HOME, *(Path(p) for p in json.loads(etc_path("mc-team/board-repos.json").read_text()).values())]
except (OSError, ValueError):
    REPOS = [OMC_HOME]
MAX_AGE = 2 * 86400


def used_pct() -> float:
    u = shutil.disk_usage("/")
    return 100 * u.used / u.total


def main() -> None:
    before = shutil.disk_usage("/").free
    for cmd in (["docker", "builder", "prune", "-f", "--filter", "until=24h"],
                ["docker", "volume", "prune", "-f"]):  # anonymous, unused volumes only
        subprocess.run(cmd, capture_output=True, timeout=1800)
    cutoff, removed = time.time() - MAX_AGE, 0
    for d in [*WORK.glob("mc-team-work*"), *TMP.glob("mc-*"), *TMP.glob("pi-*")]:
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()
                removed += 1
        except OSError:
            pass
    for repo in REPOS:
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, timeout=120)
    freed = (shutil.disk_usage("/").free - before) / 2**30
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} freed {freed:.1f} GiB, removed {removed} old worktrees/temp dirs, "
                f"disk now {used_pct():.0f}% used\n")


if __name__ == "__main__":
    main()
