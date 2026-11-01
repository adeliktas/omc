#!/usr/bin/env python3
"""Fleet watchdog (owner request 2026-10-22): detect stalls cheaply, let Opus fix them.

Cron runs this every 10 minutes; the state file decides whether a check is due.
A healthy check widens the interval along STEPS. Findings invoke Claude Code
(Opus 5.5) headless to diagnose, fix and resume, then reset the interval.
Each finding gets MAX_TRIES Opus runs, then one every RETRY_AFTER. Only a run that finds
something no model can supply escalates (an ESCALATE line): the watchdog files it as an
owner decision with options, holds the finding while it is pending, and then has Opus carry
out the owner's choice (owner 2026-10-26: maximum autonomy; the owner only decides).

Outcome checks (no progress, one block reason across many tasks, mass pauses) stay
on regardless of symptom exemptions: on 2026-10-23 every symptom of a 17h stall was
exempted as a "wait" and the owner had to debug it by hand.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from omc_paths import HOME, OMC_HOME, etc_path, load_fleet_config, lock_path, log_path, state_dir  # noqa: E402

STATE = state_dir("mc-fleet-watchdog") / "state.json"
RUNS = state_dir("mc-fleet-watchdog") / "runs"
LOG = log_path("mc-fleet-watchdog")
POOL_LOG = log_path("mc-claude-runner")
BOARDS = Path(os.environ.get("RUNNER_BOARD_REPOS", str(etc_path("mc-team/board-repos.json"))))
STEPS = [10, 20, 30]  # minutes; the check spends no tokens, a 4h backoff only hid stalls
MAX_TRIES, OPUS_TIMEOUT, OPUS_PER_DAY, RUNAWAY = 2, 3600, 6, 2_000_000
REVIEW_EVERY, SHIP_TARGET = 86_400, 0.35  # daily outcome review; busy days shipped 17-23% (2026-10-22/24)
RETRY_AFTER, DECISION, ME = 6 * 3600, "major_decision", "fleet-watchdog"
# Owner 2026-10-27: the system must not stop when Opus runs out of usage. gpt-6.1-sol (Pi through
# OmniRoute's codex pool, ponytail ultra from Pi's own config) takes over with the same prompt
# until Opus's reset has passed; both read and update HANDOFF so each continues the other's work.
HANDOFF = Path(os.environ.get("WATCHDOG_HANDOFF", str(state_dir("mc-fleet-watchdog") / "HANDOFF.md")))
OPUS = [str(HOME / ".local/bin/claude"), "-p", "--model", "claude-opus-5-5", "--output-format", "json"]
SOL = [str(HOME / ".pi/agent/bin/pi"), "-p", "--approve", "--mode", "json", "--provider", "omniroute-api",
       "--model", "codex/gpt-6.1-sol", "--thinking", "high"]
INTAKE_STATE, BATCH_LOG = state_dir("omc-intake") / "state.json", log_path("omc-next-batch")
INTAKE_LOG = Path(os.environ.get("INTAKE_LOG", str(log_path("omc-intake"))))
SIDECAR, PONYTAIL_CONFIG = HOME / ".openclaw/openclaw.mc.json", HOME / ".config/ponytail/config.json"
GH_LOG = log_path("omc-github-responder")
# Paths the repair prompts name; an instance's agent notes file (fleet.json agent_memory) is optional.
_MEMORY = load_fleet_config().get("agent_memory")
PROMPT_PATHS = {
    "memory": f"- Read {_MEMORY} and its linked notes first. They carry owner policy: privacy, no Jetson changes, cost discipline, keep all running code in git.\n" if _MEMORY else "",
    "memory_ref": f"owner policy in\n{_MEMORY} and its linked notes" if _MEMORY else "owner policy",
    "logs": f"{POOL_LOG}, {GH_LOG}, {LOG}",
    "home": OMC_HOME,
    "lock": lock_path("mc-claude-runner"),
    "intake_log": INTAKE_LOG,
    "handoffs": state_dir("mc-fleet-watchdog") / "handoffs",
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UTC = timezone.utc
# The instance's own database container (an instance sets COMPOSE_PROJECT_NAME in data/.env).
DB = ["docker", "exec", f"{os.environ.get('COMPOSE_PROJECT_NAME', 'openclaw-mission-control')}-db-1", "psql", "-U", "postgres", "-d", "mission_control", "-At", "-F", "|", "-c"]
META_BOARD = load_fleet_config().get("meta_board", "")  # meta-debug tasks run here


def log(msg):
    with LOG.open("a") as f:
        f.write(f"{datetime.now(UTC).isoformat()} {msg}\n")


def ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=UTC)


def codex_exhausted() -> bool:
    """Quota waits are expected, not a stall: every usable codex account has no session left."""
    try:
        accounts = json.loads((state_dir("mc-fleet-watchdog") / "utilization.json").read_text()).get("codex_accounts") or {}
    except (OSError, ValueError):
        return False
    usable = [a for a in accounts.values() if (a.get("weekly") or 0) > 3]
    return not usable or all((a.get("session") or 0) <= 3 for a in usable)


def sql(query: str) -> list[list[str]]:
    """Read-only query; an unreachable database is reported by the API check instead."""
    try:
        out = subprocess.run(DB + [query], capture_output=True, text=True, timeout=60).stdout
    except Exception:  # noqa: BLE001
        return []
    return [line.split("|") for line in out.splitlines() if "|" in line]


def repeated_blocks(fixed_at: float) -> list[str]:
    """One block reason across >=3 tasks in 6h is a system cause. Per-task meta-debug cannot
    see it: 50 cycles died on 'cycle deadline exceeded' over 2026-10-22/23 unnoticed.
    A reason re-fires only with a block newer than the last Opus run: on 2026-10-24 four
    pre-fix blocks re-woke Opus 10 min after f1b33728 fixed them, and would have all 6h.
    The stage name is not the cause: on 2026-10-23 three tasks each blocked in another stage on
    'not retryable unchanged (interrupted: attempt time limit reached ...)', and none fired."""
    groups: dict[str, list] = {}
    for task, last, *reason in sql("select task_id, updated_at, left(block_reason, 140) from team_cycles "
                                   "where state = 'blocked' and updated_at > now() - interval '6 hours'"):
        text = "|".join(reason)
        key = re.sub(r"[0-9a-f-]{8,}|[0-9]+", "#", re.sub(r"^stage [^ :]+", "stage #", text))
        groups.setdefault(key, []).append((task, ts(last).timestamp(), text))
    return [f"{len({t for t, _, _ in rows})} tasks blocked in 6h with the same reason "
            f"(system cause, not task-specific): {min(r for _, _, r in rows)}"
            for rows in groups.values()
            if len({t for t, _, _ in rows}) >= 3 and max(at for _, at, _ in rows) > fixed_at]


def discarded_passes(fixed_at: float) -> list[str]:
    """A cycle whose last QA PASSED but that ended cancelled or blocked with nothing published
    threw verified work away. Cap stops did this to 8 PASSes over 2026-10-22/24, and no check
    saw it: a cancelled cycle has no block reason to group, so only the mass pause it caused
    3 days later woke Opus. Fires once per discard (newer than the last Opus run)."""
    rows = sql("select c.task_id, c.updated_at, c.state from team_cycles c "
               "where c.state in ('cancelled', 'blocked') and c.published_sha is null "
               "and c.updated_at > now() - interval '6 hours' "
               "and (select a.verdict from team_attempts a where a.cycle_id = c.id and a.role_kind = 'qa' "
               "order by a.created_at desc limit 1) = 'pass' "
               "and not exists (select 1 from team_cycles p where p.task_id = c.task_id "
               "and p.published_sha is not null and p.created_at > c.created_at)")
    return [f"task {task[:8]}: QA-passed cycle {state} without publication (verified work discarded)"
            for task, last, state in rows if ts(last).timestamp() > fixed_at]


def cap_cancels(fixed_at: float) -> list[str]:
    """A cycle cancelled by a cap stop (task blocked, cap comment within seconds) threw its paid
    dev/QA work away, and the cap raise then paid for a replay from PM. 60 cycles died so over
    2026-10-21/25 and no check saw it: cancelled cycles have no block reason to group, so only
    the 8h no-Done outcome woke Opus. Since the finish grace, only a cycle that overran it fires."""
    rows = sql("select c.task_id, c.updated_at from team_cycles c where c.state = 'cancelled' "
               "and c.updated_at > now() - interval '6 hours' and exists (select 1 from activity_events e "
               "where e.task_id = c.task_id and e.message like 'Task input/output cap reached%' "
               "and e.created_at between c.updated_at - interval '5 seconds' and c.updated_at + interval '5 seconds')")
    return [f"task {task[:8]}: cycle cancelled by a cap stop mid-cycle (paid work discarded)"
            for task, last, *_ in rows if ts(last).timestamp() > fixed_at]


def unclaimed_attempts() -> list[str]:
    """An attempt queued 6h+ in a live cycle is never handed out. reap() extends the deadline
    while work is queued, so the cycle does not block until its 24h ceiling: on 2026-10-24/25
    two PM drafts the claim path always skipped idled 24h behind 'iteration limit' waits."""
    if codex_exhausted():  # held frontier engines legitimately leave QA and leads queued
        return []
    rows = sql("select a.task_id, a.stage_key, a.provider || '/' || a.model from team_attempts a "
               "join team_cycles c on c.id = a.cycle_id "
               "where a.status = 'queued' and a.round = c.round "
               "and c.state in ('planning', 'developing', 'qa') "
               "and a.created_at < now() - interval '6 hours'")
    return [f"task {task[:8]}: stage {stage} queued over 6h without a claim ({engine})"
            for task, stage, engine in rows]


def outcomes() -> list[str]:
    """Daily outcome review per fleet board. Every stop on 2026-10-22/24 got a correct point fix,
    yet only 17-23% of ~90 cycles a day shipped (multi-worker cycles: 0 of 61) and nothing looked
    at the totals until the owner asked for a deep review by hand."""
    out = []
    for board in json.loads(BOARDS.read_text()) if BOARDS.exists() else []:
        window = (f"from team_cycles c where c.board_id = '{board}' and c.state in ('published', 'blocked', "
                  "'cancelled') and c.updated_at > now() - interval '24 hours'")
        rows = sql("select count(*), count(*) filter (where c.published_sha is not null), coalesce(sum((select "
                   "sum(coalesce(a.total_tokens, 0)) from team_attempts a where a.cycle_id = c.id)), 0) " + window)
        try:
            ended, shipped, tokens = (int(x) for x in rows[0])
        except (IndexError, ValueError):
            continue
        if ended < 10 or shipped / ended >= SHIP_TARGET:
            continue
        stops = sql("select count(*), left(regexp_replace(coalesce(c.block_reason, c.state), "
                    "'[0-9a-f]{7,}|[0-9]+', '#', 'g'), 90) k " + window + " and c.published_sha is null "
                    "group by k order by 1 desc limit 4")
        out.append(f"board {board[:8]}: only {shipped}/{ended} cycles shipped in 24h ({tokens / 1e6:.0f}M tokens); "
                   "top stops: " + "; ".join(f"{r[0]}x {'|'.join(r[1:])}" for r in stops))
    return out


def history() -> str:
    """What the last runs found and fixed: four point fixes on one board in a day (2026-10-25)
    never read as one structural cause because every run started blind."""
    cutoff = time.time() - 2 * 86_400
    lines = []
    for run in sorted(RUNS.glob("*.json"))[-8:]:
        try:
            if run.stat().st_mtime > cutoff:
                lines.append(f"- {run.name[:15]}: {str(json.loads(run.read_text()).get('result'))[-300:]}")
        except (OSError, ValueError):
            continue
    return "\n".join(lines) or "- none"


def last_done(board: str):
    """When a task on the board last reached Done (None when unknown)."""
    rows = sql(f"select max(updated_at), 1 from tasks where board_id = '{board}' and status = 'done'")
    try:
        return ts(rows[0][0]) if rows and rows[0][0] else None
    except ValueError:
        return None


def findings():
    out = []
    for name, url in (("backend", "http://127.0.0.1:8000/health"), ("frontend", "http://127.0.0.1:3000/")):
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception as exc:  # noqa: BLE001
            out.append(f"{name} unhealthy: {type(exc).__name__}")
    if shutil.disk_usage("/").used / shutil.disk_usage("/").total > 0.85:
        subprocess.run([sys.executable, str(HERE / "mc-disk-janitor.py")], capture_output=True, timeout=3600)
        u = shutil.disk_usage("/")
        if u.used / u.total > 0.90:
            out.append(f"disk {100 * u.used // u.total}% full after cleanup ({u.free // 2**30} GiB free); Postgres crashes when it fills")
    try:  # /health does not touch the database; a real query does
        runpy.run_path(str(HERE / "mc"))["request"]("GET", "/api/v1/boards?limit=1")
    except BaseException as exc:  # noqa: BLE001 - the CLI exits on HTTP errors
        out.append(f"API/database unavailable: {type(exc).__name__}: {str(exc)[:150]}")
        return sorted(set(out))
    if "started" not in subprocess.run(["rc-service", "mc-team-pool", "status"], capture_output=True, text=True).stdout:
        out.append("mc-team-pool service is not started")
    now = datetime.now(UTC)
    # Errors logged before the last Opus run ended predate its fix; recounting them paid
    # for a second, idle Opus run (2026-10-22). Run files are last written at run end.
    fixed_at = max((p.stat().st_mtime for p in RUNS.glob("*.json")), default=0)
    recent, fresh = [], []
    with POOL_LOG.open(errors="replace") as f:
        f.seek(max(0, POOL_LOG.stat().st_size - 2_000_000))
        for line in f:
            m = re.match(r"(\S+) (.*)", line)
            try:
                if m and now - ts(m[1]) < timedelta(minutes=45):
                    recent.append(m[2])
                    if ts(m[1]).timestamp() > fixed_at:
                        fresh.append(m[2])
            except ValueError:
                pass
    # An engine hold is a wait (the runner says so too). The drive loop's iteration limit
    # is also a wait (shared engine slots, a quota reset, a sibling's lease): the pool just
    # restarts the runner. Neither line is worth Opus; an attempt that is never claimed
    # surfaces through unclaimed_attempts() instead.
    benign = r"[\w.-]+/[\w.:-]+ is (?:disabled or inel|temporarily unav)|iteration limit"
    errors = [re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<id>", l)[:160] for l in fresh
              if re.search(r"could not proceed|discarded|Traceback|FATAL|failed", l)
              and "disposable database" not in l and not re.search(benign, l)]
    for line in {e for e in errors if errors.count(e) >= 5}:
        out.append(f"repeating pool error (>=5x in 45 min): {line}")
    out += repeated_blocks(fixed_at)
    out += discarded_passes(fixed_at)
    out += cap_cancels(fixed_at)
    out += unclaimed_attempts()
    mc =runpy.run_path(str(HERE / "mc"))["request"]
    starts = [l for l in recent if "starting team runner" in l]
    pending = mc("GET", "/api/v1/review/items?status=pending")
    deciding = {i.get("task_id") for i in pending if isinstance(i, dict) and i.get("type") == DECISION
                and i.get("submitted_by") == ME}  # a model escalated these: the owner's choice is due
    for board in {*(json.loads(BOARDS.read_text()) if BOARDS.exists() else []), META_BOARD} - {""}:
        rows = mc("GET", f"/api/v1/boards/{board}/tasks?limit=200")
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        ready = [t for t in rows if t.get("execution_mode") == "team" and t["status"] in ("inbox", "in_progress")
                 and not t.get("is_blocked") and not t.get("split_count")]
        if ready and not starts and now - max(ts(t["updated_at"]) for t in ready) > timedelta(minutes=45):
            out.append(f"board {board[:8]}: {len(ready)} runnable team tasks but no runner started for 45 min")
        for t in rows:
            # A cap is a spend decision (auto-extension is limited to 2 in code; owners
            # set the rest), so only uncapped or over-cap spend is a runaway.
            used, cap = t.get("tokens_used") or 0, t.get("max_tokens_budget")
            if used >= RUNAWAY and (not cap or used >= cap) and t["status"] not in ("paused", "done", "cancelled"):
                out.append(f"task {t['id'][:8]} runaway spend: {t['tokens_used']} tokens and still {t['status']}")
            if t["status"] == "review" and t.get("execution_mode") == "team" and now - ts(t["updated_at"]) > timedelta(hours=2):
                out.append(f"task {t['id'][:8]} stuck in review for over 2h: {t['title'][:80]}")
            if t["status"] != "blocked" or t.get("execution_mode") != "team":
                continue
            comments = mc("GET", f"/api/v1/boards/{board}/tasks/{t['id']}/comments?markers=true&limit=200")
            comments = comments.get("items", comments) if isinstance(comments, dict) else comments
            # The pool resumes once per cause, at most 4 (RESUME_MAX), and escalates a repeat;
            # still blocked after four resumes means escalation did not happen.
            if sum((c.get("message") or "").startswith("[auto-resume] cause=") for c in comments) >= 4:
                out.append(f"task {t['id'][:8]} blocked after 4 auto-resumes: {t['title'][:80]}")
        by_id = {t["id"]: t for t in rows}
        for t in rows:
            if t["status"] == "inbox" and any(by_id.get(d, {}).get("status") == "cancelled" for d in t.get("blocked_by_task_ids") or []):
                out.append(f"task {t['id'][:8]} waits on a cancelled dependency")
        # No task waits on the owner (2026-10-26): 7 paused tasks froze their dependents and leads
        # for 15h while this check called them a "standing decision backlog" and stayed healthy.
        # The pool handles a blocked task within 5 min, so blocked for 1h means it gave up. A pause
        # is the owner's own hand unless the pool paused it for meta-debug ([escalated]), which
        # gets 6h. Only a model-escalated owner decision is a legitimate wait.
        for t in rows:
            if t.get("execution_mode") != "team" or t["status"] not in ("paused", "blocked") or t["id"] in deciding:
                continue
            age = now - ts(t["updated_at"])
            if t["status"] == "paused":
                notes = mc("GET", f"/api/v1/boards/{board}/tasks/{t['id']}/comments?markers=true&limit=200")
                notes = notes.get("items", notes) if isinstance(notes, dict) else notes
                marks = [c.get("message") or "" for c in sorted(notes, key=lambda c: str(c.get("created_at") or ""))
                         if (c.get("message") or "").startswith("[")]
                if not (marks and marks[-1].startswith("[escalated]")) or age < timedelta(hours=6):
                    continue
            elif age < timedelta(hours=1):
                continue
            out.append(f"task {t['id']} {t['status']}, no automatic recovery left: {t['title'][:80]}")
        # Many pauses at once means a system cause (owner 2026-10-24).
        if sum(t["status"] == "paused" and t.get("execution_mode") == "team"
               and now - ts(t["updated_at"]) < timedelta(hours=12) for t in rows) >= 5:
            out.append(f"board {board[:8]}: five or more tasks paused by failure escalation within 12h (likely a system cause)")
        # Frozen work waits too. On 2026-10-25 the fleet board finished nothing for 13h while
        # every open task was paused (>12h old, so not a mass pause), dependency-blocked or a
        # split parent; `ready` was empty, this check stayed silent and the owner found it.
        # Work that waits only on a pending model-escalated decision (the task, then the split
        # parents and dependents that wait on nothing else) is the one legitimate wait; its
        # finding is already held. Counted here, it re-raised a decision's wait as a stall (10-29).
        owner = set(deciding)
        while more := {t["id"] for t in rows if t["id"] not in owner and (
                on := {c["id"] for c in rows if c.get("parent_task_id") == t["id"]}
                | {d for d in t.get("blocked_by_task_ids") or [] if d in by_id}) and on <= owner}:
            owner |= more
        waiting = [t for t in rows if t.get("execution_mode") == "team" and t["id"] not in owner
                   and t["status"] in ("inbox", "in_progress", "paused", "blocked")]
        if waiting and not codex_exhausted():
            done = last_done(board)
            # The stall starts when work began waiting, not at the last Done: the meta board,
            # idle since 10-24, fired 18 min after a new meta-debug task arrived (2026-10-26).
            since = done and max(done, min(ts(t["created_at"]) if t.get("created_at") else done for t in waiting))
            if since and now - since > timedelta(hours=8):
                out.append(f"board {board[:8]}: no task finished (published/Done) for over 8h while work is waiting")
    # An idle fleet with free engines is no success (owner 2026-10-28: 0% LLM use, both plans free,
    # nothing in progress, while every check here passed). Name what it waits on.
    for board in json.loads(BOARDS.read_text()) if BOARDS.exists() else []:
        rows = sql(f"select max(created_at), 1 from team_attempts where board_id = '{board}'")
        try:
            idle = now - ts(rows[0][0])
        except (IndexError, ValueError, TypeError):
            continue
        if idle > timedelta(hours=3) and not codex_exhausted():
            asked = sum(isinstance(i, dict) and i.get("type") == DECISION and i.get("task_board_id") == board for i in pending)
            try:
                last_ok = json.loads(INTAKE_STATE.read_text()).get("last_ok") or "never"
            except (OSError, ValueError):
                last_ok = "never"
            feed = (BATCH_LOG.read_text(errors="replace").strip().splitlines() or ["no log"])[-1][:100] if BATCH_LOG.exists() else "no log"
            out.append(f"board {board[:8]}: idle {int(idle.total_seconds() // 3600)}h with free engines "
                       f"({asked} owner decisions pending; next-batch: {feed}; last intake: {last_ok[:10]})")
    # The intake logs every pull step and a 10-min heartbeat, metadata only (owner 2026-10-28: "there cannot
    # be some unknown progress"). A held lock with a silent log is a hung run; a failed run is named.
    try:
        age = now - datetime.fromtimestamp(INTAKE_LOG.stat().st_mtime, UTC)
        last = (INTAKE_LOG.read_text(errors="replace").strip().splitlines() or [""])[-1].split(" ", 1)[-1]
        with open(INTAKE_STATE.with_suffix(".lock"), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                running = False
            except BlockingIOError:
                running = True
    except OSError:
        age, last, running = timedelta(0), "", False
    if running and age > timedelta(minutes=30):
        out.append(f"intake running, but its log has been silent for {int(age.total_seconds() // 60)}m (last: {last[:150]})")
    elif not running and age < timedelta(hours=12) and re.match(r"intake \d+ (did not finish|pulled, but evaluation failed)", last):
        out.append(f"intake failed: {last[:200]}")
    # Every agent runs ponytail at the configured level (owner 2026-10-28). The runner reports what each run
    # loaded; a ruleset that silently failed to load (no node, moved hooks) shows up here, not in a review.
    try:
        expected = "ponytail:" + json.loads(PONYTAIL_CONFIG.read_text())["defaultMode"]
        models = json.loads(SIDECAR.read_text()).get("models") or {}
    except (OSError, ValueError, KeyError):
        expected, models = None, {}
    for provider, entries in models.items() if expected and expected != "ponytail:off" else ():
        for model, meta in (entries or {}).items():
            seen = (meta or {}).get("observed_harness") if isinstance(meta, dict) else None
            if seen and now - ts(seen.get("at") or "1970-01-01T00:00:00+00:00") < timedelta(hours=24) \
                    and expected not in (seen.get("skills") or []):
                out.append(f"engine {provider}/{model} ran without {expected} (reported skills: {seen.get('skills') or 'none'})")
    gh_log = GH_LOG
    if gh_log.exists():
        tail = gh_log.read_text(errors="replace").splitlines()[-30:]
        if sum(" failed: " in l for l in tail) >= 5:
            out.append("GitHub responder: repeated failures in its recent log")
    return sorted(set(out))


PROMPT = """You are the autonomous Mission Control fleet watchdog on this host (owner-authorized, 2026-10-22).
The cheap health check found these problems that stop fleet progress:

