"""Owner 2026-10-28: the fleet sat idle with both plans free; the only intake had been a one-off.
The watchdog now names an idle fleet, and a drained backlog starts the local intake."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_idle_fleet_with_free_engines_is_a_finding_that_names_what_it_waits_on(monkeypatch, tmp_path):
    w = load("fleet_watchdog_idle", SCRIPTS / "mc-fleet-watchdog.py")
    board = "b" * 36
    (tmp_path / "boards.json").write_text(json.dumps({board: "/repo"}))
    (tmp_path / "pool.log").write_text("")
    (tmp_path / "batch.log").write_text("no unreferenced open omc-fleet issues; fleet backlog drained\n")
    (tmp_path / "intake.json").write_text(json.dumps({"last_ok": "2026-10-22T03:00:00+00:00"}))
    for attr, value in (("BOARDS", "boards.json"), ("POOL_LOG", "pool.log"), ("BATCH_LOG", "batch.log"),
                        ("INTAKE_STATE", "intake.json"), ("RUNS", "runs")):
        monkeypatch.setattr(w, attr, tmp_path / value)
    monkeypatch.setattr(w, "codex_exhausted", lambda: False)
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    last = {"at": (datetime.now(timezone.utc) - timedelta(hours=7)).replace(tzinfo=None).isoformat(sep=" ")}
    monkeypatch.setattr(w, "sql", lambda q: [[last["at"], "1"]] if "max(created_at), 1 from team_attempts" in q else [])
    pending = [{"type": "major_decision", "submitted_by": "fleet-watchdog", "task_board_id": board, "task_id": "t"}]
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda m, path: pending if "/review/items" in path else []})
    idle = [f for f in w.findings() if "idle" in f]
    assert idle == [f"board {board[:8]}: idle 7h with free engines (1 owner decisions pending; "
                    "next-batch: no unreferenced open omc-fleet issues; fleet backlog drained; last intake: 2026-10-22)"]
    last["at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    assert not [f for f in w.findings() if "idle" in f]  # recent work: not idle
    assert "omc-intake.py" in w.PROMPT


def test_intake_runs_local_qwen_then_sync_at_most_every_interval(monkeypatch, tmp_path):
    intake = load("omc_intake", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com", "mattermost_url": "https://chat.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira", "mattermost"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    calls = []

    def run(label, cmd, timeout, stall=None, on_line=None):
        calls.append(cmd)
        if cmd[0].endswith("/pi"):
            assert stall and on_line and cmd[cmd.index("--mode") + 1] == "json"  # watched, streamed as events
            stamp = next(intake.WORK.glob("INSTRUCTIONS-*.md")).stem.split("-", 1)[1]
            text = (intake.WORK / f"INSTRUCTIONS-{stamp}.md").read_text()
            assert "Pull only what changed since" in text and f"raw/{stamp}/jira.json" in text and "Do not summarize" in text
            source = label.rsplit(" ", 1)[1]  # one session per source: none outgrows the local context
            assert f"only the raw/{stamp}/{source}.json part" in cmd[-1]
            (intake.WORK / "raw" / stamp / f"{source}.json").write_text("[1, 2]")
        return 0, "kept 2, issues 2"
    monkeypatch.setattr(intake, "run", run)
    assert intake.main() == 0
    assert [c[0].endswith("/pi") for c in calls[:3]] == [True] * 3 and "vllm/Qwen3.8-27B-FP8" in calls[0]
    assert calls[3][1].endswith("omc-intake-evaluate.py") and calls[4][1].endswith("omc-todo-sync.py")  # pull, evaluate, sync
    assert calls[4][2] == str(intake.WORK)  # the sync is in git; WORK keeps only its data and redact.py
    stamp = next(intake.WORK.glob("DONE-*.md")).stem.split("-", 1)[1]
    assert json.loads((intake.WORK / f"DONE-{stamp}.md").read_text()) == {"confluence": 2, "jira": 2, "mattermost": 2}
    assert json.loads(intake.STATE.read_text())["last_ok"]
    assert "sync rc=0: kept 2, issues 2" in intake.LOG.read_text()
    calls.clear()
    assert intake.main() == 0 and calls == []  # within INTAKE_EVERY: nothing runs


def test_an_unfinished_intake_never_syncs(monkeypatch, tmp_path):
    intake = load("omc_intake_unfinished", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com", "mattermost_url": "https://chat.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira", "mattermost"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    calls = []
    monkeypatch.setattr(intake, "run", lambda label, cmd, *a, **k: calls.append(cmd) or (1, ""))
    assert intake.main() == 1 and len(calls) == 3  # local sessions only; no sync of a partial pull
    assert "did not finish (missing or unreadable: confluence, jira, mattermost" in intake.LOG.read_text()
    assert "last_ok" not in json.loads(intake.STATE.read_text())


def test_a_stalled_source_does_not_discard_the_pulled_ones(monkeypatch, tmp_path):
    # 2026-10-28: the full refresh's confluence session stalled in compaction; the 63-min jira pull
    # was thrown away with it, no issues were filed and the drained fleet stayed idle.
    intake = load("omc_intake_partial", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com", "mattermost_url": "https://chat.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira", "mattermost"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    calls = []

    def run(label, cmd, timeout, stall=None, on_line=None):
        calls.append(cmd)
        source = label.rsplit(" ", 1)[1]
        if cmd[0].endswith("/pi") and source != "confluence":
            stamp = label.split()[1]
            (intake.WORK / "raw" / stamp / f"{source}.json").write_text("[1]")
        return (124 if source == "confluence" else 0), "issues 1"
    monkeypatch.setattr(intake, "run", run)
    assert intake.main() == 1  # still a failed intake: the watchdog names the missing source
    assert calls[3][1].endswith("omc-intake-evaluate.py") and calls[4][1].endswith("sync.py")
    stamp = next(intake.WORK.glob("DONE-*.md")).stem.split("-", 1)[1]
    assert json.loads((intake.WORK / f"DONE-{stamp}.md").read_text()) == {"jira": 1, "mattermost": 1}
    last = intake.LOG.read_text().splitlines()[-1]
    assert f"intake {stamp} did not finish (missing or unreadable: confluence" in last and "sync rc=0" in last
    assert "last_ok" not in json.loads(intake.STATE.read_text())  # the next run pulls confluence again


def test_pull_progress_is_logged_as_metadata_and_a_silent_pull_is_killed(monkeypatch, tmp_path):
    """Owner 2026-10-28: "there cannot be some unknown progress". The log shows every turn and tool,
    never content; the content stays in WORK's stream; a pull with no output is killed."""
    import sys

    intake = load("omc_intake_progress", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "WORK", tmp_path)
    monkeypatch.setattr(intake, "LOG", tmp_path / "intake.log")
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/jira.json").write_text("[]")
    events = [
        {"type": "tool_execution_start", "toolCallId": "1", "toolName": "mcp", "args": {"server": "camoufox", "tool": "evaluate", "code": "SECRET-ARG"}},
        {"type": "tool_execution_end", "toolCallId": "1", "isError": False, "result": "SECRET-RESULT"},
        {"type": "message_update", "message": {"content": "SECRET-DELTA"}},
        {"type": "turn_end", "message": {"content": "SECRET-TEXT", "usage": {"input": 120, "output": 7}}},
        {"type": "turn_end", "message": {"stopReason": "error", "usage": {"input": 0, "output": 0},
                                         "errorMessage": '401 {"error":"No active credentials for provider"}'}},
        {"type": "compaction_start", "summary": "SECRET-SUMMARY"},
    ]
    script = "import json, sys\nfor e in json.loads(sys.argv[1]): print(json.dumps(e), flush=True)"
    with (tmp_path / "stream.log").open("w") as stream:
        progress = intake.PullProgress("intake 1 pull", stream, tmp_path / "raw")
        assert intake.run("intake 1 pull", [sys.executable, "-c", script, json.dumps(events)], 60, 5, progress) == (0, "")
    log = intake.LOG.read_text()
    assert "tool 1: mcp camoufox evaluate ok" in log and "turn 1: in 120 out 7 tokens; 1 tools (0 errors); raw: jira.json 2B" in log
    assert "intake 1 pull event compaction_start" in log and progress.waiting == "compaction"
    assert "turn 2: in 0 out 0 tokens; 1 tools (0 errors); raw: jira.json 2B; model error: 401 error:No active credentials for provider" in log
    assert "SECRET" not in log and "exited rc=0" in log
    kept = (tmp_path / "stream.log").read_text()
    assert "SECRET-RESULT" in kept and "SECRET-DELTA" not in kept  # streamed tokens are only counted
    with (tmp_path / "stream2.log").open("w") as stream:
        stalled = intake.PullProgress("intake 2 pull", stream, tmp_path / "raw")
        stalled.waiting = "compaction"
        rc, _ = intake.run("intake 2 pull", [sys.executable, "-c", "print('x', flush=True); import time; time.sleep(60)"], 60, 1, stalled)
    assert rc == 124 and "intake 2 pull stalled after 0m (no output for 0m; waiting on compaction); killed" in intake.LOG.read_text()


