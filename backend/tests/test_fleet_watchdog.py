import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mc-fleet-watchdog.py"


def test_watchdog_reports_loops_and_exhausted_recoveries(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    now = datetime.now(timezone.utc).isoformat()
    pool = tmp_path / "pool.log"
    pool.write_text("".join(f"{now} team: team cycle could not proceed: HTTP 409 evidence\n" for _ in range(5)))
    boards = tmp_path / "boards.json"
    boards.write_text(json.dumps({"b": "/repo"}))
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "BOARDS", boards)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    tasks = [{"id": "t" * 36, "status": "blocked", "execution_mode": "team", "title": "stuck", "updated_at": now},
             {"id": "r" * 36, "status": "in_progress", "execution_mode": "team", "title": "burn", "updated_at": now, "tokens_used": 4_000_000},
             # Owner re-admitted with a hard cap above usage: bounded, not a runaway.
             {"id": "c" * 36, "status": "in_progress", "execution_mode": "team", "title": "capped", "updated_at": now,
              "tokens_used": 4_000_000, "max_tokens_budget": 4_800_000}]
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: (
        # The plain thread hides "[slug]" markers; only markers=true returns them.
        ([{"message": f"[auto-resume] cause={c} | x"} for c in "abcd"] if "markers=true" in path else [])
        if path.split("?")[0].endswith("/comments") else tasks)})
    found = w.findings()
    assert any("repeating pool error" in f for f in found)
    assert any("blocked after 4 auto-resumes" in f for f in found)
    assert [f[:13] for f in found if "runaway spend" in f] == ["task rrrrrrrr"]


def test_watchdog_ignores_engine_holds_and_drive_loop_waits(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    now = datetime.now(timezone.utc)
    hold = "team cycle could not proceed: HTTP 409: team-pm (admin-assigned): omniroute-codex/gpt-6.1-sol is disabled or ineli"
    idle = "cycle c (developing): drive loop reached its 60-iteration limit"
    pool = tmp_path / "pool.log"
    pool.write_text("".join(f"{now.isoformat()} team: {m}\n" for m in [hold, idle] * 5))
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: []})  # boards/tasks: none
    # Engine holds and idle drive loops (shared slots, sibling leases) are waits.
    assert w.findings() == []


def test_long_claim_path_engine_hold_logs_as_a_wait(monkeypatch, tmp_path):
    """2026-10-22: str(ApiError) cut 'is disabled or ineligible' off a claims path, so a
    codex quota hold looked like a failure and woke Opus."""
    from tests.test_backlog_independent_regressions import runner

    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    r = runner()
    ids = "/api/v1/boards/" + "b" * 36 + "/tasks/" + "t" * 36 + "/team/cycles/" + "c" * 36
    hold = r.ApiError(422, f"mc: HTTP 422 POST {ids}/claims: omniroute-codex/gpt-6.1-sol is disabled or ineligible")
    assert r.cycle_stop_line(hold) == "team cycle waiting on engine hold: omniroute-codex/gpt-6.1-sol is disabled or ineligible"
    now = datetime.now(timezone.utc).isoformat()
    pool = tmp_path / "pool.log"
    pool.write_text("".join(f"{now} team: {r.cycle_stop_line(hold)}\n" for _ in range(5)))
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: []})  # boards/tasks: none
    assert w.findings() == []
    # Real refusals still read as failures.
    other = r.ApiError(422, f"mc: HTTP 422 POST {ids}/claims: specialist profile 'x' is not on the allowed list")
    assert r.cycle_stop_line(other).startswith("team cycle could not proceed: HTTP 422")


def test_errors_logged_before_the_last_opus_run_do_not_retrigger(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    old = datetime.now(timezone.utc).timestamp() - 600
    pool = tmp_path / "pool.log"
    pool.write_text("".join(f"{datetime.fromtimestamp(old, timezone.utc).isoformat()} team: team cycle could not proceed: HTTP 409 evidence\n"
                            for _ in range(5)))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "RUNS", runs)
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: []})  # boards/tasks: none
    assert any("repeating pool error" in f for f in w.findings())
    (runs / "fix.json").write_text("{}")  # an Opus run ended after those lines
    assert w.findings() == []


