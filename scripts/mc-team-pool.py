#!/usr/bin/env python3
"""mc-team-pool — long-lived supervisor for native team-mode board tasks.

The legacy mc-claude-runner.sh runs one task per cron tick under the global
runner lock; that cannot use the machine's full capacity. This service is the
maximum-utilization half: it stays resident, iterates the paginated task list
and drives up to RUNNER_TEAM_TASKS team cycles at once, one mc-team-runner.py
child per task.

  MC_URL / MC_TOKEN, RUNNER_BOARD, RUNNER_LOG, RUNNER_WORKDIR  (as the runner)
  RUNNER_TEAM_TASKS       children at once, 1..32               (default 8)
  RUNNER_TEAM_PARALLEL    developer attempts per cycle, 1..16   (default 8)
  RUNNER_TEAM_POLL        seconds between board scans           (default 10)
  RUNNER_LOCK_DIR         per-task lifecycle locks              (default /run)
  RUNNER_POOL_LOCK        singleton supervisor lock  (lock_path("mc-team-pool"))

Locks, carefully named:
  lock_path("mc-team-pool")       singleton supervisor; NOT a worker or
                                  publication lock — a second pool instance
                                  just exits, it never gates workers.
  /run/mc-team-<task uuid>.lock   one runner per task for its whole lifecycle
                                  (taken nonblocking by each child; the pool
                                  only probes it before spawning).
  lock_path("mc-claude-runner")   legacy global lock, taken by team runners
                                  ONLY around publication/deployment.

Scheduling gates: only execution_mode="team" tasks in
inbox/in_progress whose lane this host owns; never a blocked task, never a task
with unfinished upstream dependencies, never a repeating task before its
interval (same cooldown rule as the legacy runner), and never a Review/Blocked/
Done task. Exhausted caps are CAS-blocked with a durable task comment before
spawning. The pool never reopens terminal states and never enables providers.

A missing or dirty deployment checkout fails safely: no new children are
spawned (already-running children keep their own publication guards).

Graceful stop on SIGTERM/SIGINT: stop scanning, terminate every child, wait,
then SIGKILL anything left — no worker is ever left unmonitored.
"""

from __future__ import annotations

import fcntl
import json
import re
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # shared budget policy must not dirty the checkout
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from omc_paths import etc_path, load_fleet_config, lock_path, log_path, state_dir  # noqa: E402

MC = str(HERE / "mc")
BOARD = os.environ.get("RUNNER_BOARD", "")
LOG = os.environ.get("RUNNER_LOG", str(log_path("mc-claude-runner")))
TASKS = max(1, min(32, int(os.environ.get("RUNNER_TEAM_TASKS", "8"))))
POLL = max(2, int(os.environ.get("RUNNER_TEAM_POLL", "10")))
LOCK_DIR = os.environ.get("RUNNER_LOCK_DIR", str(lock_path("mc-team-pool").parent))
POOL_LOCK = os.environ.get("RUNNER_POOL_LOCK", str(lock_path("mc-team-pool")))
PAGE = 50  # page size for the paginated task list

stop = threading.Event()


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} pool: {msg}\n")
    except OSError:
        print(msg, file=sys.stderr)


def request_stop(signum, _frame) -> None:
    stop.set()


def probe_task_lock(task_id: str) -> bool:
    """True if no local runner currently holds this task's lifecycle lock.

    Only a probe: the child takes the binding lock itself, so a race here just
    spawns a child that exits immediately.
    """
    Path(LOCK_DIR).mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(Path(LOCK_DIR) / f"mc-team-{task_id}.lock"), os.O_WRONLY | os.O_CREAT, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def repo_ready() -> bool:
    """A missing or dirty deployment checkout fails safely: spawn nothing."""
    try:
        subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        log(f"deployment checkout {REPO} is missing or not a git repo; idle")
        return False
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    if dirty:
        log("deployment checkout is dirty; not starting new team workers")
        return False
    return True


_down_at = -1e9