def test_full_refresh_reads_every_page_and_post_in_batches(monkeypatch, tmp_path):
    """Owner 2026-10-28: 'update all the info'. A full refresh is ~160 pages; the first eight were read."""
    work = tmp_path / "work"
    (work / "raw" / "T").mkdir(parents=True)
    (work / "redact.py").write_text("URL, KEY, MODEL, SCAN = 'u', 'k', 'm', {'email': r'@'}\nd = json.load(open(os.path.join(WORK, 'todos.json')))\n")
    monkeypatch.setattr("sys.argv", ["omc-intake-evaluate.py", str(work), "T"])
    ev = load("omc_intake_evaluate", SCRIPTS / "omc-intake-evaluate.py")
    seen = []
    monkeypatch.setattr(ev, "_evaluate", lambda pages, posts: seen.append((len(pages), len(posts))) or
                        {"decisions": ["same decision"], "next_steps": [f"step {len(seen)}"]})
    merged = ev.evaluate([{"title": f"p{i}"} for i in range(12)], [{"message": "m"}] * 50)
    assert seen == [(5, 0), (5, 0), (2, 0), (0, 40), (0, 10)]
    assert merged["decisions"] == ["same decision"] and len(merged["next_steps"]) == 5


def test_intake_since_forces_a_full_refresh(monkeypatch, tmp_path):
    intake = load("omc_intake_since", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com", "mattermost_url": "https://chat.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira", "mattermost"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("INTAKE_SINCE", "2025-01-01")
    monkeypatch.setattr(intake, "run", lambda *a, **k: (1, ""))
    intake.main()
    text = next(intake.WORK.glob("INSTRUCTIONS-*.md")).read_text()
    assert "changed since 2025-01-01" in text and "never direct or group messages" in text


def test_watchdog_names_a_silent_running_intake_and_a_failed_one(monkeypatch, tmp_path):
    import fcntl
    import os

    w = load("fleet_watchdog_intake", SCRIPTS / "mc-fleet-watchdog.py")
    log = tmp_path / "intake.log"
    log.write_text("2026-10-28T11:00:00+00:00 intake 202610281101 pull turn 3: in 1 out 1 tokens\n")
    for attr, value in (("INTAKE_LOG", log), ("INTAKE_STATE", tmp_path / "state.json"), ("POOL_LOG", tmp_path / "pool.log"),
                        ("BOARDS", tmp_path / "none.json")):
        monkeypatch.setattr(w, attr, value)
    (tmp_path / "pool.log").write_text("")
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " * status: started"})())
    monkeypatch.setattr(w.runpy, "run_path", lambda path: {"request": lambda m, path: []})
    intake = lambda: [f for f in w.findings() if f.startswith("intake")]  # noqa: E731
    assert intake() == []  # idle and fine
    with open(tmp_path / "state.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # a run in progress
        assert intake() == []  # logging: fine
        os.utime(log, (0, time_ago := datetime.now().timestamp() - 3600))
        assert intake() == ["intake running, but its log has been silent for 60m (last: intake 202610281101 pull turn 3: in 1 out 1 tokens)"]
    log.write_text(log.read_text() + "2026-10-28T12:00:00+00:00 intake 202610281101 did not finish (rc=124; blocked: none)\n")
    assert intake() == ["intake failed: intake 202610281101 did not finish (rc=124; blocked: none)"]
    os.utime(log, (0, time_ago - 12 * 3600))
    assert intake() == []  # the next try is due; next-batch reruns it


def test_intake_pulls_only_what_is_newer_than_earlier_pulls_and_at_most_three_weeks(monkeypatch, tmp_path):
    """Owner 2026-10-28: find what earlier runs (Claude or Pi) already pulled, pull only newer items,
    never more than about 3 weeks back; no full re-pull unless asked."""
    import os
    import time

    intake = load("omc_intake_window", SCRIPTS / "omc-intake.py")
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira"))
    monkeypatch.delenv("INTAKE_SINCE", raising=False)
    monkeypatch.setattr(intake, "run", lambda *a, **k: (1, ""))
    (tmp_path / "work").mkdir()

    def since():
        found = sorted(intake.WORK.glob("INSTRUCTIONS-*.md"))
        text = found[-1].read_text()
        for f in found:
            f.unlink()
        intake.STATE.write_text("{}")
        return text.split("Pull only what changed since ")[1][:10]

    day = lambda age: intake.datetime.fromtimestamp(time.time() - age * 86400, intake.timezone.utc).date().isoformat()  # noqa: E731
    intake.main()
    assert since() == day(21)  # nothing pulled yet: three weeks
    for name, age in (("a/jira_all.json", 3), ("b/confluence.json", 1), ("c/jira.json", 30)):  # legacy names count
        f = tmp_path / "work/raw" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[]")
        os.utime(f, (0, time.time() - age * 86400))
    intake.main()
    assert since() == day(3)  # jira's newest pull is the older one
    (tmp_path / "work/raw/d").mkdir()
    (tmp_path / "work/raw/d/confluence.json").write_text("[{")  # a killed session's partial file
    os.utime(tmp_path / "work/raw/b/confluence.json", (0, time.time() - 40 * 86400))
    intake.main()
    assert since() == day(21)  # never more than three weeks back


def test_a_pull_the_local_model_stopped_answering_retries_on_the_next_tick(monkeypatch, tmp_path):
    # 2026-10-29: an interactive session held the local model's only OmniRoute slot; every pull request
    # got a 429 (from the first call, or after a first answer), Pi gave up, and the run took the next 12 h
    # of intake with it while the fleet sat idle.
    intake = load("omc_intake_busy", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    busy = {"type": "auto_retry_start", "errorMessage": "429 Semaphore timeout after 30000ms"}

    def run(label, cmd, timeout, stall=None, on_line=None):
        answered = [{"type": "turn_end", "message": {"usage": {"input": 7420, "output": 70}}}] if "jira" in label else []
        for event in answered + [busy, {"type": "auto_retry_end", "success": False}]:
            on_line(json.dumps(event) + "\n")
        return 0, ""
    monkeypatch.setattr(intake, "run", run)
    assert intake.main() == 1
    log = intake.LOG.read_text()
    assert "event auto_retry_start: 429 Semaphore timeout after 30000ms" in log  # the cause, not just the event
    assert "local model stopped answering confluence, jira; retrying on the next tick" in log.splitlines()[-1]
    assert "last_try" not in json.loads(intake.STATE.read_text())  # next-batch's next tick tries again


def test_a_runaway_pull_is_stopped_and_a_crashed_one_gets_one_fresh_session(monkeypatch, tmp_path):
    # 2026-11-04: a jira session's codemode loop made 10,543 camoufox calls in 7 min (normal pulls: under
    # 160) until Pi aborted (rc=-6); one bad model loop cost jira the next 12 h of intake.
    import sys

    intake = load("omc_intake_runaway", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "WORK", tmp_path)
    monkeypatch.setattr(intake, "LOG", tmp_path / "intake.log")
    monkeypatch.setattr(intake, "MAX_TOOLS", 3)
    loop = ("import json\nwhile True:\n for t in ('tool_execution_start', 'tool_execution_end'):\n"
            "  print(json.dumps({'type': t, 'toolCallId': '1', 'toolName': 'mcp'}), flush=True)")
    with (tmp_path / "stream.log").open("w") as stream:
        progress = intake.PullProgress("intake 1 jira", stream, tmp_path / "raw")
        rc, _ = intake.run("intake 1 jira", [sys.executable, "-c", loop], 60, 1, progress)
    assert rc == 124 and "intake 1 jira ran away (over 3 tool calls) after 0m" in intake.LOG.read_text()

    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("jira",))
    monkeypatch.setattr(intake, "STATE", tmp_path / "state/state.json")
    pulls = []

    def run(label, cmd, timeout, stall=None, on_line=None):
        if cmd[0].endswith("/pi"):
            pulls.append(label)
            if len(pulls) == 1:
                return -6, ""  # Pi aborted
            (intake.WORK / "raw" / label.split()[1] / "jira.json").write_text("[1]")
        return 0, "STATS {}"
    monkeypatch.setattr(intake, "run", run)
    assert intake.main() == 0 and len(pulls) == 2
    assert "jira pull crashed (rc=-6); one fresh session" in intake.LOG.read_text()


def test_a_partial_pull_whose_missing_source_only_waited_is_no_failure(monkeypatch, tmp_path):
    # 2026-10-29: jira pulled and synced 14 new issues, confluence only waited for the busy local slot,
    # yet the run's summary read "did not finish" and the watchdog raised "intake failed" for 12 h.
    import re

    intake = load("omc_intake_partial_wait", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()

    def run(label, cmd, timeout, stall=None, on_line=None):
        if "confluence" in label:
            for event in ({"type": "auto_retry_start", "errorMessage": "429 Semaphore timeout"},
                          {"type": "auto_retry_end", "success": False}):
                on_line(json.dumps(event) + "\n")
        elif "jira" in label:
            (intake.WORK / "raw" / label.split()[1] / "jira.json").write_text("[1]")
        return 0, "STATS {}"
    monkeypatch.setattr(intake, "run", run)
    intake.main()
    last = intake.LOG.read_text().splitlines()[-1].split(" ", 1)[1]
    assert "stopped answering confluence; retrying on the next tick" in last and "sync rc=0" in last
    assert not re.match(r"intake \d+ (did not finish|pulled, but evaluation failed)", last)  # the watchdog's failure line
    assert json.loads(intake.STATE.read_text()) == {}  # retried on the next tick; last_ok waits for confluence


def test_an_evaluation_waits_for_the_busy_local_slot_and_names_a_failure(monkeypatch, tmp_path):
    # 2026-10-30: the evaluation's first request met the local model's only OmniRoute slot busy (429 after
    # the 30 s queue); the uncaught error dropped a complete 50-min pull and the log said only "rc=1".
    import io
    import urllib.error

    work = tmp_path / "work"
    (work / "raw" / "T").mkdir(parents=True)
    (work / "redact.py").write_text("URL, KEY, MODEL, SCAN = 'http://u', 'k', 'm', {}\nd = json.load(open(os.path.join(WORK, 'todos.json')))\n")
    monkeypatch.setattr("sys.argv", ["omc-intake-evaluate.py", str(work), "T"])
    ev = load("omc_intake_evaluate_busy", SCRIPTS / "omc-intake-evaluate.py")
    replies = [429, 429, b'{"choices": [{"message": {"content": "{\\"decisions\\": [\\"d\\"]}"}}]}']

    def urlopen(req, timeout):
        reply = replies.pop(0)
        if isinstance(reply, int):
            raise urllib.error.HTTPError("u", reply, "Too Many Requests", {}, None)
        return io.BytesIO(reply)
    monkeypatch.setattr(ev.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ev.time, "sleep", lambda s: None)
    assert ev._evaluate([{"title": "p"}], []) == {"decisions": ["d"]} and replies == []
    replies[:] = [500]
    try:
        ev._evaluate([{"title": "p"}], [])
        raise AssertionError("a real provider error must still fail the evaluation")
    except urllib.error.HTTPError as e:
        assert e.code == 500

    intake = load("omc_intake_eval_failed", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("jira",))
    for attr, value in (("WORK", "work2"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work2").mkdir()

    def run(label, cmd, timeout, stall=None, on_line=None):
        if "evaluation" in label:
            return 1, "Traceback (most recent call last):\nurllib.error.HTTPError: HTTP Error 429: Too Many Requests\n"
        (intake.WORK / "raw" / label.split()[1] / "jira.json").write_text("[1]")
        return 0, ""
    monkeypatch.setattr(intake, "run", run)
    assert intake.main() == 1
    assert intake.LOG.read_text().splitlines()[-1].endswith(
        "pulled, but evaluation failed rc=1: urllib.error.HTTPError: HTTP Error 429: Too Many Requests")


def test_todo_sync_refreshes_a_changed_ticket_and_requeues_its_issue(monkeypatch, tmp_path):
    """2026-10-31: 12 of 19 changed tickets were known keys; their updates never reached the fleet."""
    sync = load("omc_todo_sync", SCRIPTS / "omc-todo-sync.py")
    monkeypatch.setattr(sync, "REPO", "o/r")
    (tmp_path / "redact.py").write_text(
        "import json, os\nSCAN = {'leak': 'LEAK'}\n"
        "def ask(raw):\n    open(os.path.join(os.path.dirname(__file__), 'asked'), 'a').write(raw['title'] + '\\n')\n"
        "    return {'title': raw['title'], 'summary': raw['summary'], 'acceptance': ['done']}\n"
        "d = json.load(open(os.path.join(WORK, 'todos.json')))\n")
    same, changed, new = ({"key": f"SCC-{n}", "title": t, "summary": s} for n, t, s in
                          ((1, "same", "s"), (2, "changed", "new text"), (3, "new", "n")))

    def old(key, h):
        return {"key": key, "status": "kept", "hash": h, "todo": {"key": key, "type": "Task", "priority": None,
                "title": "old", "summary": "old text", "acceptance": [], "link": None}}
    (tmp_path / "todos.json").write_text(json.dumps({"todos": [same, changed, new]}))
    (tmp_path / "todos-redacted.jsonl").write_text(json.dumps(old("SCC-1", sync.fingerprint(same))) + "\n"
                                                   + json.dumps(old("SCC-2", sync.fingerprint({**changed, "summary": "old"}))) + "\n")
    (tmp_path / "issues-map.json").write_text(json.dumps({"SCC-1": 11, "SCC-2": 12}))
    calls = []

    def gh(*a):
        calls.append(a)
        return "[]" if a[:2] == ("issue", "list") else "https://github.com/o/r/issues/13\n"
    monkeypatch.setattr(sync, "gh", gh)
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: None)  # label create
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert sync.main([str(tmp_path), "--workers", "1"]) == 0
    assert (tmp_path / "asked").read_text().split() == ["changed", "new"]  # an unchanged ticket costs no Qwen call
    edits = [a for a in calls if a[:2] == ("issue", "edit")]
    assert len(edits) == 1 and edits[0][2] == "12" and "new text" in edits[0][edits[0].index("--body") + 1]
    assert edits[0][-2:] == ("--remove-label", "omc-needs-human")  # next-batch judges the new text
    assert [a[a.index("--title") + 1] for a in calls if a[:2] == ("issue", "create")] == ["[SCC-3] new"]
    assert json.loads((tmp_path / "issues-map.json").read_text())["SCC-3"] == 13
    calls.clear()
    (tmp_path / "asked").unlink()
    (tmp_path / "todos.json").write_text(json.dumps({"todos": [{**same, "status": "In Progress"}]}))
    assert sync.main([str(tmp_path)]) == 0  # a status move is no new requirement
    assert not (tmp_path / "asked").exists() and [a[:2] for a in calls] == [("issue", "list")]


def test_a_site_the_model_reports_logged_out_is_no_empty_pull(monkeypatch, tmp_path):
    # 2026-10-31: an empty Saturday pull could not be told from a logged-out site. The instructions had
    # the model write BLOCKED-<site>.md *and* `[]`, so the coordinator logged "done, 0 items" and the
    # empty file moved the next run's since date past the days it never pulled.
    intake = load("omc_intake_blocked", SCRIPTS / "omc-intake.py")
    monkeypatch.setattr(intake, "INTAKE", {"jira_url": "https://jira.example.com", "confluence_url": "https://wiki.example.com"})
    monkeypatch.setattr(intake, "SOURCES", ("confluence", "jira"))
    for attr, value in (("WORK", "work"), ("STATE", "state/state.json"), ("LOG", "intake.log")):
        monkeypatch.setattr(intake, attr, tmp_path / value)
    (tmp_path / "work").mkdir()
    calls = []

    def run(label, cmd, timeout, stall=None, on_line=None):
        calls.append(cmd)
        if cmd[0].endswith("/pi"):
            source, stamp = label.rsplit(" ", 1)[1], label.split()[1]
            if source == "jira":
                (intake.WORK / "BLOCKED-jira.md").write_text("log in")
            (intake.WORK / "raw" / stamp / f"{source}.json").write_text("[]" if source == "jira" else "[1]")
        return 0, "issues 1"
    monkeypatch.setattr(intake, "run", run)
    assert "Write `[]` only when" in (SCRIPTS / "omc-intake-instructions.md").read_text()
    assert intake.main() == 1
    last = intake.LOG.read_text().splitlines()[-1]
    assert "did not finish (missing or unreadable: jira; blocked: BLOCKED-jira.md)" in last
    stamp = next(intake.WORK.glob("DONE-*.md")).stem.split("-", 1)[1]
    assert json.loads((intake.WORK / f"DONE-{stamp}.md").read_text()) == {"confluence": 1}
    assert "last_ok" not in json.loads(intake.STATE.read_text())
    assert intake.pulled_at("jira") == 0 and intake.pulled_at("confluence") > 0  # jira's window stays put


def test_an_unreachable_camoufox_daemon_holding_the_login_profile_is_stopped_before_a_pull(monkeypatch, tmp_path):
    """2026-11-03: a daemon no MCP client could reach kept profile `default` (the logins) open and locked, so the
    confluence pull failed as logged out. Only daemons on the pull's data dir other than the live one are stopped."""
    import os
    import socket
    import threading

    intake = load("omc_intake_reap", SCRIPTS / "omc-intake.py")
    data = tmp_path / "camoufox-mcp"
    (data / "daemon").mkdir(parents=True)
    monkeypatch.setattr(intake, "CAMOUFOX", data)
    monkeypatch.setattr(intake, "LOG", tmp_path / "intake.log")
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(data / "daemon" / "daemon.sock"))
    server.listen()

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{"pid": 101, "active_sessions": 1}')
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    proc = tmp_path / "proc"
    tick = os.sysconf("SC_CLK_TCK")
    (proc).mkdir()
    (proc / "uptime").write_text("100000.0 1.0\n")

    def fake(pid, cmd, env, started_s):
        d = proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(cmd.encode().split()) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        (d / "stat").write_text(f"{pid} (python) S " + " ".join(["0"] * 18) + f" {int(started_s * tick)} 0 0\n")

    on_dir = {"HOME": "/root", "CAMOUFOX_DATA_DIR": str(data)}
    fake(101, "python -m camoufox_mcp.daemon", on_dir, 90000)  # the live one: kept
    fake(102, "python -m camoufox_mcp.daemon", on_dir, 50000)  # orphaned 14h ago: stopped
    fake(103, "python -m camoufox_mcp.daemon", on_dir, 99990)  # just spawned, not yet published: kept
    fake(104, "python -m camoufox_mcp.daemon", {"CAMOUFOX_DATA_DIR": "/root/.local/share/mcp-camoufox"}, 1000)  # other data dir
    fake(105, "python -m http.server", on_dir, 1000)  # not a daemon
    killed = []
    monkeypatch.setattr(intake.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    intake.reap_orphan_browsers("202611030200", proc)

    assert killed == [(102, intake.signal.SIGTERM)]
    assert "stopped camoufox daemon 102" in (tmp_path / "intake.log").read_text()
    server.close()
    # No daemon answers at all: every older daemon on the dir is unreachable, so none is spared as "live".
    killed.clear()
    intake.reap_orphan_browsers("202611030200", proc)
    assert sorted(pid for pid, _ in killed) == [101, 102]