def test_api_request_rides_out_a_backend_restart(monkeypatch, tmp_path):
    """A redeploy must not fail heartbeats (which cancel workers) or discard results."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import mc_task_budget as b

    calls = []

    def run(argv, **kw):
        calls.append(argv)
        down = len(calls) <= 2
        return type("P", (), {"returncode": 3 if down else 0, "stderr": "cannot reach backend" if down else "",
                              "stdout": "" if down else '{"ok": true}'})()

    monkeypatch.setattr(b.subprocess, "run", run)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    assert b.request("mc", "post", "/heartbeat", {}) == {"ok": True}
    assert len(calls) == 3
    # Real HTTP errors (lease lost, conflict) still fail at once.
    calls.clear()
    monkeypatch.setattr(b.subprocess, "run", lambda argv, **kw: calls.append(argv) or type(
        "P", (), {"returncode": 2, "stderr": "mc: HTTP 409 conflict", "stdout": ""})())
    with pytest.raises(b.ApiError) as exc:
        b.request("mc", "post", "/heartbeat", {})
    assert exc.value.code == 409 and len(calls) == 1


@pytest.mark.parametrize("state, used, stopped", [
    ("qa_passed", 2_131_405, False), ("publishing", 2_131_405, False),
    # 0000a014 (2026-10-25): the cap stop after its round-2 correction cancelled the cycle twice;
    # a started cycle now finishes within the grace, and only a cycle boundary or a runaway stops.
    ("developing", 2_131_405, False), ("qa", 2_131_405, False),
    ("developing", 2_084_726 + 600_000, True), ("blocked", 2_131_405, True), (None, 2_131_405, True)])
def test_cap_stop_never_discards_a_started_cycle(monkeypatch, state, used, stopped):
    """0000a00d (2026-10-24): the QA PASS's own usage crossed the cap; blocking the task
    cancelled the QA-passed cycle. Publication spends no inference, so it must go on."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import mc_task_budget as b

    task = {"id": "t1", "status": "in_progress", "updated_at": "u", "execution_mode": "team",
            "tokens_used": used, "max_tokens_budget": 2_084_726}
    patches = []

    def api(method, path, body=None):
        if method == "patch":
            return patches.append(body)
        if path.endswith("/team"):
            return {"cycles": [{"cycle": {"id": "c", "state": state}}] if state else []}
        return [task]

    assert (b.check(api, "b", "t1", task) is None) == stopped
    assert [p["status"] for p in patches] == (["blocked"] if stopped else [])


def test_database_outage_is_a_finding_even_when_health_is_200(monkeypatch, tmp_path):
    # 2026-10-23: a full disk crash-looped Postgres while /health still answered 200.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_db", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    pool = tmp_path / "pool.log"
    pool.write_text("")
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())

    def down(method, path):
        raise SystemExit("mc: HTTP 500 database system is in recovery mode")

    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": down})
    assert any("API/database unavailable" in f for f in w.findings())


def test_shared_block_reason_and_no_progress_are_findings_even_when_logs_look_like_waits(monkeypatch, tmp_path):
    # 2026-10-23: every symptom of a 17h stall was exempted as a wait; 50 cycles of many tasks
    # died on "cycle deadline exceeded" and the owner had to debug it by hand.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_outcomes", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    now = datetime.now(timezone.utc).isoformat()
    pool = tmp_path / "pool.log"
    pool.write_text(f"{now} team: cycle c (developing): drive loop reached its 60-iteration limit\n" * 10)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "RUNS", runs)
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w, "META_BOARD", "m" * 36)  # the operations board is the one board here
    monkeypatch.setattr(w, "codex_exhausted", lambda: False)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    last_block = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(tzinfo=None).isoformat(sep=" ")

    def run(cmd, *a, **k):
        q = cmd[-1]
        out = ("".join(f"t{i}|{last_block}|cycle deadline exceeded\n" for i in range(12)) if "team_cycles" in q else
               "2026-10-23 10:35:14.123456|1\n" if "from tasks" in q else " * status: started")
        return type("R", (), {"stdout": out})()

    monkeypatch.setattr(w.subprocess, "run", run)
    tasks = [{"id": "i" * 36, "status": "inbox", "execution_mode": "team", "title": "x", "updated_at": "2026-10-23T10:00:00+00:00"}]
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: tasks})
    found = w.findings()
    assert "12 tasks blocked in 6h with the same reason (system cause, not task-specific): cycle deadline exceeded" in found
    assert any("no task finished (published/Done) for over 8h" in f for f in found)
    assert not any("iteration limit" in f for f in found)  # the symptom stays a wait; the outcome is the signal
    # 2026-10-24: blocks an Opus run already fixed must not re-wake Opus for the rest of the
    # window; one fresh block with the same reason brings the whole count back.
    (runs / "fix.json").write_text("{}")
    os.utime(runs / "fix.json", (time.time() - 60,) * 2)  # the run ended after those blocks
    assert not any("same reason" in f for f in w.findings())
    last_block = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    assert any(f.startswith("12 tasks blocked in 6h with the same reason") for f in w.findings())


