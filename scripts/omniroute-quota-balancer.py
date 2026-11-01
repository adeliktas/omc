#!/usr/bin/env python3
"""Spread codex load by remaining quota and report engine utilization (owner, 2026-10-22).

OmniRoute picks codex accounts strictly by connection priority, which drains one
account after another. Every run re-ranks the codex connections by remaining
weekly quota, then session quota, so sol AND luna use the account with the most
headroom. It also writes state_dir("mc-fleet-watchdog")/utilization.json: 24h tokens
per engine, account quota windows and underutilized capacity.
"""
import json
import sqlite3
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import HOME, log_path, state_dir  # noqa: E402

REPORT = state_dir("mc-fleet-watchdog") / "utilization.json"
LOG = log_path("omniroute-quota-balancer")
SIDECAR = HOME / ".openclaw/openclaw.mc.json"
PI_MODELS = HOME / ".pi/agent/models.json"


def omniroute_db() -> str:
    """OmniRoute's SQLite file inside its compose volume, under this Docker daemon's root dir."""
    if os.environ.get("OMNIROUTE_DB"):
        return os.environ["OMNIROUTE_DB"]
    root = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"], capture_output=True, text=True).stdout.strip()
    project = os.environ.get("COMPOSE_PROJECT_NAME", "openclaw-mission-control")
    return f"{root}/volumes/{project}_omniroute_data/_data/storage.sqlite"
EXHAUSTED = 3  # percent remaining; such windows go last


def held_on_quota(models, now):
    """omniroute-codex models MC holds on quota for longer than one balancer interval."""
    return [m for m, meta in models.items() if meta.get("availability_reason") == "quota_exhausted"
            and datetime.fromisoformat(meta.get("unavailable_until") or "1970-01-01T00:00:00+00:00")
            > now + timedelta(minutes=15)]


def release_codex_hold(models, probe, release, now):
    """OmniRoute's "all codex accounts reached configured quota threshold" 429 names one reset for the
    pool, which can be hours after an account frees up (2026-10-27: held to 06:37Z, lukas reset 02:00Z),
    and MC keeps that hold. Probe each held model once per run; when all answer, lift the hold."""
    held = held_on_quota(models, now)
    if held and all(probe(m) for m in held):
        release(held[0], now)
        return held
    return []


def probe(model):
    api = json.loads(PI_MODELS.read_text())["providers"]["omniroute-api"]
    req = urllib.request.Request(api["baseUrl"] + "/chat/completions", headers={
        "Authorization": "Bearer " + api["apiKey"], "Content-Type": "application/json"},
        data=json.dumps({"model": f"codex/{model}", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "Reply OK"}]}).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status == 200
    except OSError:  # HTTPError (429) included
        return False


def release(model, now):
    # An already-passed reset clears the account-wide hold on every sibling (model_ranking.record_usage).
    subprocess.run(["scripts/mc", "post", "/api/v1/ranking/usage", json.dumps({
        "provider": "omniroute-codex", "model": model, "success": True, "source": "quota-balancer probe",
        "unavailable_until": now.isoformat(), "availability_reason": "quota_exhausted"})],
        check=True, capture_output=True, timeout=60)


def order(accounts):
    """accounts: {id: {"weekly": pct, "session": pct}} -> ids, most headroom first."""
    def key(item):
        w, s = item[1].get("weekly", 0), item[1].get("session", 0)
        return (min(w, s) <= EXHAUSTED, -w, -s)
    return [i for i, _ in sorted(accounts.items(), key=key)]


def main():
    db = sqlite3.connect(f"file:{omniroute_db()}?mode=ro", uri=True)
    names = dict(db.execute("select id, name from provider_connections where provider='codex' and is_active=1"))
    current = dict(db.execute("select id, priority from provider_connections where provider='codex' and is_active=1"))
    accounts = {i: {} for i in names}
    for cid, window, pct, reset in db.execute(
        """select q.connection_id, q.window_key, q.remaining_percentage, q.next_reset_at from quota_snapshots q
           join (select connection_id, window_key, max(created_at) m from quota_snapshots where provider='codex' group by 1,2) l
           on q.connection_id=l.connection_id and q.window_key=l.window_key and q.created_at=l.m"""):
        if cid in accounts:
            accounts[cid][window] = pct
            accounts[cid][f"{window}_reset"] = reset
    wanted = {cid: n for n, cid in enumerate(order(accounts), 1)}
    if wanted != current:
        updates = ";".join(f"u.run({n},'{cid}')" for cid, n in wanted.items())
        subprocess.run(["docker", "exec", "omniroute", "node", "-e",
                        "const db=require('better-sqlite3')('/app/data/storage.sqlite');"
                        "const u=db.prepare('UPDATE provider_connections SET priority=? WHERE id=?');" + updates],
                       check=True, capture_output=True, timeout=60)
        with LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} codex priority -> "
                    + ", ".join(f"{names[c]}={n} (weekly {accounts[c].get('weekly')}%, session {accounts[c].get('session')}%)"
                                for c, n in sorted(wanted.items(), key=lambda x: x[1])) + "\n")
    usage = [dict(provider=p, model=m, calls=c, tokens=t, busy_seconds=round((d or 0) / 1000))
             for p, m, c, t, d in db.execute(
                 """select provider, model, count(*), sum(coalesce(tokens_in,0)+coalesce(tokens_out,0)), sum(duration)
                    from call_logs where timestamp > datetime('now','-1 day') and model not in ('connection-test','model-sync')
                    group by 1,2 order by 4 desc""")]
    local = sum(u["busy_seconds"] for u in usage if u["model"].startswith("Qwen3.8-27B"))
    underused = [f"local Qwen busy {local * 100 // 86400}% of the last 24h"] if local < 0.5 * 86400 else []
    underused += [f"codex account {names[c]} has {a.get('weekly')}% weekly quota left"
                  for c, a in accounts.items() if (a.get("weekly") or 0) >= 40]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "codex_accounts": {names[c]: {**a, "priority": wanted[c]} for c, a in accounts.items()},
        "usage_24h": usage, "underutilized": underused,
        "all_codex_exhausted": all(min(a.get("weekly", 0), a.get("session", 0)) <= EXHAUSTED for a in accounts.values()),
    }, indent=1))
    now = datetime.now(timezone.utc)
    lifted = release_codex_hold(json.loads(SIDECAR.read_text())["models"].get("omniroute-codex", {}),
                                probe, release, now)
    if lifted:
        with LOG.open("a") as f:
            f.write(f"{now.isoformat()} codex answered a probe; lifted MC quota hold on {', '.join(lifted)}\n")


if __name__ == "__main__":
    main()