{findings}

Diagnose the root cause and fix it so the fleet resumes on its own:
- First read {handoff} (shared with the other watchdog engine: Opus or gpt-6.1-sol, whichever has usage). Before you finish, update it: what you did, the current state, open threads and the next steps.
{memory}- Relevant logs: {logs}. The MC checkout is {home} and the API CLI is scripts/mc.
- Fix system bugs at the root in an isolated git worktree. Add a focused test, commit, fast-forward the checkout under {lock}, and deploy with scripts/mc-deploy.sh (backend|frontend) or `rc-service mc-team-pool restart` when the pool is idle.
- Then re-admit affected tasks through the normal API. Never delete data, reset usage counters, read protected Jira/customer data, or change the Jetson or SGLang.
- Look across tasks before fixing one: compare recent block reasons (`cd {home} && docker compose exec -T db psql -U postgres -d mission_control -c "select task_id, block_reason, updated_at from team_cycles where state='blocked' order by updated_at desc limit 60"`) and the escalation handoffs in {handoffs}. A shared cause beats N task-local fixes.
- Never make a finding go away by exempting or narrowing a watchdog check, unless you prove the check itself is wrong and say so. The outcome checks (no progress, repeated block reasons, mass pauses) must stay.
- Maximum autonomy (owner 2026-10-26): resolve everything a model can. Unstick paused or blocked tasks yourself: fix the cause, then re-admit, replan (append the pool's `## Re-scope (fleet)` note) or split; choose documented defaults where the task allows it.
- Escalate only what no model can supply: live infrastructure data, credentials, a product or policy decision, or a system design trade-off only the owner may choose. Leave the affected task blocked and make the LAST line of your reply exactly: ESCALATE: {{"title": "<the decision, one line>", "task_id": "<full task UUID or null>", "summary": "<what you found, what you tried, what exactly is missing>", "options": ["<2-4 concrete options; each says what the system will do once chosen, including system design changes where they apply>"]}}. The watchdog files it on /review, waits for the owner's choice and then has you carry it out. Never escalate what a model could still solve.
  Apply the owner's standing rules first (the notes above: keep functional fixes, drop assurance or safeguard gates that only block delivery). When one of them decides the question, carry it out yourself and record which rule decided it; escalate only what no standing rule answers.
- A "cycles shipped in 24h" finding is an outcome review, not one bug. Break the last week's cycles down (plan shape, stage, engine, stop reason, restarts vs first runs, tokens per shipped task), find what structurally keeps work from shipping, and change that design. Point fixes for the individual stops have already happened and were not enough.
- An "idle with free engines" finding means the fleet has no work, not that it is fine. Refill it: run `INTAKE_EVERY_HOURS=0 python3 scripts/omc-intake.py` (local Qwen pulls Jira/Confluence/Mattermost; its progress, metadata only, is in {intake_log}; read only that log and WORK's DONE-*, BLOCKED-* and evaluation-* files, never raw/, todos or WORK's logs), make sure each pending owner decision still blocks something, and fix real blockers. Never invent busywork.
  A pending owner decision explains only its own task, never an idle board: keep the rest moving. Re-queue `omc-needs-human` issues whose tasks failed only through system bugs fixed since (remove the label; next-batch plans them again), refill through the intake, and look for other stuck work on the board.
- Keep it minimal.

Recent watchdog runs (newest last). The same board needing repeated point fixes means a structural cause:
{history}

End with a 3-line summary: root cause, fix, current state (then the ESCALATE line, if any)."""

DECIDED = """You are the autonomous Mission Control fleet watchdog on this host (owner-authorized, 2026-10-22).
Earlier you escalated a decision no model could make. The owner has decided:

Decision: {title}
{summary}

Options offered:
{options}

Owner's choice: {choice} (review status: {status})

First read {handoff} and update it before you finish. Carry out exactly that choice, including any system design change it names. Follow {memory_ref}, fix code in an isolated git worktree with a
focused test, commit, fast-forward {home} under {lock} and
deploy with scripts/mc-deploy.sh, then re-admit affected tasks through the API (scripts/mc).
End with a 3-line summary: what was decided, what you changed, current state."""


def fingerprint(text: str) -> str:
    """Ignore drifting counts (tokens used) so MAX_TRIES holds for the same finding."""
    return hashlib.sha256(re.sub(r"\d+", "#", text).encode()).hexdigest()[:16]


ALERT = "MAJOR: fleet watchdog could not fix: "  # legacy items; settle_alerts clears them


def escalation(result: str) -> dict | None:
    """The run's last ESCALATE line: what no model can supply, as an owner decision with options."""
    lines = [l for l in (result or "").splitlines() if l.startswith("ESCALATE:")]
    try:
        e = json.loads(lines[-1][len("ESCALATE:"):]) if lines else None
    except ValueError:
        return None
    opts = e.get("options") if isinstance(e, dict) else None
    if not (isinstance(e, dict) and isinstance(e.get("title"), str) and e["title"].strip() and isinstance(opts, list)
            and 2 <= len(opts) <= 5 and all(isinstance(o, str) and o.strip() for o in opts)):
        return None
    return e


def file_decision(e: dict, found: list[str]) -> str | None:
    """One owner decision with selectable options (/review renders a numbered list as buttons)."""
    try:
        mc = runpy.run_path(str(HERE / "mc"))["request"]
        tid = e.get("task_id") if isinstance(e.get("task_id"), str) and UUID.fullmatch(e["task_id"]) else None
        return mc("POST", "/api/v1/review/items", json.dumps({
            "title": f"DECISION: {e['title'].strip()[:150]}", "type": DECISION, "submitted_by": ME, "task_id": tid,
            "summary": f"{str(e.get('summary') or '').strip()}\n\nFindings:\n" + "\n".join(f"- {f}" for f in found),
            "reason": "No model can supply what is missing. The system waits for your choice, then carries it out.",
            # Whole options: a 190-char cut dropped "and re-admits the task" from an owner's choice (10-29).
            "proposed_action": "\n".join(f"{n}. {o.strip()}" for n, o in enumerate(e["options"], 1))})).get("id")
    except BaseException as exc:  # noqa: BLE001 - the CLI exits on HTTP errors
        log(f"decision filing failed: {type(exc).__name__}: {str(exc)[:150]}")
        return None


def owner_choices(state: dict) -> list[dict]:
    """Escalated decisions the owner has made since (a supersede or settle is no choice)."""
    if not state.get("decisions"):
        return []
    try:
        mc = runpy.run_path(str(HERE / "mc"))["request"]
        items = {i["id"]: i for i in mc("GET", "/api/v1/review/items?include_snoozed=true") if isinstance(i, dict)}
    except BaseException as exc:  # noqa: BLE001
        log(f"decision lookup failed: {type(exc).__name__}: {str(exc)[:150]}")
        return []
    out = []
    for rid in list(state["decisions"]):
        item = items.get(rid)
        if item is not None and item.get("status") == "pending":
            continue
        note = (item or {}).get("reviewer_note") or ""
        if item is None or note.startswith("Superseded") or "no decision needed" in note:
            state["decisions"].pop(rid)  # gone or closed by the system: nothing to carry out
            continue
        out.append(item)
    return out


def _run(cmd: list[str], out: Path) -> int:
    with out.open("w") as f:
        try:
            return subprocess.run(cmd, cwd=HOME, stdout=f, stderr=subprocess.STDOUT, timeout=OPUS_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            return 124
        except OSError as exc:  # the harness is not installed (it is optional on an instance)
            f.write(f"{type(exc).__name__}: {exc}\n")
            return 127


def run_agent(prompt: str, key: str, state: dict) -> str:
    """Opus by default; gpt-6.1-sol while Opus is out of usage, with the identical prompt."""
    spec = importlib.util.spec_from_file_location("mc_agent_result", HERE / "mc-agent-result.py")
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    parse = parser.parse
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    until = state.get("opus_until")
    if not until or ts(until) <= datetime.now(UTC):
        if state.pop("opus_until", None):
            log("Opus usage is back: Opus takes over again from gpt-6.1-sol (HANDOFF.md)")
        out = RUNS / f"{stamp}-{key}.json"
        rc = _run(OPUS + [prompt], out)
        res = parse("claude", out.read_text(errors="replace"), "claude-code", "claude-opus-5-5", rc)
        reset = None if res.get("success") else (res.get("usage") or {}).get("unavailable_until")
        if not reset:
            log(f"Opus run done rc={rc} cost=${(res.get('usage') or {}).get('cost_usd', 0):.2f}: {res.get('text', '')[-400:]}")
            return res.get("text") or ""
        state["opus_until"] = reset
        log(f"Opus is out of usage until {reset}: gpt-6.1-sol takes over (HANDOFF.md)")
    if state.get("sol_until") and ts(state["sol_until"]) > datetime.now(UTC):
        log(f"gpt-6.1-sol is out of usage until {state['sol_until']}: findings wait for the first engine back")
        return ""
    out = RUNS / f"{stamp}-{key}-sol.ndjson"
    rc = _run(SOL + ["--session-dir", str(RUNS / "sol-sessions"), "--", prompt], out)
    res = parse("pi", out.read_text(errors="replace"), "omniroute-codex", "gpt-6.1-sol", rc)
    text = res.get("text") or ""
    # history() and the freshness check read the run's .json summary, whichever engine ran.
    (RUNS / f"{stamp}-{key}.json").write_text(json.dumps({"result": text, "engine": "gpt-6.1-sol",
                                                           "success": bool(res.get("success"))}))
    if not res.get("success") and (reset := (res.get("usage") or {}).get("unavailable_until")):
        state["sol_until"] = reset  # 2026-10-27: OmniRoute's codex pool hit its quota threshold too
        log(f"gpt-6.1-sol is out of usage until {reset} too: findings wait for the first engine back")
        return ""
    log(f"gpt-6.1-sol run {'done' if res.get('success') else 'failed'} rc={rc}: {text[-400:]}")
    return text


def engines_out(state: dict) -> str | None:
    """The earliest reset when both Opus and gpt-6.1-sol are out of usage, else None."""
    now = datetime.now(UTC)
    holds = [state.get(k) for k in ("opus_until", "sol_until")]
    if all(h and ts(h) > now for h in holds):
        return min(holds, key=ts)
    if state.get("sol_until") and ts(state["sol_until"]) <= now:
        state.pop("sol_until")
    return None


def _pending_alerts(mc) -> list[dict]:
    pending = mc("GET", "/api/v1/review/items?status=pending&limit=200")
    rows = pending.get("items", pending) if isinstance(pending, dict) else pending
    return [i for i in rows or [] if isinstance(i, dict) and (i.get("title") or "").startswith(ALERT)]


def settle_alerts(found: list[str]) -> None:
    """An alert whose finding cleared needs no decision (2026-10-25: eight stale copies piled up)."""
    try:
        mc = runpy.run_path(str(HERE / "mc"))["request"]
        live = {fingerprint(ALERT + f[:110]) for f in found}
        for i in _pending_alerts(mc):
            if fingerprint(i["title"]) not in live:
                mc("PATCH", f"/api/v1/review/items/{i['id']}", json.dumps({
                    "action": "resolve", "expected_status": "pending",
                    "reviewer_note": "Finding cleared; no decision needed."}))
    except BaseException as exc:  # noqa: BLE001 - the CLI exits on HTTP errors; settling must not stop the check
        log(f"alert settle failed: {type(exc).__name__}: {str(exc)[:150]}")


def main():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"step": 0, "next": 0, "tries": {}}
    if time.time() < state["next"] - 60:  # cron fires on the minute; "next" lands a second later
        return 0
    found = findings()
    review = outcomes() if time.time() - state.get("reviewed_at", 0) > REVIEW_EVERY else []
    for f in review:  # once a day, each with fresh tries
        state.get("tries", {}).pop(fingerprint(f), None)
    found += [f for f in review if f not in found]
    settle_alerts(found)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"], state["opus_today"] = today, 0
    waiting_for = engines_out(state)  # no engine has usage: nothing to spend tries on
    capped = state.get("opus_today", 0) >= OPUS_PER_DAY or bool(waiting_for)
    choices = [] if capped else owner_choices(state)
    if choices:  # the owner decided an escalation: carry it out before anything else
        item = choices[0]
        state["decisions"].pop(item["id"])
        state["opus_today"] = state.get("opus_today", 0) + 1
        state["step"], state["next"] = 0, time.time() + OPUS_TIMEOUT
        STATE.write_text(json.dumps(state))
        log(f"owner decided {item['id']}: {item.get('reviewer_note')}")
        run_agent(DECIDED.format(**PROMPT_PATHS, handoff=HANDOFF, title=item["title"], summary=item.get("summary") or "", status=item.get("status"),
                                options=item.get("proposed_action") or "", choice=item.get("reviewer_note") or ""),
                 f"decided-{item['id'][:8]}", state)
    elif not found:
        state["tries"], state["last"] = {}, {}  # a problem that disappeared and comes back gets fresh attempts
        state["step"] = min(state["step"] + 1, len(STEPS) - 1)
        log(f"healthy; next check in {STEPS[state['step']]} min")
    else:
        # Tries count per finding. One key over the whole set gave every finding fresh tries
        # whenever another one cleared: on 2026-10-24 two tasks waiting on a human merge went
        # to Opus three times while the set shrank from five findings to two.
        keys = {f: fingerprint(f) for f in found}
        legacy = state["tries"].get(fingerprint("\n".join(found)), 0)  # state from before per-finding tries
        state["tries"] = {k: state["tries"].get(k, legacy) for k in keys.values()}
        # A finding exhausted before run times were kept waits one RETRY_AFTER from now.
        state["last"] = {k: state.get("last", {}).get(k, time.time()) for k in keys.values()}
        waiting = {k for ks in state.get("decisions", {}).values() for k in ks}
        # No give-up: after MAX_TRIES a finding gets one run per RETRY_AFTER, and only a model's
        # ESCALATE line reaches the owner (a bare "could not fix" alert left the owner to debug).
        new = [f for f in found if not capped and keys[f] not in waiting and (
            state["tries"][keys[f]] < MAX_TRIES or time.time() - state["last"][keys[f]] >= RETRY_AFTER)]
        held = [f for f in found if f not in new]
        if held:
            state["step"] = min(state["step"] + 1, len(STEPS) - 1)  # keep looking while broken
            why = (f"Opus and gpt-6.1-sol out of usage until {waiting_for}" if waiting_for
                   else "daily Opus cap reached" if capped else "awaiting the owner's decision or the next retry")
            log(f"held ({why}): {held}")
        if new:
            stubborn = [f for f in new if state["tries"][keys[f]] >= MAX_TRIES]
            for f in new:
                state["tries"][keys[f]] += 1
                state["last"][keys[f]] = time.time()
            key = fingerprint("\n".join(new))
            state["opus_today"] = state.get("opus_today", 0) + 1
            log(f"findings {key} (Opus run {max(state['tries'][keys[f]] for f in new)}): {new}")
            if set(new) & set(review):
                state["reviewed_at"] = time.time()
            state["step"], state["next"] = 0, time.time() + OPUS_TIMEOUT  # persist the try before spending
            STATE.write_text(json.dumps(state))
            text = "\n".join(f"- {x}" for x in new) + (
                "\n\nEarlier watchdog runs did not clear these; find the structural cause now, or ESCALATE "
                "if no model can supply what is missing:\n" + "\n".join(f"- {x}" for x in stubborn) if stubborn else "")
            e = escalation(run_agent(PROMPT.format(**PROMPT_PATHS, findings=text, history=history(), handoff=HANDOFF), key, state))
            if e and (rid := file_decision(e, new)):
                # The decision holds the task findings it is about. A board outcome finding keeps its
                # normal retries, so one task's question never parks a whole idle board (10-29: 18h).
                state.setdefault("decisions", {})[rid] = [keys[f] for f in new if not f.startswith("board ")]
                log(f"escalated to the owner as decision {rid}: {e['title'][:120]}")
            state["step"] = 0
    state["next"] = time.time() + STEPS[state["step"]] * 60
    STATE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