def test_no_progress_counts_frozen_work_not_only_runnable_tasks(monkeypatch, tmp_path):
    # 2026-10-25: nothing finished for 13h; every open task was an old pause, a
    # dependency-blocked inbox task or a split parent, so no task was "runnable".
    spec = importlib.util.spec_from_file_location("fleet_watchdog_frozen", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    pool = tmp_path / "pool.log"
    pool.write_text("")
    monkeypatch.setattr(w, "POOL_LOG", pool)
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w, "META_BOARD", "m" * 36)  # the operations board is the one board here
    monkeypatch.setattr(w, "codex_exhausted", lambda: False)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    done = (datetime.now(timezone.utc) - timedelta(hours=13)).replace(tzinfo=None).isoformat(sep=" ")
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, *a, **k: type("R", (), {
        "stdout": f"{done}|1\n" if "from tasks" in cmd[-1] else " * status: started"})())
    old = (datetime.now(timezone.utc) - timedelta(hours=15)).isoformat()
    tasks = [
        {"id": "p" * 36, "status": "paused", "execution_mode": "team", "title": "p", "updated_at": old},
        {"id": "i" * 36, "status": "inbox", "execution_mode": "team", "title": "i", "updated_at": old,
         "is_blocked": True},
        {"id": "l" * 36, "status": "in_progress", "execution_mode": "team", "title": "l", "updated_at": old,
         "split_count": 3},
    ]
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: tasks})
    found = w.findings()
    assert any("no task finished (published/Done) for over 8h" in f for f in found)
    assert not any("no runner started" in f for f in found)  # nothing was runnable
    # 2026-10-26: a board idle since its last Done got one new task; it has waited minutes, not 8h.
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=18)).isoformat()
    for t in tasks:
        t["created_at"] = fresh
    assert not any("no task finished" in f for f in w.findings())
    tasks[0]["created_at"] = old  # one task has waited since before the last Done
    assert any("no task finished" in f for f in w.findings())
    tasks[:] = [dict(t, status="done") for t in tasks]
    assert not any("no task finished" in f for f in w.findings())  # a settled board is no stall


def test_no_progress_skips_work_that_waits_only_on_a_pending_owner_decision(monkeypatch, tmp_path):
    # 2026-10-29: a task was paused under an owner decision and its lead waited on it. The idle
    # finding was held for that decision, but this check re-raised the same wait 8h later.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_deciding", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    (tmp_path / "pool.log").write_text("")
    monkeypatch.setattr(w, "POOL_LOG", tmp_path / "pool.log")
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w, "META_BOARD", "m" * 36)
    monkeypatch.setattr(w, "codex_exhausted", lambda: False)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    done = (datetime.now(timezone.utc) - timedelta(hours=13)).replace(tzinfo=None).isoformat(sep=" ")
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, *a, **k: type("R", (), {
        "stdout": f"{done}|1\n" if "from tasks" in cmd[-1] else " * status: started"})())
    old = (datetime.now(timezone.utc) - timedelta(hours=15)).isoformat()
    team = {"execution_mode": "team", "title": "t", "updated_at": old, "created_at": old}
    tasks = [
        dict(team, id="p" * 36, status="paused", parent_task_id="l" * 36),
        dict(team, id="l" * 36, status="in_progress", split_count=2),
        dict(team, id="i" * 36, status="inbox", is_blocked=True, blocked_by_task_ids=["p" * 36]),
    ]
    pending = [{"type": "major_decision", "submitted_by": "fleet-watchdog", "task_id": "p" * 36}]
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {
        "request": lambda method, path: [] if "/comments" in path else pending if "/review/items" in path else tasks})
    stalled = lambda: any("no task finished" in f for f in w.findings())
    assert not stalled()  # the owner's choice is the one legitimate wait, with its parent and dependent
    tasks.append(dict(team, id="q" * 36, status="paused", parent_task_id="l" * 36))
    assert stalled()  # the lead also waits on a child no decision covers
    tasks.pop()
    pending.clear()
    assert stalled()  # with no decision pending the same board is a stall