def fetch_rows(path: str) -> list[dict]:
    """Iterate an authorized paginated endpoint; tolerate a non-paginated body."""
    rows: list[dict] = []
    offset = 0
    while True:
        try:
            p = subprocess.run(
                [
                    MC,
                    "get",
                    f"{path}{'&' if '?' in path else '?'}limit={PAGE}&offset={offset}",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if p.returncode:
                global _down_at
                # mc exit 3 = the API is unreachable (a deploy restart): every fetch of this
                # pass fails alike, so log it once per poll, not once per board or task.
                if p.returncode != 3 or time.monotonic() - _down_at >= POLL:
                    log(f"board scan failed: {(p.stderr.strip().splitlines() or ['?'])[-1][:200]}")
                if p.returncode == 3:
                    _down_at = time.monotonic()
                return []
            data = json.loads(p.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            log(f"board scan failed: {exc}")
            return []
        batch = data if isinstance(data, list) else data.get("items", [])
        rows.extend(batch)
        if len(batch) < PAGE or isinstance(data, list):
            return rows
        offset += PAGE


def fetch_tasks() -> list[dict]:
    # Explicit '*' opts into this authenticated organization's ready boards.
    boards = fetch_rows("/api/v1/boards") if BOARD == "*" else [{"id": BOARD}]
    rows = []
    for board in boards:
        if BOARD == "*" and board.get("goal_confirmed") is not True:
            continue
        for task in fetch_rows(f"/api/v1/boards/{board['id']}/tasks"):
            if task.get("board_id") not in (None, board["id"]):
                continue
            rows.append({**task, "board_id": board["id"]})
    return rows


FLEET_BOARDS = os.environ.get("RUNNER_BOARD_REPOS", str(etc_path("mc-team/board-repos.json")))
AUTO_MAX, CAP_STEP, AUTO_EVERY = 2, 600_000, 300
# The deterministic backend stop emitted by team_cycle.py when an implementation attempt
# returns the exact assigned base with no changed files: replaying it cannot succeed.
NO_CHANGE_STOP = "implementation reported the exact assigned base with no changed files"
# Owner 2026-10-25: recovery resumes the stopped cycle in place (team_cycle._reopen keeps its
# plan, finished leaves and integration) once per distinct cause; the same cause again escalates
# to ONE meta-debug task per cause. Blind re-admits replayed whole cycles from PM: 111 of 200
# restarts discarded finished work and restarts took 74% of the week's I/O.
RESUME, RESUME_MAX, ALSO = "[auto-resume]", 4, "[meta-also]"
# Owner 2026-10-26: no fleet task waits on the owner while a replan can still help. Every task
# left paused had stopped on a big plan (6 cycles, 23M tokens each); a fresh plan told why splits
# it into child tasks (team_cycle._split). team_cycle.RESCOPE_MARK keeps the note off children.
RESCOPE = "## Re-scope (fleet)"
_auto_at = 0.0


def cause(reason: str) -> str:
    """A stop's cause, stable across counts, SHAs and ids."""
    return re.sub(r"[0-9a-f]{7,}|\d+", "#", reason or "stopped")[:80]


META_BOARD = os.environ.get("RUNNER_META_BOARD") or load_fleet_config().get("meta_board", "")  # board for [meta-debug] tasks
META, ESCALATED, RESUMED, SWITCH = "[meta-debug]", "[escalated]", "[meta-resumed]", "[meta-switch]"
META_LADDER = [None, ("omniroute-codex", "gpt-6.1-sol")]  # None = normal pool routing (luna/Qwen implement)
HANDOFFS = Path(os.environ.get("RUNNER_HANDOFFS", str(state_dir("mc-fleet-watchdog") / "handoffs")))
HANDOFF_MAX_BYTES = 9_000  # UTF-8, including headings, fences and omission notices


def _notes(base: str, tid: str) -> list[str]:
    # markers=true: the plain thread hides "[slug]" markers, i.e. every counter below.
    rows = sorted(fetch_rows(f"{base}/{tid}/comments?markers=true"), key=lambda c: str(c.get("created_at") or ""))
    return [c.get("message") or "" for c in rows]


def _excerpt(text: str, budget: int) -> str:
    """Keep both diagnosis (head) and conclusion (tail), without splitting UTF-8."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    notice = f"\n[shortened from {len(raw)} UTF-8 bytes]\n"
    remaining = budget - len(notice.encode("utf-8"))
    if remaining <= 0:
        return "[omitted]"
    head = (remaining * 2 + 2) // 3
    tail = remaining - head
    return (raw[:head].decode("utf-8", errors="ignore") + notice
            + (raw[-tail:].decode("utf-8", errors="ignore") if tail else ""))


def _report(label: str, body: str, budget: int | None = None) -> str:
    """Bound a complete Markdown section, not the serialized handoff."""
    body_budget = len(body.encode("utf-8")) if budget is None else budget
    while True:
        excerpt = _excerpt(body, body_budget)
        # Either fence character may occur in worker Markdown; use the cheaper safe fence.
        fences = [ch * max(3, 1 + max((len(s) for s in re.findall(ch + "+", excerpt)), default=0))
                  for ch in ("`", "~")]
        fence = min(fences, key=len)
        section = f"{label}\n\n{fence}\n{excerpt}\n{fence}\n\n"
        excess = 0 if budget is None else len(section.encode("utf-8")) - budget
        if excess <= 0:
            return section
        body_budget -= excess


def handoff(board: str, t: dict, why: str) -> str:
    """Bounded diagnostic summary, NOT certified worker evidence or a failure diagnosis."""
    def inline(value, limit=160):
        return _excerpt(str(value).replace("\n", " ").replace("\r", " "), limit).replace("\n", " ")

    text = (f"# Handoff: {inline(t['title'], 320)}\n\n"
            f"- Board `{inline(board)}`, task `{inline(t['id'])}`\n"
            f"- Why: {inline(why, 384)}\n- Tokens used: {inline(t.get('tokens_used'), 40)}\n\n")
    try:
        # team_cycle.view: newest cycles first, at most five. This is not all-time history.
        data = api("get", f"/api/v1/boards/{board}/tasks/{t['id']}/team", None)
        cycles = data["cycles"]
        if not isinstance(cycles, list):
            raise ValueError("invalid team view")
    except Exception:  # noqa: BLE001
        cycles = []
        text += "Team evidence unavailable: the team view could not be fetched or read.\n\n"
    else:
        text += ("Team view: newest cycles first; older history may not be returned.\n\n" if cycles
                 else "Team view returned no cycles.\n\n")

    records = []
    if cycles:
        text += "## Cycle outcomes\n\n"
    for view in cycles[:6]:
        c, attempts = view["cycle"], view.get("attempts") or []
        cid = inline(c["id"][:8])
        text += _report(f"### Cycle {cid}: {inline(c.get('state'), 40)} — block reason",
                        c.get("block_reason") or "(none reported)", 960)
        # _attempts sorts by (round, stage_key, attempt_no), NOT completion time.
        # Retry numbers are meaningful within a stage; rounds precede timestamps.
        ordered = sorted(attempts, key=lambda a: (
            a.get("round") or 0, a.get("finished_at") or a.get("started_at") or "",
            a.get("attempt_no") or 0, a.get("stage_key") or ""), reverse=True)
        primary, earlier, seen = [], [], set()
        for a in ordered:
            role = a.get("role_kind")
            # Failed planning is the task-level diagnosis for backlog tasks and
            # module-manager delegation failures; successful plans are routine.
            planning_failure = role == "pm" and a.get("status") in ("failed", "interrupted")
            if role not in ("developer", "specialist", "qa") and not planning_failure:
                continue
            if not (a.get("artifact") or a.get("error") or a.get("verdict")):
                continue
            key = (role, a.get("stage_key"))
            # A later QA execution error without a report must not displace the
            # latest actual QA diagnosis (e.g. FAIL followed by a transport retry).
            latest = key not in seen and (role != "qa" or bool(a.get("artifact")))
            if latest:
                seen.add(key)
            name = "Latest QA" if latest and role == "qa" else role
            label = (f"### Cycle {cid}: {name} report — {inline(a.get('stage_key'))}, "
                     f"round {a.get('round')}, attempt {a.get('attempt_no')}; "
                     f"execution={inline(a.get('status'), 40)}")
            if role == "qa":
                label += f"; QA verdict={inline(a.get('verdict') or 'not recorded', 40).upper()}"
            body = ((f"Error: {a['error']}\n\n" if a.get("error") else "")
                    + (a.get("artifact") or "Report unavailable in team view."))
            (primary if latest else earlier).append((label, body))
        # Always include a preview of the latest QA and each worker before older retries.
        primary.sort(key=lambda item: "Latest QA" not in item[0])
        records.append({
            "id": cid, "groups": [primary, earlier], "omitted": len(primary) + len(earlier),
            "shortened": 0, "metadata": attempts, "meta_omitted": len(attempts),
        })
    if len(cycles) > 6:
        text += f"[{len(cycles) - 6} older cycles omitted.]\n\n"
    if records:
        text += "## Diagnostic reports (newest cycle first)\n\n"

    def notices(reserve=False):
        return "".join(
            f"- Cycle {r['id']}: {r['omitted']} diagnostic report(s) omitted; "
            f"{r['omitted'] if reserve else r['shortened']} shortened; "
            f"{r['meta_omitted']} routine attempt summary(s) omitted.\n"
            for r in records)

    # Reserve the largest possible counters and the separating newline, not just zeros.
    remaining = HANDOFF_MAX_BYTES - len((text + "\n" + notices(reserve=True)).encode("utf-8"))
    for record in records:
        for group in record["groups"]:
            # A section needs its label, fence and a useful preview. If even that cannot
            # fit, omit it explicitly rather than silently dropping a worker's rationale.
            selected, sizes, limits = [], [], []
            spare = remaining
            for label, body in group:
                size = len(_report(label, body).encode("utf-8"))
                floor = min(size, len(label.encode("utf-8")) + 256)
                if floor <= spare:
                    selected.append((label, body))
                    sizes.append(size)
                    limits.append(floor)
                    spare -= floor
            if not selected:
                continue
            # Finish fitting reports first; share the rest among oversized reports.
            for i in sorted(range(len(sizes)), key=sizes.__getitem__):
                needed = sizes[i] - limits[i]
                if needed <= spare:
                    limits[i] += needed
                    spare -= needed
            hungry = [i for i in range(len(sizes)) if limits[i] < sizes[i]]
            for n, i in enumerate(hungry):
                extra = spare // (len(hungry) - n)
                limits[i] += extra
                spare -= extra
            for (label, body), size, limit in zip(selected, sizes, limits):
                section = _report(label, body, limit)
                text += section
                remaining -= len(section.encode("utf-8"))
                record["omitted"] -= 1
                record["shortened"] += size > limit

    # Routine engine/token metadata is lower priority than every cycle's diagnostics.
    for record in records:
        for a in record["metadata"]:
            io = (a.get("input_tokens") or 0) + (a.get("output_tokens") or 0)
            line = (f"- Cycle {record['id']}: {inline(a.get('stage_key'))} "
                    f"({inline(a.get('role_kind'), 40)}), round {a.get('round')} "
                    f"attempt {a.get('attempt_no')}, execution={inline(a.get('status'), 40)}")
            if a.get("role_kind") == "qa":
                line += f"; QA verdict={inline(a.get('verdict') or 'not recorded', 40).upper()}"
            line += (f" on {inline(a.get('provider'))}/{inline(a.get('model'))}, "
                     f"effort {inline(a.get('thinking_level'), 40)}, {io} tokens"
                     + (f": {inline(a['error'], 200)}" if a.get("error") else "") + "\n")
            if len(line.encode("utf-8")) <= remaining:
                text += line
                remaining -= len(line.encode("utf-8"))
                record["meta_omitted"] -= 1
    text += "\n" + notices() if records else ""
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    (HANDOFFS / f"{t['id'][:8]}-{int(time.time())}.md").write_text(text, encoding="utf-8")
    return text


def settle_reviews() -> None:
    """An item whose task moved on (re-admitted, running, Done, cancelled) needs no decision."""
    try:
        pending = api("get", "/api/v1/review/items?status=pending", None) or []
        for i in pending if isinstance(pending, list) else []:
            if i.get("task_status") and i["task_status"] not in ("blocked", "paused"):
                api("patch", f"/api/v1/review/items/{i['id']}", {
                    "action": "resolve", "expected_status": "pending",
                    "reviewer_note": f"Task is {i['task_status']} now; no decision needed."})
    except BaseException as exc:  # noqa: BLE001 - housekeeping must never stop the pool (mc exits on HTTP errors)
        log(f"review settle skipped: {exc}")


def _stops(base: str, tid: str) -> str:
    try:
        cycles = api("get", f"{base}/{tid}/team", None).get("cycles") or []
    except BaseException:  # noqa: BLE001 - the handoff already says when the team view is unavailable
        return "unavailable"
    return " · ".join((c["cycle"].get("block_reason") or "?")[:80] for c in cycles[:3]
                      if c.get("cycle", {}).get("state") == "blocked")


def rescope(board: str, t: dict, why: str) -> bool:
    """Replan instead of pausing, once per task: the note changes the task, so the next cycle
    starts from a fresh PM (no resume in place) that splits the work or verifies it exists."""
    desc = t.get("description") or ""
    if t["title"].startswith(META) or RESCOPE in desc:
        return False
    base, tid = f"/api/v1/boards/{board}/tasks", t["id"]
    cap, used = t.get("max_tokens_budget"), t.get("tokens_used") or 0
    if cap and cap < used + CAP_STEP:  # the API takes a cap change only on its own
        t = api("patch", f"{base}/{tid}", {"max_tokens_budget": used + CAP_STEP, "expected_status": t["status"],
                                           "expected_updated_at": t["updated_at"],
                                           "comment": f"[auto-rescope] token cap raised to {used + CAP_STEP} for the replan."})
    t = api("patch", f"{base}/{tid}", {
        "description": desc + f"\n\n{RESCOPE}\n\nThe previous plan stopped: {why}. Last stops: "
                       f"{_stops(base, tid)}.\nReplan from the current repository state; do not repeat "
                       "the stopped plan. Split the work into 2 or more smaller, independently shippable "
                       "workers where it divides: each becomes its own task and pull request. If the "
                       "requested behavior already exists, plan mode=verify workers instead of changes."
                       + (" For the reported leaf-integration conflict, preserve the complete conflict "
                          "diagnostic and paths; assign coupled shared test bootstrap/helpers to one "
                          "implementation owner rather than splitting that coupled work across workers."
                          if "leaf-integration conflict" in why.lower() else ""),
        "expected_status": t["status"], "expected_updated_at": t["updated_at"],
        "comment": f"[auto-rescope] {why}: replanning from scratch, split into smaller tasks."})
    api("patch", f"{base}/{tid}", {"status": "inbox", "expected_status": t["status"],
                                   "expected_updated_at": t["updated_at"]})
    log(f"rescoped task {tid}: {why}")
    return True


def _pause(base: str, t: dict, note: str) -> dict:
    return api("patch", f"{base}/{t['id']}", {"status": "paused", "expected_status": t["status"],
                                              "expected_updated_at": t["updated_at"], "comment": note})


def escalate(board: str, t: dict, notes: list[str], key: str = "") -> None:
    """A task stopping again for the same cause is paused and handed to the meta-debug task for
    that cause on the MC board (owner 2026-10-22/25); meta tasks switch models instead. What
    neither solves, the fleet watchdog takes over: only a model's ESCALATE reaches the owner
    (owner 2026-10-26: maximum autonomy), so the pool files no /review items itself."""
    base = f"/api/v1/boards/{board}/tasks"
    if t["title"].startswith(META):
        current = (t["harness_provider"], t["harness_model"]) if t.get("harness_model") else None
        rung = META_LADDER.index(current) + 1 if current in META_LADDER else len(META_LADDER)
        if rung < len(META_LADDER):
            nxt = META_LADDER[rung]
            md = handoff(board, t, f"two failures on {current or 'pool routing'}; switching to {nxt[0]}/{nxt[1]}")
            tid = t["id"]
            t = api("patch", f"{base}/{tid}", {
                "harness_provider": nxt[0], "harness_model": nxt[1], "expected_status": "blocked",
                "expected_updated_at": t["updated_at"],
                "description": (t.get("description") or "") + f"\n\n## Handoff to {nxt[1]} (previous: {current[1] if current else 'pool routing'})\n\n{md}",
                "comment": f"{SWITCH} implementation engine -> {nxt[0]}/{nxt[1]}; handoff written."})
            api("patch", f"{base}/{tid}", {"status": "inbox", "expected_status": "blocked",
                                           "expected_updated_at": t["updated_at"],
                                           "comment": f"{RESUME} model switch | re-admitted on {nxt[1]} with the handoff."})
            log(f"meta task {tid} switched to {nxt[1]}")
            return
        md = handoff(board, t, "meta-debug exhausted every model")
        _pause(base, t, f"{ESCALATED} meta-debug exhausted every model; the fleet watchdog takes it from here.")
        log(f"meta task {t['id']} exhausted; left to the fleet watchdog")
        return
    md = handoff(board, t, f"stopped again: {key}")
    why = f"{(t.get('tokens_used') or 0) / 1e6:.1f}M tokens used. Last stops: {_stops(base, t['id'])}"
    _pause(base, t, f"{ESCALATED} stopped again after resuming ({key}); paused so no more tokens are spent. {why}")
    if any(n.startswith(ESCALATED) for n in notes):  # already had its meta-debug: no loop
        log(f"task {t['id']} failed again after meta-debug; left to the fleet watchdog")
        return
    if not META_BOARD:  # no operations board configured: the paused task and its review item stand alone
        log(f"task {t['id']} escalated: paused; no meta_board configured for a meta-debug task")
        return
    # One meta-debug task per cause: one backlog parser bug had produced eight (2026-10-24).
    meta = next((m for m in fetch_rows(f"/api/v1/boards/{META_BOARD}/tasks")
                 if m.get("title", "").startswith(META) and m.get("status") not in ("done", "cancelled")
                 and f"Cause: `{key}`" in (m.get("description") or "")), None)
    if meta:
        api("post", f"/api/v1/boards/{META_BOARD}/tasks/{meta['id']}/comments",
            {"message": f"{ALSO} board={board} task={t['id']}: same cause; it resumes when this task is Done."})
        log(f"task {t['id']} escalated: paused, joined meta-debug {meta['id']} for its cause")
        return
    meta = api("post", f"/api/v1/boards/{META_BOARD}/tasks", {
        "title": f"{META} Why does '{t['title'][:80]}' keep failing?",
        "description": ("OWNER DECISION (fleet escalation policy 2026-10-22): investigate and fix autonomously.\n\n"
                        f"Task `{t['id']}` on board `{board}` failed three times and is paused so no more tokens are spent on it.\n"
                        "Find the Mission Control system cause (team runner, pool, backend team cycle, prompts, sandbox, "
                        "routing, evidence handling) and fix it in this repository with a focused regression test; "
                        "publication deploys the fix automatically. If the cause is not a system bug (the task needs a "
                        "product decision, live infrastructure or a clearer scope), change no code and state exactly what "
                        "the task needs in your final report. Do not attempt the original task's own work.\n\n"
                        f"Cause: `{key}`\n\nOriginal task: board={board} task={t['id']}\n\n## Handoff\n\n{md}"),
        "status": "inbox", "execution_mode": "team", "execution_lane": "harness",
        "max_tokens_budget": 800_000, "max_seconds_budget": 3600, "priority": "high"})
    log(f"task {t['id']} escalated: paused, meta-debug {meta['id']}")


def resume_after_meta() -> None:
    """A Done meta-debug task resumes each task it covers once."""
    if not META_BOARD:
        return
    base = f"/api/v1/boards/{META_BOARD}/tasks"
    for m in fetch_rows(f"{base}?status=done"):  # the unfiltered listing hides Done tasks
        if m.get("status") != "done" or not m["title"].startswith(META):
            continue
        for ref in _meta_refs(base, m):
            _resume_ref(m, ref)


def _meta_refs(base: str, m: dict) -> list[tuple[str, str]]:
    refs = re.findall(r"Original task: board=(\S+) task=(\S+)", m.get("description") or "")
    try:
        refs += [r for n in (_notes(base, m["id"]) if refs else []) if n.startswith(ALSO)
                 for r in re.findall(r"board=(\S+) task=([\w-]+)", n)]
    except Exception as exc:  # noqa: BLE001 - the original still resumes
        log(f"meta-also notes unavailable for {m['id']}: {exc}")
    return refs


def _resume_ref(m: dict, ref: tuple[str, str]) -> None:
    obase = f"/api/v1/boards/{ref[0]}/tasks"
    try:
        t = next((x for x in fetch_rows(obase) if x["id"] == ref[1]), None)
        if not t or t["status"] != "paused" or any(n.startswith(RESUMED) for n in _notes(obase, t["id"])):
            return
        back, oid = t.get("paused_from_status") or "inbox", t["id"]
        t = api("patch", f"{obase}/{oid}", {"status": back, "expected_status": "paused",
                                            "expected_updated_at": t["updated_at"],
                                            "comment": f"{RESUMED} meta-debug {m['id']} finished; one more attempt."})
        if t.get("status") == "blocked":
            api("patch", f"{obase}/{oid}", {"status": "inbox", "expected_status": "blocked",
                                            "expected_updated_at": t["updated_at"],
                                            "comment": "Re-admitted after the meta-debug fix."})
        log(f"task {ref[1]} resumed after meta-debug {m['id']}")
    except Exception as exc:  # noqa: BLE001
        log(f"meta resume skipped for {ref[1]}: {exc}")


def release_cancelled_dependencies(board: str) -> None:
    """A dependency on a cancelled task can never be satisfied; drop it so the dependent runs."""
    base = f"/api/v1/boards/{board}/tasks"
    cancelled = {t["id"] for t in fetch_rows(f"{base}?status=cancelled")}
    for t in fetch_rows(f"{base}?status=inbox"):
        deps = t.get("blocked_by_task_ids") or []
        if deps and set(deps) & cancelled:
            try:
                api("patch", f"{base}/{t['id']}", {"depends_on_task_ids": [d for d in deps if d not in cancelled],
                                                   "comment": "Dropped dependencies on cancelled tasks."})
                log(f"released task {t['id']} from cancelled dependencies")
            except Exception as exc:  # noqa: BLE001
                log(f"dependency release skipped {t['id']}: {exc}")


def cancel_duplicates(board: str) -> None:
    """An Inbox task whose GitHub issues are all covered by an older live or Done task
    never starts (duplicate PRs for one issue, #247)."""
    base = f"/api/v1/boards/{board}/tasks"
    tasks = [t for st in ("inbox", "in_progress", "review", "blocked", "paused", "done")
             for t in fetch_rows(f"{base}?status={st}")]
    issues = lambda t: {int(n) for line in re.findall(r"(?im)^GitHub issues?:.*$", t.get("description") or "")
                        for n in re.findall(r"#(\d+)", line)}
    by_id = {t["id"]: t for t in tasks}

    def root(t):  # a split's parts share their parent's issues and age; they are not duplicates (2026-10-26)
        while t.get("parent_task_id") in by_id:
            t = by_id[t["parent_task_id"]]
        return t
    age = lambda t: str(root(t).get("created_at") or "")
    for t in tasks:
        mine = issues(t)
        if t.get("status") != "inbox" or not mine:
            continue
        older = set().union(*[issues(o) for o in tasks if root(o) is not root(t) and age(o) < age(t)] or [set()])
        if mine <= older:
            try:
                api("patch", f"{base}/{t['id']}", {"status": "cancelled", "expected_status": "inbox",
                                                   "expected_updated_at": t["updated_at"],
                                                   "comment": f"Cancelled as a duplicate: issues {sorted(mine)} are already covered by older tasks."})
                log(f"cancelled duplicate task {t['id']} for issues {sorted(mine)}")
            except Exception as exc:  # noqa: BLE001
                log(f"duplicate cancel skipped {t['id']}: {exc}")


def recover_blocked() -> None:
    """Fleet boards heal themselves (owner policy 2026-10-22/25): a blocked team task resumes its
    stopped cycle in place once per distinct cause; a cap stop first gets +CAP_STEP tokens (at
    most AUTO_MAX times). The same cause again escalates (see escalate)."""
    global _auto_at
    if time.monotonic() - _auto_at < AUTO_EVERY:
        return
    _auto_at = time.monotonic()
    accept_published()
    try:
        boards = list(json.loads(Path(FLEET_BOARDS).read_text()))
    except (OSError, ValueError):
        return
    resume_after_meta()
    settle_reviews()
    for board in boards:
        cancel_duplicates(board)
        release_cancelled_dependencies(board)
        settle_parents(board)
    for board in dict.fromkeys(boards + ([META_BOARD] if META_BOARD else [])):
        base = f"/api/v1/boards/{board}/tasks"
        for t in fetch_rows(base):
            if t.get("status") != "blocked" or t.get("execution_mode") != "team":
                continue
            tid = t["id"]
            try:
                notes = _notes(base, tid)
                # A model switch (meta ladder) is a changed input: its causes start afresh.
                since = notes[max((k for k, n in enumerate(notes) if n.startswith(SWITCH)), default=-1) + 1:]
                caps = sum(n.startswith("[auto-recovery:cap]") for n in notes)
                cap, used = t.get("max_tokens_budget"), t.get("tokens_used") or 0
                cycles = api("get", f"{base}/{tid}/team", None).get("cycles") or []
                latest = cycles[0].get("cycle", {}) if cycles else {}
                if latest.get("state") == "published" or (
                        not any("published as pull request" in n for n in notes)
                        and any("is committed locally, but opening the PR failed" in n for n in notes)):
                    continue  # a rejected PR needs publication repair, not another implementation cycle
                reason = latest.get("block_reason") or ""
                capped = bool(cap and used >= cap)
                if latest.get("state") == "blocked" and "leaf-integration conflict" in reason.lower():
                    # An in-place reopen retains the conflicting leaf/integration state. Replan
                    # once, carrying the unabridged paths; never replay that unchanged merge.
                    if not rescope(board, t, reason):
                        escalate(board, t, notes, cause(reason))
                    continue
                if latest.get("state") == "blocked" and "not retryable unchanged" in reason:
                    rescope(board, t, cause(reason))  # once; then it stays blocked for the fleet watchdog
                    continue
                if latest.get("state") == "blocked" and NO_CHANGE_STOP in reason and t.get("title", "").startswith(META):
                    # Its brief allows "no system defect: change no code", so that report is the
                    # result, not a stop: resume what it covers once and close it (15 of 31
                    # no-change stops in a week were meta tasks, each left blocked with its original paused).
                    for ref in _meta_refs(base, t):
                        _resume_ref(t, ref)
                    api("patch", f"{base}/{tid}", {
                        "status": "cancelled", "expected_status": "blocked", "expected_updated_at": t["updated_at"],
                        "comment": "No system defect found (no code change); covered tasks resumed once."})
                    continue
                if latest.get("state") == "blocked" and NO_CHANGE_STOP in reason:
                    # A no-change implementation stop is deterministic: resuming replays
                    # work that already returned the exact base. Replan once, then the watchdog.
                    rescope(board, t, "the implementation changed nothing")
                    continue
                key = cause(reason)
                tried = [n for n in since if n.startswith(f"{RESUME} cause=")]
                # A cap stop is a spend limit, not a failure: the raise is the change.
                if (caps >= AUTO_MAX) if capped else (
                        any(n.startswith(f"{RESUME} cause={key} |") for n in tried) or len(tried) >= RESUME_MAX):
                    if not rescope(board, t, f"stopped again ({key})"):
                        escalate(board, t, notes, key)
                    continue
                if capped:
                    t = api("patch", f"{base}/{tid}", {
                        "max_tokens_budget": used + CAP_STEP, "expected_status": "blocked",
                        "expected_updated_at": t["updated_at"],
                        "comment": f"[auto-recovery:cap] token cap raised to {used + CAP_STEP} (+{CAP_STEP})."})
                api("patch", f"{base}/{tid}", {
                    "status": "inbox", "expected_status": "blocked", "expected_updated_at": t["updated_at"],
                    "comment": (f"{RESUME} cap raised | " if capped else f"{RESUME} cause={key} | ")
                               + "resuming the stopped cycle in place: finished work is kept and only the "
                                 "stopped stage reruns. The same cause again escalates."})
                log(f"auto-resume: task {tid} ({'cap raised' if capped else key})")
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pool
                log(f"auto-recovery skipped task {tid}: {exc}")


def accept_published() -> None:
    """Owner policy 2026-10-22: acceptance = native QA PASS + clean publication.
    A team task in Review whose latest cycle is published is closed as Done; for
    other repos the pull request stays the human gate. Runs with recover_blocked."""
    for board in fetch_rows("/api/v1/boards"):
        if board.get("goal_confirmed") is not True:
            continue
        base = f"/api/v1/boards/{board['id']}"
        for t in fetch_rows(f"{base}/tasks"):
            if t.get("status") != "review" or t.get("execution_mode") != "team":
                continue
            tid = t["id"]
            try:
                cycles = api("get", f"{base}/tasks/{tid}/team", None).get("cycles") or []
                cycle = cycles[0]["cycle"] if cycles else {}
                if cycle.get("state") != "published" or not cycle.get("published_sha"):
                    continue
                notes = _notes(f"{base}/tasks", tid)
                if (any(f"{cycle['published_sha'][:10]} is committed locally, but opening the PR failed" in n for n in notes)
                        and not any("published as pull request" in n for n in notes)):
                    continue  # no PR, no publication: 17 rejected pushes were accepted as Done (2026-10-23..26)
                _close(base, t, f"[auto-accept] native QA PASS and qualified publication {cycle['published_sha'][:10]} "
                                "(owner policy 2026-10-22: acceptance = QA PASS + publication).")
                log(f"auto-accepted task {tid} (published {cycle['published_sha'][:10]})")
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pool
                log(f"auto-accept skipped task {tid}: {exc}")


def _close(base: str, t: dict, why: str) -> None:
    tid = t["id"]
    ap = api("post", f"{base}/approvals", {"task_id": tid, "action_type": "close", "confidence": 100,
                                            "payload": {"reason": why}, "lead_reasoning": why})
    if ap.get("status") != "approved":
        api("patch", f"{base}/approvals/{ap['id']}", {"status": "approved"})
    if t["status"] == "in_progress":  # a delegated parent closes via Review
        t = api("patch", f"{base}/tasks/{tid}", {"status": "review", "expected_status": "in_progress",
                                                 "expected_updated_at": t["updated_at"], "comment": why})
    api("patch", f"{base}/tasks/{tid}", {"status": "done", "expected_status": "review",
                                         "expected_updated_at": t["updated_at"], "comment": why})


def settle_parents(board: str) -> None:
    """A split parent ([backlog] lead or a task whose plan became child tasks) delegated all of
    its work: it is Done once every child is Done or cancelled, which releases its dependents,
    and it reopens when a child is open again."""
    base = f"/api/v1/boards/{board}"
    tasks = [t for st in ("inbox", "in_progress", "review", "blocked", "paused", "done", "cancelled")
             for t in fetch_rows(f"{base}/tasks?status={st}")]
    for t in tasks:
        kids = [k for k in tasks if k.get("parent_task_id") == t["id"]]
        if (t.get("status") == "done" and t.get("execution_mode") == "team" and t.get("split_count")
                and any(k["status"] in ("inbox", "in_progress", "blocked", "paused") for k in kids)):
            # A reopened part under a Done parent never starts: the cycle fence wants an
            # in_progress parent (0000a00e, 2026-10-26: 409 twice, then escalated). The parent
            # is open again until its parts settle.
            why = "[auto-reopen] a child task is open again; the split parent waits for it."
            try:
                r = api("patch", f"{base}/tasks/{t['id']}", {"status": "inbox", "expected_status": "done",
                                                            "expected_updated_at": t["updated_at"], "comment": why})
                api("patch", f"{base}/tasks/{t['id']}", {"status": "in_progress", "expected_status": "inbox",
                                                         "expected_updated_at": r["updated_at"]})
                log(f"reopened split parent {t['id']}")
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pool
                log(f"split reopen skipped {t['id']}: {exc}")
            continue
        if (t.get("status") not in ("in_progress", "review") or t.get("execution_mode") != "team"
                or not t.get("split_count") or not any(k["status"] == "done" for k in kids)
                or any(k["status"] not in ("done", "cancelled") for k in kids)):
            continue
        try:
            _close(base, t, f"[auto-accept] split settled: {sum(k['status'] == 'done' for k in kids)}/"
                            f"{len(kids)} child tasks done.")
            log(f"settled split parent {t['id']}")
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pool
            log(f"split settle skipped {t['id']}: {exc}")


def schedulable(t: dict) -> bool:
    """Only explicit team-mode work this lane owns, gated like the legacy runner."""
    if t.get("execution_mode") != "team":
        return False
    lane = (t.get("execution_lane") or "").strip().lower()
    if lane and lane != "harness":
        return False
    status = str(t.get("status", "")).lower()
    if status not in ("inbox", "in_progress"):
        return False  # never reopen Review/Blocked/Done
    if t.get("split_count"):
        return False  # split parents delegate to their board children; the
        # parent itself must never run workers overlapping the children's goal
    if t.get("is_blocked") or t.get("blocked_by_task_ids"):
        return False  # dependencies and explicit blocks are respected
    if t.get("scheduled_at"):
        try:
            due = datetime.fromisoformat(str(t["scheduled_at"]).replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        if due > datetime.now(timezone.utc):
            return False
    # Repeat cooldown: a recurring task is due again only after its interval.
    if (
        t.get("repeat_enabled")
        and t.get("repeat_interval_minutes")
        and status == "inbox"
    ):
        try:
            last = datetime.fromisoformat(str(t.get("updated_at"))[:26])
            interval = timedelta(minutes=int(t["repeat_interval_minutes"]))
        except (TypeError, ValueError):
            return False
        if datetime.now(timezone.utc).replace(tzinfo=None) - last < interval:
            return False
    return True


def eligible(t: dict) -> bool:
    cap = t.get("max_tokens_budget")
    return schedulable(t) and not (
        cap is not None and cap > 0 and (t.get("tokens_used") or 0) >= cap
    )


def api(method: str, path: str, body: dict | None = None):
    from mc_task_budget import request
    return request(MC, method, path, body)


def budget_ready(task: dict) -> bool:
    # Disabled budgets need no pool bookkeeping; the child always rechecks the
    # fresh task before admission/claims, including newly owner-enabled caps.
    if not task.get("max_tokens_budget"):
        return schedulable(task)
    from mc_task_budget import check
    return check(api, task["board_id"], task["id"], task) is not None


def spawn(task_id: str, board_id: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "RUNNER_BOARD": board_id,
        "RUNNER_TEAM_PARALLEL": os.environ.get("RUNNER_TEAM_PARALLEL", "8"),
        "RUNNER_LOCK_DIR": LOCK_DIR,
    }
    log(f"starting team runner for task {task_id}")
    return subprocess.Popen(
        [sys.executable, str(HERE / "mc-team-runner.py"), task_id],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def reap(children: dict[str, subprocess.Popen]) -> None:
    for task_id, proc in list(children.items()):
        if proc.poll() is not None:
            log(f"team runner for task {task_id} exited rc={proc.returncode}")
            del children[task_id]


def supervise() -> None:
    children: dict[str, subprocess.Popen] = {}
    # Round robin: least recently started first. A runner that only waits on an engine hold
    # exits without touching updated_at, so oldest-first restarted the same 6 tasks ~1300
    # times each while 9 others, some able to draft locally, never ran (2026-10-27, 0000a017).
    started: dict[str, float] = {}
    while not stop.is_set():
        reap(children)
        recover_blocked()
        if len(children) < TASKS and repo_ready():
            # No hold while codex is exhausted: held engines' work is drafted on local Qwen
            # and cycles extend while they wait (owner 2026-10-24).
            for task in sorted(
                (t for t in fetch_tasks() if schedulable(t)),
                key=lambda t: (started.get(t["id"], 0.0),
                               str(t.get("updated_at") or t.get("created_at") or "")),
            ):
                if len(children) >= TASKS:
                    break
                task_id = task["id"]
                if task_id in children or not probe_task_lock(task_id):
                    continue  # already driven locally
                try:
                    if not budget_ready(task):
                        continue
                except Exception as exc:
                    log(f"task budget check failed; no child started: {exc}")
                    continue
                children[task_id] = spawn(task_id, task["board_id"])
                started[task_id] = time.monotonic()
        if stop.wait(POLL):
            break
    # Graceful stop: no child is left unmonitored.
    log(f"stopping {len(children)} team runner(s)")
    for proc in children.values():
        if proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    deadline = time.monotonic() + 10
    for proc in children.values():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
    for proc in children.values():
        proc.wait()


def main() -> int:
    if os.environ.get("RUNNER_APPLY", "0") != "1":
        log("pool disarmed; RUNNER_APPLY=1 required")
        return 78
    if not BOARD:
        log("FATAL: RUNNER_BOARD unset")
        return 78
    Path(LOCK_DIR).mkdir(parents=True, exist_ok=True)
    fd = os.open(POOL_LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another mc-team-pool instance already holds the singleton lock; exiting")
        return 0
    # The singleton lock is NOT a worker/publication lock: it only stops a
    # duplicate supervisor. It stays held by this process for its lifetime.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log(f"team pool up: board={BOARD} max_tasks={TASKS} poll={POLL}s")
    supervise()
    log("team pool stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