def test_opus_tries_count_per_finding_however_the_other_findings_change(monkeypatch, tmp_path):
    # 2026-10-24: tries were keyed on the whole finding set, so two tasks waiting on a human
    # merge went to Opus three times (06:00, 06:20, 06:40) as the set shrank from 5 to 2.
    # 2026-10-26: no give-up to the owner; an exhausted finding retries every RETRY_AFTER.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_tries", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    for name in ("STATE", "LOG"):
        monkeypatch.setattr(w, name, tmp_path / name)
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    prompts, current, posts = [], [], []
    monkeypatch.setattr(w, "findings", lambda: current)
    monkeypatch.setattr(w, "outcomes", lambda: [])  # never the live database
    monkeypatch.setattr(w, "settle_alerts", lambda found: None)
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda *a: posts.append(a) or []})
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, stdout, **k: (
        prompts.append(cmd[-1]), stdout.write("{}"), type("R", (), {"returncode": 0})())[-1])

    def check(found, **state):
        current[:] = found
        prompts.clear()
        if w.STATE.exists():
            w.STATE.write_text(json.dumps({**json.loads(w.STATE.read_text()), "next": 0, **state}))
        w.main()
        return [line[2:] for p in prompts for line in p.split("\n\nEarlier")[0].splitlines() if line.startswith("- task")]

    a, b, shared = "task aaaaaaaa blocked after 2 auto-recoveries: x", "task bbbbbbbb blocked after 2 auto-recoveries: y", "task cccccccc stuck"
    assert check([shared, a, b]) == [shared, a, b]
    assert check([a, b]) == [a, b]  # shared cleared; a and b are on their second try
    assert check([a, b]) == []  # exhausted for now: no Opus, and no bare alert for the owner
    assert not [x for x in posts if x[0] == "POST"]
    d = "task dddddddd runaway spend: 1 tokens"
    assert check([a, d]) == [d]  # a new finding still gets Opus, alone
    old = time.time() - w.RETRY_AFTER - 1
    assert check([a, d], last={w.fingerprint(a): old, w.fingerprint(d): time.time()}) == [a, d]  # a retried later
    assert "find the structural cause now, or ESCALATE" in prompts[0]
    assert check([]) == [] and check([a]) == [a]  # a cleared finding that returns starts over
    # State from before per-finding tries kept one key for the whole set: it carries over.
    w.STATE.write_text(json.dumps({"step": 0, "next": 0, "tries": {w.fingerprint(f"{a}\n{b}"): 2}}))
    assert check([a, b]) == []


def test_only_a_models_escalation_reaches_the_owner_and_the_choice_is_carried_out(monkeypatch, tmp_path):
    """Owner 2026-10-26: maximum autonomy; only what no model can supply becomes an owner
    decision with options, the task waits for it, and the system then executes the choice."""
    spec = importlib.util.spec_from_file_location("fleet_watchdog_decide", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    for name in ("STATE", "LOG"):
        monkeypatch.setattr(w, name, tmp_path / name)
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    tid = "00000000-0000-4000-8000-0000000000c3"
    finding = f"task {tid} blocked, no automatic recovery left: Podman preflight"
    current, prompts, items = [finding], [], {}
    reply = {"result": "Root cause: #181 baseline missing.\nESCALATE: " + json.dumps({
        "title": "Which Podman/crun baseline should #181 pin?", "task_id": tid, "summary": "No source names one.",
        "options": ["Pin Podman 5.4.2 + crun 1.21 as documented defaults", "Owner supplies the production tuple",
                    "Drop the preflight from the fleet (system design: offline-only scope)",
                    "Report an unverifiable profile identity as a warning with a documented manual check: "
                    "the watchdog changes the acceptance item so a clean host passes, and re-admits the task"]})}

    def request(method, path, body=None):
        if method == "POST":
            items["r1"] = {"id": "r1", "status": "pending", **json.loads(body)}
            return items["r1"]
        return list(items.values())
    monkeypatch.setattr(w, "findings", lambda: current)
    monkeypatch.setattr(w, "outcomes", lambda: [])
    monkeypatch.setattr(w, "settle_alerts", lambda found: None)
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": request})
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, stdout, **k: (
        prompts.append(cmd[-1]), stdout.write(json.dumps(reply)), type("R", (), {"returncode": 0})())[-1])

    def tick():
        prompts.clear()
        if w.STATE.exists():
            w.STATE.write_text(json.dumps({**json.loads(w.STATE.read_text()), "next": 0}))
        w.main()

    tick()
    assert "ESCALATE:" in prompts[0] and "Never escalate what a model could still solve" in prompts[0]
    item = items["r1"]
    assert item["type"] == "major_decision" and item["task_id"] == tid and item["title"].startswith("DECISION: Which")
    assert item["proposed_action"].splitlines()[0] == "1. Pin Podman 5.4.2 + crun 1.21 as documented defaults"
    assert item["proposed_action"].endswith("so a clean host passes, and re-admits the task")  # never cut
    reply["result"] = "nothing new"
    w.STATE.write_text(json.dumps({**json.loads(w.STATE.read_text()), "last": {w.fingerprint(finding): 0}}))
    tick()
    assert prompts == []  # held while the owner decides, even past RETRY_AFTER
    item.update(status="approved", reviewer_note="Pin Podman 5.4.2 + crun 1.21 as documented defaults")
    tick()
    assert len(prompts) == 1 and "Owner's choice: Pin Podman 5.4.2 + crun 1.21" in prompts[0]
    assert "4. Report an unverifiable" in prompts[0] and "and re-admits the task" in prompts[0]
    assert json.loads(w.STATE.read_text())["decisions"] == {}
    tick()
    assert len(prompts) == 1  # carried out once; the finding is worked again if it persists
    item.update(status="resolved", reviewer_note="Superseded by a newer item for the same task.")
    assert w.escalation("ESCALATE: {\"title\": \"x\", \"options\": [\"only one\"]}") is None  # needs 2+ options


def test_one_block_reason_in_different_stages_is_one_shared_cause(monkeypatch):
    # 2026-10-23: 0000a016, 0000a01a and 0000a01b each timed out in another stage; grouped by
    # the whole reason, the stage name split them and no finding fired.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_stages", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    why = "not retryable unchanged (interrupted: attempt time limit reached; partial output discarded)"
    rows = [[f"t{i}", at, f"stage {stage}: {why}"] for i, stage in enumerate(["pm", "dev-a", "dev-component-layout"])]
    monkeypatch.setattr(w, "sql", lambda q: rows)
    assert w.repeated_blocks(0) == [
        "3 tasks blocked in 6h with the same reason (system cause, not task-specific): "
        f"stage dev-a: {why}"]
    monkeypatch.setattr(w, "sql", lambda q: rows[:2] + [["t0", at, f"stage qa: {why}"]])
    assert w.repeated_blocks(0) == []  # two tasks are not a shared cause


def test_a_discarded_qa_pass_is_a_finding(monkeypatch):
    # 2026-10-24: a cap stop cancelled 0000a00d's QA-passed cycle; nothing noticed for 3 days.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_pass", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(w, "sql", lambda q: [["0000a00d-cd79", at.isoformat(sep=" "), "cancelled"]])
    assert w.discarded_passes(0) == [
        "task 0000a00d: QA-passed cycle cancelled without publication (verified work discarded)"]
    assert w.discarded_passes(at.replace(tzinfo=timezone.utc).timestamp() + 1) == []  # already handed to Opus


def test_a_cycle_cancelled_by_a_cap_stop_is_a_finding(monkeypatch):
    # 2026-10-21/25: 60 cap-stop cancellations discarded paid work; none was a finding.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_capcancel", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    at = datetime(2026, 10, 25, 9, 7, 6)
    queries = []
    monkeypatch.setattr(w, "sql", lambda q: queries.append(q) or [["0000a014-ad7d", at.isoformat(sep=" ")]])
    assert w.cap_cancels(0) == ["task 0000a014: cycle cancelled by a cap stop mid-cycle (paid work discarded)"]
    assert "Task input/output cap reached" in queries[0] and "'cancelled'" in queries[0]
    assert w.cap_cancels(at.replace(tzinfo=timezone.utc).timestamp() + 1) == []  # already handed to Opus


def test_an_attempt_never_claimed_for_6h_is_a_finding(monkeypatch):
    # 2026-10-24/25: the claim path skipped two queued PM drafts for 24h; the runner logged only
    # 'iteration limit' waits and reap() kept extending the cycles, so nothing fired.
    spec = importlib.util.spec_from_file_location("fleet_watchdog_unclaimed", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    monkeypatch.setattr(w, "sql", lambda q: [["0000a00f-ff49", "pm", "vllm/Qwen3.8-27B-FP8"]])
    monkeypatch.setattr(w, "codex_exhausted", lambda: False)
    assert w.unclaimed_attempts() == ["task 0000a00f: stage pm queued over 6h without a claim (vllm/Qwen3.8-27B-FP8)"]
    monkeypatch.setattr(w, "codex_exhausted", lambda: True)
    assert w.unclaimed_attempts() == []  # every frontier engine held: QA and leads wait for the reset


def test_alerts_dedupe_across_drifting_counts_and_settle_when_cleared(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog_alerts", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    monkeypatch.setattr(w, "LOG", tmp_path / "log")
    items, calls = [{"id": "old", "title": w.ALERT + "12 tasks blocked in 6h with the same reason: x"},
                    {"id": "gone", "title": w.ALERT + "task aaaaaaaa runaway spend: 9 tokens"},
                    {"id": "human", "title": "MAJOR: task failed 3 times: y"}], []
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda m, path, body=None: (
        calls.append((m, path, body)), {"items": items} if m == "GET" else {})[1]})
    w.settle_alerts(["14 tasks blocked in 6h with the same reason: x"])
    assert [c[1] for c in calls if c[0] == "PATCH"] == ["/api/v1/review/items/gone"]


def test_mass_pause_counts_recent_pauses_not_a_standing_decision_backlog(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog_pauses", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    (tmp_path / "pool.log").write_text("")
    (tmp_path / "boards.json").write_text(json.dumps({"b": "/repo"}))
    monkeypatch.setattr(w, "POOL_LOG", tmp_path / "pool.log")
    monkeypatch.setattr(w, "BOARDS", tmp_path / "boards.json")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    tasks = [{"id": f"{i}" * 36, "status": "paused", "execution_mode": "team", "title": "p", "updated_at": old} for i in range(5)]
    notes, pending = {}, []

    def request(method, path):
        if "/comments" in path:
            return [{"message": m, "created_at": str(n)} for n, m in enumerate(notes.get(path.split("/")[-2], []))]
        return pending if "/review/items" in path else tasks
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": request})
    stuck = lambda: [f for f in w.findings() if "no automatic recovery left" in f]
    assert not any("five or more tasks paused" in f for f in w.findings())
    assert stuck() == []  # an owner's own pause (no pool marker) is the owner's hand
    # 2026-10-26: no task waits on the owner. A pool pause for meta-debug gets 6h, then counts.
    notes["0" * 36] = ["[auto-resume] cause=x | y", "[escalated] stopped again after resuming (x)"]
    assert stuck() == [f"task {'0' * 36} paused, no automatic recovery left: p"]
    notes["0" * 36].append("[meta-resumed] meta-debug m is Done; one more attempt.")
    assert stuck() == []
    tasks[1]["status"] = "blocked"  # the pool is done with a task blocked for an hour
    assert stuck() == [f"task {'1' * 36} blocked, no automatic recovery left: p"]
    pending.append({"type": "major_decision", "submitted_by": "fleet-watchdog", "task_id": "1" * 36})
    assert stuck() == []  # a model escalated it: the owner's choice is the one legitimate wait
    tasks[1]["status"] = "paused"
    for t in tasks:
        t["updated_at"] = datetime.now(timezone.utc).isoformat()
    assert any("five or more tasks paused" in f for f in w.findings())


def _loaded(monkeypatch, tmp_path, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    for attr in ("STATE", "LOG"):
        monkeypatch.setattr(w, attr, tmp_path / attr)
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    (tmp_path / "boards.json").write_text(json.dumps({"b" * 36: "/repo"}))
    monkeypatch.setattr(w, "BOARDS", tmp_path / "boards.json")
    return w


def test_outcome_review_reports_low_shipping_with_its_top_stops(monkeypatch, tmp_path):
    # 2026-10-22/24: every stop got a point fix, yet 17-23% of ~90 cycles a day shipped.
    w = _loaded(monkeypatch, tmp_path, "fleet_watchdog_outcomes")
    sent = []
    data = {"ended": "20|4|9000000"}
    monkeypatch.setattr(w, "sql", lambda q: sent.append(q) or (
        [data["ended"].split("|")] if "filter" in q else [["9", "cycle deadline exceeded"], ["5", "QA failed in round #/#"]]))
    found = w.outcomes()
    assert found == ["board bbbbbbbb: only 4/20 cycles shipped in 24h (9M tokens); "
                     "top stops: 9x cycle deadline exceeded; 5x QA failed in round #/#"]
    data["ended"] = "20|8|1"  # 40% ships: healthy
    assert w.outcomes() == []
    data["ended"] = "6|0|1"  # too few cycles to judge
    assert w.outcomes() == []


def test_outcome_review_runs_once_a_day_with_fresh_tries_and_run_history(monkeypatch, tmp_path):
    w = _loaded(monkeypatch, tmp_path, "fleet_watchdog_review")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20261025T090000-x.json").write_text(json.dumps({"result": "Fix: cb61de92 cap grace"}))
    prompts, review = [], ["board bbbbbbbb: only 4/20 cycles shipped in 24h (9M tokens); top stops: x"]
    monkeypatch.setattr(w, "findings", lambda: [])
    monkeypatch.setattr(w, "outcomes", lambda: review)
    monkeypatch.setattr(w, "settle_alerts", lambda found: None)
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, stdout, **k: (
        prompts.append(cmd[-1]), stdout.write("{}"), type("R", (), {"returncode": 0})())[-1])

    def tick():
        if w.STATE.exists():
            w.STATE.write_text(json.dumps({**json.loads(w.STATE.read_text()), "next": 0}))
        w.main()

    tick()
    assert len(prompts) == 1 and review[0] in prompts[0]
    assert "outcome review, not one bug" in prompts[0] and "cb61de92 cap grace" in prompts[0]  # earlier runs
    assert "Maximum autonomy" in prompts[0] and "human must decide" not in prompts[0]
    tick()
    assert len(prompts) == 1  # not again the same day
    state = json.loads(w.STATE.read_text())
    w.STATE.write_text(json.dumps({**state, "reviewed_at": time.time() - w.REVIEW_EVERY - 1,
                                   "tries": {w.fingerprint(review[0]): w.MAX_TRIES}}))
    tick()
    assert len(prompts) == 2  # next day: fresh tries, not an owner alert


def test_gpt_sol_takes_over_while_opus_is_out_of_usage_and_hands_back(monkeypatch, tmp_path):
    """Owner 2026-10-27: Opus running out of usage must not stop the meta level. gpt-6.1-sol runs
    the same prompt (and the shared HANDOFF.md) until Opus's reset, then Opus takes over again."""
    w = _loaded(monkeypatch, tmp_path, "fleet_watchdog_handoff")
    (tmp_path / "runs").mkdir()
    calls, opus_out = [], {"value": True}

    def run(cmd, stdout, **k):
        calls.append((cmd[0].rsplit("/", 1)[-1], cmd[-1]))
        if cmd[0].endswith("claude"):
            stdout.write(json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit · resets 5pm (UTC)"})
                         if opus_out["value"] else json.dumps({"type": "result", "result": "Opus fixed it", "total_cost_usd": 1}))
        else:
            stdout.write(json.dumps({"type": "turn_end", "message": {"content": [{"type": "text", "text": "sol fixed it"}]}}) + "\n")
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(w.subprocess, "run", run)
    state = {}
    assert w.run_agent("PROMPT", "k1", state) == "sol fixed it"
    assert [c[0] for c in calls] == ["claude", "pi"] and calls[0][1] == calls[1][1] == "PROMPT"
    assert state["opus_until"] and json.loads(next((tmp_path / "runs").glob("*-k1.json")).read_text())["engine"] == "gpt-6.1-sol"
    calls.clear()
    assert w.run_agent("PROMPT", "k2", state) == "sol fixed it" and [c[0] for c in calls] == ["pi"]  # Opus held
    opus_out["value"], state["opus_until"] = False, "2000-01-01T00:00:00+00:00"  # reset passed
    calls.clear()
    assert w.run_agent("PROMPT", "k3", state) == "Opus fixed it" and [c[0] for c in calls] == ["claude"]
    assert "opus_until" not in state
    assert "First read /" in w.PROMPT.format(**w.PROMPT_PATHS, findings="- x", history="- none", handoff=w.HANDOFF)


def test_when_opus_and_sol_are_both_out_findings_wait_without_spending_tries(monkeypatch, tmp_path):
    """2026-10-27: OmniRoute's codex pool hit its quota threshold while Opus could run out too."""
    w = _loaded(monkeypatch, tmp_path, "fleet_watchdog_both_out")
    (tmp_path / "runs").mkdir()
    calls = []
    limit = ('omniroute-api API error (429): {"message":"[codex/gpt-6.1-sol] All codex accounts reached '
             'configured quota threshold (reset after 2h 19m 6s)"}')

    def run(cmd, stdout, **k):
        calls.append(cmd[0].rsplit("/", 1)[-1])
        stdout.write(json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit · resets 5pm (UTC)"})
                     if cmd[0].endswith("claude") else
                     json.dumps({"type": "turn_end", "message": {"stopReason": "error", "errorMessage": limit, "content": []}}) + "\n")
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(w.subprocess, "run", run)
    state = {}
    assert w.run_agent("PROMPT", "k1", state) == "" and calls == ["claude", "pi"]
    assert w.engines_out(state)
    monkeypatch.setattr(w, "findings", lambda: ["task cccccccc stuck"])
    monkeypatch.setattr(w, "outcomes", lambda: [])
    monkeypatch.setattr(w, "settle_alerts", lambda found: None)
    w.STATE.write_text(json.dumps({"step": 0, "next": 0, "tries": {}, **state}))
    calls.clear()
    w.main()
    assert calls == []  # no engine has usage: no run, no spent try
    assert json.loads(w.STATE.read_text())["tries"][w.fingerprint("task cccccccc stuck")] == 0
    assert "out of usage until" in w.LOG.read_text()


def test_watchdog_reports_a_recent_run_without_the_configured_ponytail(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    now, old = datetime.now(timezone.utc), datetime.now(timezone.utc) - timedelta(days=3)
    (tmp_path / "ponytail.json").write_text(json.dumps({"defaultMode": "ultra"}))
    (tmp_path / "sidecar.json").write_text(json.dumps({"models": {"vllm": {
        "ok": {"observed_harness": {"skills": ["ponytail:ultra"], "at": now.isoformat()}},
        "bare": {"observed_harness": {"skills": [], "at": now.isoformat()}},
        "stale": {"observed_harness": {"skills": [], "at": old.isoformat()}}}}}))
    monkeypatch.setattr(w, "PONYTAIL_CONFIG", tmp_path / "ponytail.json")
    monkeypatch.setattr(w, "SIDECAR", tmp_path / "sidecar.json")
    (tmp_path / "pool.log").write_text("")
    monkeypatch.setattr(w, "POOL_LOG", tmp_path / "pool.log")
    monkeypatch.setattr(w, "BOARDS", tmp_path / "none.json")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda method, path: []})
    assert [f for f in w.findings() if "ponytail" in f] == [
        "engine vllm/bare ran without ponytail:ultra (reported skills: none)"]


def test_watchdog_uses_its_compose_projects_db_and_survives_a_missing_harness(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "omc-demo")
    spec = importlib.util.spec_from_file_location("fleet_watchdog_instance", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    assert w.DB[2] == "omc-demo-db-1"
    assert w._run(["/nonexistent/claude", "-p", "x"], tmp_path / "out.json") == 127
    assert "nonexistent" in (tmp_path / "out.json").read_text()


def test_a_task_decision_never_parks_an_idle_board(monkeypatch, tmp_path):
    """2026-10-29: an owner decision came from an 'idle with free engines' run and held that board
    finding for 18h; nothing looked for the board's other work until the owner asked. A pending
    decision holds the task it is about, never the board's outcome finding."""
    spec = importlib.util.spec_from_file_location("fleet_watchdog_board_hold", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    for name in ("STATE", "LOG"):
        monkeypatch.setattr(w, name, tmp_path / name)
    monkeypatch.setattr(w, "RUNS", tmp_path / "runs")
    finding = "board 00000000: idle 3h with free engines (0 owner decisions pending; next-batch: drained)"
    prompts, items = [], {}
    reply = {"result": "ESCALATE: " + json.dumps({
        "title": "Which source proves the profile?", "task_id": "00000000-0000-4000-8000-0000000000c3",
        "summary": "One task waits on it.", "options": ["Warn", "Fail"]})}

    def request(method, path, body=None):
        if method == "POST":
            items["r1"] = {"id": "r1", "status": "pending", **json.loads(body)}
            return items["r1"]
        return list(items.values())
    monkeypatch.setattr(w, "findings", lambda: [finding])
    monkeypatch.setattr(w, "outcomes", lambda: [])
    monkeypatch.setattr(w, "settle_alerts", lambda found: None)
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": request})
    monkeypatch.setattr(w.subprocess, "run", lambda cmd, stdout, **k: (
        prompts.append(cmd[-1]), stdout.write(json.dumps(reply)), type("R", (), {"returncode": 0})())[-1])

    def tick():
        prompts.clear()
        if w.STATE.exists():
            w.STATE.write_text(json.dumps({**json.loads(w.STATE.read_text()), "next": 0}))
        w.main()

    tick()
    assert items["r1"]["status"] == "pending" and len(prompts) == 1
    assert "pending owner decision explains only its own task" in prompts[0]
    assert "standing rules" in prompts[0]
    reply["result"] = "re-queued two issues"
    tick()  # second try within MAX_TRIES: the decision does not park the board finding
    assert len(prompts) == 1


def _watchdog():
    spec = importlib.util.spec_from_file_location("fleet_watchdog", SCRIPT)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    return w


def test_an_instance_checks_its_own_ports(monkeypatch):
    w = _watchdog()
    monkeypatch.setenv("BACKEND_PORT", "13001")
    monkeypatch.setenv("FRONTEND_PORT", "13000")
    urls = []
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda url, **k: urls.append(url))
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(w.runpy, "run_path", lambda path: (_ for _ in ()).throw(OSError("down")))
    w.findings()
    assert urls == ["http://127.0.0.1:13001/health", "http://127.0.0.1:13000/"]


def test_the_pool_check_asks_the_right_service_manager(monkeypatch):
    w = _watchdog()
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("env", {}).get("XDG_RUNTIME_DIR")))
        return type("R", (), {"stdout": " * status: started", "returncode": 0})()

    monkeypatch.setattr(w.subprocess, "run", run)
    monkeypatch.delenv("OMC_DATA", raising=False)
    assert w.pool_started() and calls[-1][0] == ["rc-service", "mc-team-pool", "status"]  # production
    monkeypatch.setenv("OMC_DATA", "/home/omc-x/omc/data")
    monkeypatch.setenv("RUNNER_APPLY", "0")
    calls.clear()
    assert w.pool_started() and calls == []  # a disarmed instance stops its pool on purpose
    monkeypatch.setenv("RUNNER_APPLY", "1")
    assert w.pool_started()
    cmd, runtime = calls[-1]
    assert cmd in (["rc-service", "-U", "mc-team-pool", "status"], ["systemctl", "--user", "is-active", "--quiet", "mc-team-pool"])
    assert runtime == f"/run/user/{os.getuid()}"
