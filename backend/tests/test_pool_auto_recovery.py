import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.api.comment_filters import SYSTEM_MARKER_REGEX

POOL = Path(__file__).resolve().parents[2] / "scripts/mc-team-pool.py"
META = "00000000-0000-4000-8000-0000000000aa"  # the operations board that holds [meta-debug] tasks


def load(monkeypatch, tmp_path):
    mapping = tmp_path / "board-repos.json"
    mapping.write_text(json.dumps({"b": "/repo"}))
    monkeypatch.setenv("RUNNER_BOARD_REPOS", str(mapping))
    monkeypatch.setenv("RUNNER_META_BOARD", META)
    spec = importlib.util.spec_from_file_location("pool_auto_recovery", POOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blocked_fleet_tasks_resume_once_per_cause_and_cap_stops_get_extended(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    tasks = [
        {"id": "capped", "status": "blocked", "execution_mode": "team", "max_tokens_budget": 100, "tokens_used": 150, "updated_at": "t1"},
        {"id": "qa", "status": "blocked", "execution_mode": "team", "max_tokens_budget": 900, "tokens_used": 10, "updated_at": "t2"},
        {"id": "spent", "status": "blocked", "execution_mode": "team", "title": "t", "updated_at": "t3", "description": "x\n\n## Re-scope (fleet)"},
        {"id": "capped_twice", "status": "blocked", "execution_mode": "team", "title": "t", "max_tokens_budget": 100, "tokens_used": 150, "updated_at": "t6", "description": "x\n\n## Re-scope (fleet)"},
        {"id": "single", "status": "blocked", "execution_mode": "single", "updated_at": "t4"},
        {"id": "running", "status": "in_progress", "execution_mode": "team", "updated_at": "t5"},
    ]
    comments = {"spent": [{"message": "[auto-resume] cause=stopped | x"}], "qa": [{"message": "[auto-recovery:cap] x"}],
                "capped_twice": [{"message": "[auto-recovery:cap] a"}, {"message": "[auto-recovery:cap] b"}]}  # never a third raise
    calls = []
    monkeypatch.setattr(p, "log", lambda message: None)  # never write the production pool log
    monkeypatch.setattr(p, "fetch_rows", lambda path: tasks if path == "/api/v1/boards/b/tasks" else [] if path.endswith("/tasks") else comments.get(path.split("/")[-2], []))
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((path.split("/")[-1], body)) or {**(body or {}), "id": "m", "updated_at": "new"})
    monkeypatch.setattr(p, "HANDOFFS", tmp_path / "handoffs")
    p.recover_blocked()
    readmits = [c for c in calls if c[0] != "tasks" and (c[1] and c[1].get("status") == "inbox" or (c[1] or {}).get("max_tokens_budget"))]
    assert [c[0] for c in readmits] == ["capped", "capped", "qa"]
    assert readmits[0][1]["max_tokens_budget"] == 150 + p.CAP_STEP
    assert readmits[1][1]["status"] == "inbox" and readmits[1][1]["expected_updated_at"] == "new"
    assert readmits[2][1]["comment"].startswith("[auto-resume] cause=stopped |")
    paused = [c[0] for c in calls if (c[1] or {}).get("status") == "paused"]
    assert paused == ["spent", "capped_twice"]  # the same cause again, and a spent cap, escalate
    calls.clear()
    p.recover_blocked()  # rate-limited: no second sweep within AUTO_EVERY
    assert calls == []


def test_published_review_tasks_are_accepted_and_unpublished_ones_left(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    tasks = [{"id": "pub", "status": "review", "execution_mode": "team", "updated_at": "u1"},
             {"id": "unpub", "status": "review", "execution_mode": "team", "updated_at": "u2"},
             {"id": "single", "status": "review", "execution_mode": "single", "updated_at": "u3"},
             {"id": "nopr", "status": "review", "execution_mode": "team", "updated_at": "u4"}]
    cycles = {"pub": {"state": "published", "published_sha": "a" * 40}, "unpub": {"state": "qa", "published_sha": None},
              "nopr": {"state": "published", "published_sha": "b" * 40}}
    failed = [{"message": f"**team-runner**: QA-passed commit {'b' * 10} is committed locally, but opening the PR failed: push rejected"}]
    monkeypatch.setattr(p, "fetch_rows", lambda path: [{"id": "b", "goal_confirmed": True}] if path == "/api/v1/boards"
                        else failed if "/tasks/nopr/comments" in path else [] if "/comments" in path else tasks)
    calls = []

    def api(method, path, body):
        calls.append((method, path, body))
        if path.endswith("/team"):
            return {"cycles": [{"cycle": cycles[path.split("/")[-2]]}]}
        return {"id": "ap", "status": "pending"}

    monkeypatch.setattr(p, "api", api)
    p.accept_published()
    done = [c for c in calls if c[0] == "patch" and c[1].endswith("/tasks/pub")]
    assert done and done[0][2]["status"] == "done" and done[0][2]["expected_status"] == "review"
    assert not any(c[1].endswith(("/tasks/unpub", "/tasks/single", "/tasks/nopr")) for c in calls if c[0] == "patch")
    assert not any(c[0] == "post" and (c[2] or {}).get("task_id") == "nopr" for c in calls)  # a rejected push is no publication


def _world(monkeypatch, tmp_path, tasks, comments):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    monkeypatch.setattr(p, "HANDOFFS", tmp_path / "handoffs")
    calls = []

    def fetch_rows(path):
        if path == "/api/v1/boards":
            return []
        if "/comments" in path:
            notes = comments.get(path.split("/")[-2], [])
            if "markers=true" not in path:  # what the backend thread does: hides "[slug]" markers
                notes = [m for m in notes if not re.match(SYSTEM_MARKER_REGEX, m)]
            return [{"message": m} for m in notes]
        return [t for t in tasks if f"/boards/{t['board']}/" in path + "/"]

    def api(method, path, body):
        calls.append((method, path, body))
        if path.endswith("/team"):
            return {"cycles": [{"cycle": {"id": "c" * 36, "state": "blocked", "block_reason": "QA failed"},
                                "attempts": [{"stage_key": "dev", "role_kind": "specialist", "status": "failed",
                                              "provider": "omniroute-codex", "model": "gpt-6-luna", "error": "boom"}]}]}
        return {**(body or {}), "id": "new-meta", "updated_at": "u2", "status": (body or {}).get("status")}

    monkeypatch.setattr(p, "fetch_rows", fetch_rows)
    monkeypatch.setattr(p, "api", api)
    monkeypatch.setattr(p, "_auto_at", 0.0)
    return p, calls


TRIED = ["[auto-resume] cause=QA failed | resumed in place"]  # _world's latest stop
REPLANNED = "goal\n\n## Re-scope (fleet)\n\nalready replanned once"  # escalation is the next step


def test_blocked_local_publication_is_not_replayed_as_implementation(monkeypatch, tmp_path):
    task = {"id": "orig", "board": "b", "title": "Workflow", "status": "blocked",
            "execution_mode": "team", "updated_at": "u"}
    p, calls = _world(monkeypatch, tmp_path, [task], {
        "orig": ["QA-passed commit aaaaaaaaaa is committed locally, but opening the PR failed: workflow scope"]})
    original = p.api

    def api(method, path, body):
        if method == "get" and path.endswith("/team"):
            return {"cycles": [
                {"cycle": {"state": "cancelled"}},  # an old supervisor already tried to replay it
                {"cycle": {"state": "published", "published_sha": "a" * 40}}]}
        return original(method, path, body)

    monkeypatch.setattr(p, "api", api)
    p.recover_blocked()
    assert not [c for c in calls if c[0] in ("patch", "post") and c[1].endswith("/orig")]


def test_same_cause_after_resume_pauses_and_creates_one_meta_task_without_review(monkeypatch, tmp_path):
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [t], {"orig": TRIED})
    p.recover_blocked()
    assert any(m == "patch" and b.get("status") == "paused" for m, path, b in calls)
    assert not [b for m, path, b in calls if path == "/api/v1/review/items"]  # only a model escalates (2026-10-26)
    meta = [b for m, path, b in calls if path == f"/api/v1/boards/{p.META_BOARD}/tasks" and m == "post"]
    assert len(meta) == 1 and meta[0]["title"].startswith(p.META)
    assert "Original task: board=b task=orig" in meta[0]["description"] and "boom" in meta[0]["description"]
    assert "Cause: `QA failed`" in meta[0]["description"]
    assert list((tmp_path / "handoffs").glob("orig*.md"))  # handoff written to disk


def test_failure_after_meta_debug_pauses_for_the_watchdog_no_loop(monkeypatch, tmp_path):
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [t], {"orig": TRIED + ["[escalated] earlier"]})
    p.recover_blocked()
    assert not [c for c in calls if c[1] == f"/api/v1/boards/{p.META_BOARD}/tasks" and c[0] == "post"]
    assert not [c for c in calls if c[1] == "/api/v1/review/items"]
    assert any(b.get("status") == "paused" for m, path, b in calls if m == "patch")


def test_meta_task_switches_model_with_handoff_then_stops(monkeypatch, tmp_path):
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "description": "d"}
    p, calls = _world(monkeypatch, tmp_path, [meta], {"meta": TRIED})
    p.recover_blocked()
    switch = [b for m, path, b in calls if m == "patch" and b.get("harness_model")]
    assert switch and switch[0]["harness_model"] == "gpt-6.1-sol" and "## Handoff to gpt-6.1-sol" in switch[0]["description"]
    assert any(b.get("status") == "inbox" for m, path, b in calls if m == "patch")
    # two more failures on sol after the switch: the ladder is exhausted -> pause + review, no new meta task
    meta.update(harness_provider="omniroute-codex", harness_model="gpt-6.1-sol")
    p, calls = _world(monkeypatch, tmp_path, [meta], {"meta": TRIED + ["[meta-switch] x", "[auto-resume] model switch | x"] + TRIED})
    p.recover_blocked()
    assert any(b.get("status") == "paused" for m, path, b in calls if m == "patch")
    assert not [c for c in calls if c[1] == "/api/v1/review/items"]  # the watchdog takes the paused meta task
    assert not [c for c in calls if c[0] == "patch" and c[2].get("harness_model")]


def test_one_sol_failure_after_switch_is_still_retried(monkeypatch, tmp_path):
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "harness_provider": "omniroute-codex", "harness_model": "gpt-6.1-sol"}
    p, calls = _world(monkeypatch, tmp_path, [meta], {"meta": TRIED + ["[meta-switch] x", "[auto-resume] model switch | x"]})
    p.recover_blocked()
    assert [b["comment"] for m, path, b in calls if m == "patch" and b.get("status") == "inbox"][0].startswith("[auto-resume] cause=QA failed |")


def test_done_meta_task_resumes_its_original_once(monkeypatch, tmp_path):
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "done",
            "execution_mode": "team", "updated_at": "u", "description": "Original task: board=b task=orig\n"}
    orig = {"id": "orig", "board": "b", "title": "Hard", "status": "paused", "paused_from_status": "blocked", "updated_at": "u"}
    p, calls = _world(monkeypatch, tmp_path, [meta, orig], {})
    p.recover_blocked()
    statuses = [b["status"] for m, path, b in calls if m == "patch" and path.endswith("/orig")]
    assert statuses == ["blocked", "inbox"]
    p, calls = _world(monkeypatch, tmp_path, [meta, orig], {"orig": ["[meta-resumed] once"]})
    p.recover_blocked()
    assert not [c for c in calls if c[0] == "patch" and c[1].endswith("/orig")]


def test_no_change_meta_task_resumes_its_original_and_closes(monkeypatch, tmp_path):
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "description": "Original task: board=b task=orig\n"}
    orig = {"id": "orig", "board": "b", "title": "Hard", "status": "paused", "paused_from_status": "blocked", "updated_at": "u"}
    p, calls = _world(monkeypatch, tmp_path, [meta, orig], {})
    cycle = {"cycle": {"state": "blocked", "block_reason": p.NO_CHANGE_STOP + "; no automatic replay"}}
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": [cycle]} if path.endswith("/team") else
                         {**(body or {}), "updated_at": "u2", "status": (body or {}).get("status")}))
    p.recover_blocked()
    assert [b["status"] for m, path, b in calls if m == "patch" and path.endswith("/orig")] == ["blocked", "inbox"]
    assert [b["status"] for m, path, b in calls if m == "patch" and path.endswith("/meta")] == ["cancelled"]


def test_leaf_integration_conflict_replans_with_full_paths_and_coupled_owner_guidance(monkeypatch, tmp_path):
    reason = ("leaf-integration conflict; owner resolution required; stage=leaf-integration "
              "base=f2d52995207a conflicts=internal/container/deploy_test.go, "
              "internal/container/main_test.go")
    task = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "description": "goal"}
    p, calls = _world(monkeypatch, tmp_path, [task], {})
    cycle = {"cycle": {"id": "latest", "state": "blocked", "block_reason": reason}, "attempts": []}
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": [cycle]} if path.endswith("/team") else
                         {**(body or {}), "updated_at": "u2", "status": (body or {}).get("status")}))
    p.recover_blocked()
    edit = next(body for method, path, body in calls
                if method == "patch" and path.endswith("/orig") and "description" in body)
    assert reason in edit["description"]
    assert "internal/container/deploy_test.go" in edit["description"]
    assert "internal/container/main_test.go" in edit["description"]
    assert "coupled shared test bootstrap/helpers" in edit["description"]
    assert "one implementation owner" in edit["description"]
    assert not any((body or {}).get("comment", "").startswith("[auto-resume]")
                   for method, _, body in calls)
    assert any((body or {}).get("status") == "inbox" for method, _, body in calls)


def test_leaf_conflict_after_replan_escalates_without_replay(monkeypatch, tmp_path):
    reason = "leaf-integration conflict; conflicts=internal/container/main_test.go"
    task = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [task], {})
    cycle = {"cycle": {"id": "latest", "state": "blocked", "block_reason": reason}, "attempts": []}
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": [cycle]} if path.endswith("/team") else {"items": []} if method == "get" else
                         {**(body or {}), "updated_at": "u2", "status": (body or {}).get("status")}))
    p.recover_blocked()
    original_calls = [(method, body) for method, path, body in calls if path.endswith("/orig")]
    assert not any((body or {}).get("status") == "inbox" for method, body in original_calls)
    assert not any((body or {}).get("comment", "").startswith("[auto-resume]")
                   for method, body in original_calls)
    assert any((body or {}).get("status") == "paused" for method, body in original_calls)
    assert any(method == "post" and path == f"/api/v1/boards/{p.META_BOARD}/tasks"
               for method, path, _ in calls)


def test_nonretryable_latest_cycle_after_its_replan_stays_blocked_for_the_watchdog(monkeypatch, tmp_path):
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [t], {})
    reason = "stage qa: not retryable unchanged (QA cannot inspect complete required evidence)"
    cycle = {"cycle": {"id": "latest", "state": "blocked", "block_reason": reason}, "attempts": []}
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": [cycle]} if path.endswith("/team") else {"items": []} if method == "get" else {}))
    p.recover_blocked()
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("status") == "inbox"]
    assert not [c for c in calls if c[0] == "post" and c[1].endswith("/tasks")]
    assert not [c for c in calls if c[0] == "post" and c[1] == "/api/v1/review/items"]

    calls.clear()
    monkeypatch.setattr(p, "_auto_at", 0.0)
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": [cycle]} if path.endswith("/team") else
                         {"items": [{"title": "MAJOR: task needs intervention: Hard task"}]} if method == "get" else {}))
    p.recover_blocked()
    assert not [c for c in calls if c[0] == "post" and c[1] == "/api/v1/review/items"]
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("status") == "inbox"]


# The exact deterministic stop the backend emits (team_cycle.py _retry_or_block,
# deterministic_unchanged=True): replaying it cannot succeed.
NO_CHANGE_REASON = ("implementation reported the exact assigned base with no changed files; no automatic "
                    "replay. Replan as explicit mode=verify only if evidence establishes the requested "
                    "behavior already exists; otherwise the task still needs implementation")


def test_no_change_stop_never_replayed_capped_or_escalated_and_review_deduplicated(monkeypatch, tmp_path):
    """Cycle 0000a006: a no-change implementation stop must stop the pool, not burn replays."""
    t = {"id": "orig", "board": "b", "title": "Go BDD task", "status": "blocked", "execution_mode": "team",
         "max_tokens_budget": 800_000, "tokens_used": 800_000, "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [t], {})
    cycles = [
        {"cycle": {"id": "0000a006", "state": "blocked", "block_reason": NO_CHANGE_REASON}, "attempts": []},
        {"cycle": {"id": "0000a013", "state": "blocked", "block_reason": "cycle deadline exceeded"}, "attempts": []},
        {"cycle": {"id": "11111111", "state": "blocked", "block_reason": NO_CHANGE_REASON}, "attempts": []},
    ]
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": cycles} if path.endswith("/team") else {"items": []} if method == "get" else {}))
    p.recover_blocked()
    # no inbox re-admission, no pause, no token cap raise, no replacement meta task
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("status") in ("inbox", "paused")]
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("max_tokens_budget")]
    assert not [c for c in calls if c[0] == "post" and c[1].endswith("/tasks")]
    assert not [c for c in calls if c[0] == "post" and c[1] == "/api/v1/review/items"]
    calls.clear()
    monkeypatch.setattr(p, "_auto_at", 0.0)
    monkeypatch.setattr(p, "api", lambda method, path, body: calls.append((method, path, body)) or
                        ({"cycles": cycles} if path.endswith("/team") else
                         {"items": [{"title": "MAJOR: task needs intervention: Go BDD task"}]} if method == "get" else {}))
    p.recover_blocked()  # repeated sweep: the open alert dedupes; the stop still holds
    assert not [c for c in calls if c[0] == "post" and c[1] == "/api/v1/review/items"]
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("status") in ("inbox", "paused")]
    assert not [c for c in calls if c[0] == "patch" and (c[2] or {}).get("max_tokens_budget")]


def test_older_no_change_cycle_does_not_veto_latest_recoverable_cycle(monkeypatch, tmp_path):
    t = {"id": "orig", "board": "b", "title": "Go BDD task", "status": "blocked", "execution_mode": "team", "updated_at": "u"}
    p, calls = _world(monkeypatch, tmp_path, [t], {})
    cycles = [
        {"cycle": {"id": "latest", "state": "blocked", "block_reason": "QA failed"}, "attempts": []},
        {"cycle": {"id": "old", "state": "blocked", "block_reason": NO_CHANGE_REASON}, "attempts": []},
    ]
    real = p.api
    monkeypatch.setattr(p, "api", lambda method, path, body: {"cycles": cycles} if path.endswith("/team") else real(method, path, body))
    p.recover_blocked()
    assert any(m == "patch" and (b or {}).get("status") == "inbox" for m, path, b in calls)


def test_older_nonretryable_cycle_does_not_veto_latest_recoverable_cycle(monkeypatch, tmp_path):
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [t], {})
    cycles = [
        {"cycle": {"id": "latest", "state": "blocked", "block_reason": "QA failed"}, "attempts": []},
        {"cycle": {"id": "old", "state": "blocked", "block_reason": "stage dev: not retryable unchanged (old timeout)"}, "attempts": []},
    ]
    real = p.api
    monkeypatch.setattr(p, "api", lambda method, path, body: {"cycles": cycles} if path.endswith("/team") else real(method, path, body))
    p.recover_blocked()
    assert any(m == "patch" and (b or {}).get("status") == "inbox" for m, path, b in calls)


def test_each_cause_resumes_once_and_resumes_are_bounded(monkeypatch, tmp_path):
    """Owner 2026-10-25: retry is no fix. A stop resumes its cycle in place once per cause;
    a different cause gets its own resume, RESUME_MAX bounds a task that keeps finding new ones."""
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    stop = lambda r: [{"cycle": {"id": "c" * 36, "state": "blocked", "block_reason": r}, "attempts": []}]
    for notes, reason, resumed in (
            ([], "stage dev-a failed after 2 attempts (429)", True),
            (["[auto-resume] cause=stage dev-a failed after # attempts (#) | x"], "stage dev-a failed after 2 attempts (429)", False),
            (["[auto-resume] cause=stage dev-a failed after # attempts (#) | x"], "cycle deadline exceeded", True),
            ([f"[auto-resume] cause={i} | x" for i in "abcd"], "cycle deadline exceeded", False)):
        p, calls = _world(monkeypatch, tmp_path, [t], {"orig": notes})
        real = p.api
        monkeypatch.setattr(p, "api", lambda m, path, body: {"cycles": stop(reason)} if path.endswith("/team") else real(m, path, body))
        p.recover_blocked()
        statuses = [b.get("status") for m, path, b in calls if m == "patch" and path.endswith("/orig")]
        assert statuses == (["inbox"] if resumed else ["paused"]), (notes, reason)


def test_reviews_whose_task_moved_on_are_resolved(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    items = [{"id": "a", "task_status": "done"}, {"id": "b", "task_status": "paused"},
             {"id": "c", "task_status": None}, {"id": "d", "task_status": "in_progress"}]
    calls = []
    monkeypatch.setattr(p, "api", lambda m, path, body: items if m == "get" else calls.append((path, body)))
    p.settle_reviews()
    assert [c[0].split("/")[-1] for c in calls] == ["a", "d"] and calls[0][1]["action"] == "resolve"


def test_same_cause_joins_the_open_meta_debug_task_and_resumes_with_it(monkeypatch, tmp_path):
    """2026-10-24: one backlog parser bug produced eight meta-debug tasks; one per cause now."""
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "in_progress",
            "execution_mode": "team", "updated_at": "u", "description": "Cause: `QA failed`\n\nOriginal task: board=b task=first\n"}
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team", "updated_at": "u", "description": REPLANNED}
    p, calls = _world(monkeypatch, tmp_path, [meta, t], {"orig": TRIED})
    p.recover_blocked()
    assert not [c for c in calls if c[1] == f"/api/v1/boards/{p.META_BOARD}/tasks" and c[0] == "post"]
    also, = [b for m, path, b in calls if path.endswith("/tasks/meta/comments")]
    assert also["message"].startswith("[meta-also] board=b task=orig")
    assert any(b.get("status") == "paused" for m, path, b in calls if m == "patch" and path.endswith("/orig"))
    meta["status"], t["status"], t["paused_from_status"] = "done", "paused", "blocked"
    p, calls = _world(monkeypatch, tmp_path, [meta, t], {"meta": [also["message"]]})
    p.recover_blocked()
    assert [b["status"] for m, path, b in calls if m == "patch" and path.endswith("/orig")] == ["blocked", "inbox"]


def test_cap_stop_after_earlier_failures_gets_cap_raise_not_escalation(monkeypatch, tmp_path):
    """0000a008, 2026-10-22: three blocked cycles were already counted, so its cap stop after
    meta-debug escalated instead of getting a raise and every re-admission re-blocked on the cap."""
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team",
         "max_tokens_budget": 800, "tokens_used": 1042, "updated_at": "u"}
    p, calls = _world(monkeypatch, tmp_path, [t], {"orig": TRIED + ["[escalated] x", "[meta-resumed] y"]})
    real = p.api
    monkeypatch.setattr(p, "api", lambda m, path, body: {"cycles": [{"cycle": {"id": str(i) * 36, "state": "blocked"}, "attempts": []} for i in range(3)]}
                        if path.endswith("/team") else real(m, path, body))
    p.recover_blocked()
    assert not [c for c in calls if c[1] == "/api/v1/review/items"]
    patches = [b for m, path, b in calls if m == "patch" and path.endswith("/orig")]
    assert patches[0]["max_tokens_budget"] == 1042 + p.CAP_STEP and patches[1]["status"] == "inbox"


def test_newer_inbox_task_for_already_covered_issues_is_cancelled(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    tasks = {"done": [{"id": "old", "status": "done", "created_at": "1", "description": "GitHub issues: #247"}],
             "inbox": [{"id": "dup", "status": "inbox", "created_at": "2", "updated_at": "u", "description": "GitHub issues: #247"},
                       {"id": "new", "status": "inbox", "created_at": "3", "updated_at": "u", "description": "GitHub issues: #247, #300"},
                       # 2026-10-26: a split's parts (and their parts) repeat the parent's issues; not duplicates
                       {"id": "part", "status": "inbox", "created_at": "4", "updated_at": "u", "parent_task_id": "old",
                        "description": "GitHub issues: #247"},
                       {"id": "subpart", "status": "inbox", "created_at": "5", "updated_at": "u", "parent_task_id": "part",
                        "description": "GitHub issues: #247"}]}
    monkeypatch.setattr(p, "fetch_rows", lambda path: next((v for k, v in tasks.items() if f"status={k}" in path), []))
    calls = []
    monkeypatch.setattr(p, "api", lambda m, path, body: calls.append((path.split("/")[-1], body)) or {})
    p.cancel_duplicates("b")
    assert [c[0] for c in calls] == ["dup"] and calls[0][1]["status"] == "cancelled"  # 'new' still adds #300


def test_pool_reads_its_own_hidden_markers_so_meta_debug_does_not_loop(monkeypatch, tmp_path):
    """0000a015, 2026-10-22: the comment thread hid [escalated]/[meta-resumed], so every
    sweep re-resumed the task and every cap stop opened another meta-debug task (four)."""
    meta = {"id": "meta", "board": "00000000-0000-4000-8000-0000000000aa", "title": "[meta-debug] x", "status": "done",
            "execution_mode": "team", "updated_at": "u", "description": "Original task: board=b task=orig\n"}
    orig = {"id": "orig", "board": "b", "title": "Hard", "status": "blocked", "execution_mode": "team",
            "max_tokens_budget": 100, "tokens_used": 150, "updated_at": "u", "description": REPLANNED}
    notes = ["[auto-recovery:cap] a", "[auto-recovery:cap] b", "[escalated] failed three times", "[meta-resumed] once"]
    p, calls = _world(monkeypatch, tmp_path, [meta, orig], {"orig": notes})
    p.recover_blocked()
    assert not [c for c in calls if c[1] == f"/api/v1/boards/{p.META_BOARD}/tasks" and c[0] == "post"]
    assert not [b for m, path, b in calls if path == "/api/v1/review/items"]
    orig["status"] = "paused"
    p, calls = _world(monkeypatch, tmp_path, [meta, orig], {"orig": notes})
    p.recover_blocked()
    assert not [c for c in calls if c[0] == "patch" and c[1].endswith("/orig")]


def test_dependencies_on_cancelled_tasks_are_released(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    rows = {"cancelled": [{"id": "gone"}], "inbox": [{"id": "waits", "blocked_by_task_ids": ["gone", "live"]},
                                                      {"id": "fine", "blocked_by_task_ids": ["live"]}]}
    monkeypatch.setattr(p, "fetch_rows", lambda path: next((v for k, v in rows.items() if f"status={k}" in path), []))
    calls = []
    monkeypatch.setattr(p, "api", lambda m, path, body: calls.append((path.split("/")[-1], body)) or {})
    p.release_cancelled_dependencies("b")
    assert calls == [("waits", {"depends_on_task_ids": ["live"], "comment": "Dropped dependencies on cancelled tasks."})]


def _handoff_attempt(stage, role, report, *, round=1, attempt_no=1, **fields):
    return {
        "stage_key": stage, "role_kind": role, "artifact": report,
        "round": round, "attempt_no": attempt_no, "status": "succeeded",
        "verdict": "fail" if role == "qa" else None,
        "provider": "offline", "model": "test", "thinking_level": "high",
        "input_tokens": 10, "output_tokens": 20,
        "finished_at": f"2026-10-22T12:{round:02d}:{attempt_no:02d}",
        **fields,
    }


def _handoff_cycle(cid, attempts, reason="QA failed in round 2/2"):
    # Match team_cycle.view/_attempts, not completion-time order.
    return {
        "cycle": {"id": cid, "state": "blocked", "block_reason": reason},
        "attempts": sorted(attempts, key=lambda a: (a["round"], a["stage_key"], a["attempt_no"])),
    }


def _escalation_evidence(monkeypatch, tmp_path, cycles, route="original", unavailable=False):
    task = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked",
            "execution_mode": "team", "updated_at": "u", "tokens_used": 42}
    if route != "original":
        task.update(title="[meta-debug] Hard task", description="Existing owner goal.")
    if route == "exhausted":
        task.update(harness_provider="omniroute-codex", harness_model="gpt-6.1-sol")
    p, calls = _world(monkeypatch, tmp_path, [task], {})
    original_api = p.api

    def api(method, path, body):
        if path.endswith("/team"):
            assert method == "get" and path == "/api/v1/boards/b/tasks/orig/team"
            if unavailable:
                raise RuntimeError("offline team view unavailable")
            return {"cycles": cycles}
        return original_api(method, path, body)

    monkeypatch.setattr(p, "api", api)
    p.escalate("b", task, [])
    saved, = (tmp_path / "handoffs").glob("orig*.md")
    md = saved.read_text(encoding="utf-8")
    if route == "original":
        description, = [body["description"] for method, path, body in calls
                        if method == "post" and path == f"/api/v1/boards/{p.META_BOARD}/tasks"]
        assert description.endswith(md)
    elif route == "switch":
        description, = [body["description"] for method, path, body in calls
                        if method == "patch" and body.get("harness_model")]
        assert description.startswith("Existing owner goal.\n\n## Handoff to gpt-6.1-sol")
        assert description.endswith(md)
    assert not [c for c in calls if c[0] == "post" and c[1] == "/api/v1/review/items"]  # the file is the handoff
    assert len(md.encode("utf-8")) <= p.HANDOFF_MAX_BYTES == 9_000
    return md


@pytest.mark.parametrize("route", ["original", "switch", "exhausted"])
def test_handoff_retains_fitting_implementation_and_qa_diagnosis_before_long_log(
    monkeypatch, tmp_path, route
):
    implementation = "Implementation rationale: preserve the owner boundary.\n" + "detail " * 440
    diagnosis = "ROOT FINDING: the fitting implementation report was dropped by the handoff budget."
    qa = ("Review context.\n" * 100 + diagnosis + "\n```text\n"
          + "routine QA log\n" * 2400 + "```\nVERDICT: FAIL")
    cycle = _handoff_cycle("newest01", [
        _handoff_attempt("dev", "specialist", implementation),
        _handoff_attempt("qa", "qa", qa),
    ])
    md = _escalation_evidence(monkeypatch, tmp_path, [cycle], route)
    assert implementation in md
    assert diagnosis in md and "VERDICT: FAIL" in md
    assert "execution=succeeded; QA verdict=FAIL" in md
    assert "specialist report — dev, round 1, attempt 1; execution=succeeded" in md
    assert "QA failed in round 2/2" in md
    assert "shortened from" in md and "1 shortened" in md
    assert "implementation execution failure" not in md.lower()


@pytest.mark.parametrize("route", ["original", "switch"])
def test_handoff_preserves_failed_no_change_rationale_and_every_worker(
    monkeypatch, tmp_path, route
):
    reports = [
        _handoff_attempt(
            "dev-a", "specialist", "WORKER A rationale: no change needed.\n" + "a" * 18000,
            status="failed", error="developer must commit a change (head_sha differs from base)",
        ),
        _handoff_attempt(
            "dev-b", "developer",
            "WORKER B rationale: scope requires an owner decision.\n" + "b" * 18000,
            status="failed", error="worker stopped",
        ),
        _handoff_attempt("qa", "qa", "QA DIAGNOSIS: evidence is insufficient.\n" + "q" * 18000),
    ]
    md = _escalation_evidence(monkeypatch, tmp_path,
                              [_handoff_cycle("nochange", reports, NO_CHANGE_REASON)], route)
    assert NO_CHANGE_REASON in md
    assert "developer must commit a change (head_sha differs from base)" in md
    assert "WORKER A rationale: no change needed." in md
    assert "WORKER B rationale: scope requires an owner decision." in md
    assert "QA DIAGNOSIS: evidence is insufficient." in md
    assert "execution=failed" in md and "execution=succeeded; QA verdict=FAIL" in md
    assert "3 shortened" in md


def test_handoff_uses_round_retry_and_completion_not_backend_stage_order(monkeypatch, tmp_path):
    attempts = [
        _handoff_attempt(
            "dev-a", "specialist", "ROUND ONE failed developer rationale", status="failed",
        ),
        _handoff_attempt("dev-a", "specialist", "ROUND TWO first try", round=2, status="failed"),
        _handoff_attempt("dev-a", "specialist", "ROUND TWO retry rationale", round=2, attempt_no=2),
        _handoff_attempt("dev-z", "specialist", "OTHER WORKER still contributes from round one"),
        _handoff_attempt("qa", "qa", "ROUND ONE QA diagnosis"),
        _handoff_attempt("qa-a", "qa", "LATEST QA DIAGNOSIS", round=2, attempt_no=2,
                         finished_at="2026-10-22T13:00:00"),
        _handoff_attempt("qa-z", "qa", "EARLIER QA DIAGNOSIS despite later stage key", round=2,
                         finished_at="2026-10-22T12:00:00"),
    ]
    cycle = _handoff_cycle("retries1", attempts)
    assert cycle["attempts"][-1]["artifact"].startswith("EARLIER QA")
    md = _escalation_evidence(monkeypatch, tmp_path, [cycle], "switch")
    for attempt in attempts:
        assert attempt["artifact"] in md  # all fitting reports, including failed retries, survive
    latest = md.split("Latest QA report", 1)[1].split("###", 1)[0]
    assert "LATEST QA DIAGNOSIS" in latest and "EARLIER QA DIAGNOSIS" not in latest
    assert md.index("ROUND TWO retry rationale") < md.index("ROUND TWO first try")
    assert "0 diagnostic report(s) omitted; 0 shortened" in md


@pytest.mark.parametrize("route", ["original", "switch", "exhausted"])
def test_handoff_spends_budget_on_newest_diagnostics_before_older_reports_and_metadata(
    monkeypatch, tmp_path, route
):
    newest = _handoff_cycle("newest01", [
        _handoff_attempt("dev", "specialist", "NEW IMPLEMENTATION rationale fits in full."),
        _handoff_attempt(
            "qa", "qa", "NEW QA DIAGNOSIS at the beginning.\n" + "x" * 40000 + "\nVERDICT: FAIL",
        ),
        *[_handoff_attempt(f"pm-{i}", "pm", "planning " * 2000) for i in range(35)],
    ], "newest cycle block reason")
    older = _handoff_cycle("older001", [
        _handoff_attempt("dev", "developer", "OLDER IMPLEMENTATION CONTENT " * 500),
        _handoff_attempt("qa", "qa", "OLDER QA CONTENT " * 500),
    ], "older cycle block reason")
    md = _escalation_evidence(monkeypatch, tmp_path, [newest, older], route)
    assert "NEW IMPLEMENTATION rationale fits in full." in md
    assert "NEW QA DIAGNOSIS at the beginning." in md
    assert "VERDICT: FAIL" in md
    assert "newest cycle block reason" in md and "older cycle block reason" in md
    assert "OLDER IMPLEMENTATION CONTENT" not in md and "OLDER QA CONTENT" not in md
    assert "Cycle older001: 2 diagnostic report(s) omitted" in md
    assert "routine attempt summary(s) omitted" in md
    assert "planning planning" not in md


@pytest.mark.parametrize("stage", ["pm", "module-manager"])
def test_handoff_includes_failed_planning_artifact_and_error_but_not_successful_plan(
    monkeypatch, tmp_path, stage
):
    report = _handoff_attempt(stage, "pm", "PLANNING ARTIFACT: invalid backlog response", status="failed",
                              error="backlog plan must be a JSON object with a subtasks list")
    routine = _handoff_attempt("pm", "pm", "successful routine plan")
    md = _escalation_evidence(monkeypatch, tmp_path,
                              [_handoff_cycle("planfail", [report, routine])])
    assert "PLANNING ARTIFACT: invalid backlog response" in md
    assert "backlog plan must be a JSON object with a subtasks list" in md
    assert "successful routine plan" not in md
    assert "Cycle planfail: 0 diagnostic report(s) omitted" in md


def test_handoff_prioritizes_latest_round_and_retry_over_backend_sorted_history(
    monkeypatch, tmp_path
):
    attempts = [
        _handoff_attempt("dev-a", "specialist", "OBSOLETE ROUND ONE implementation " * 2000),
        _handoff_attempt(
            "dev-a", "specialist", "OBSOLETE FIRST RETRY " * 2000, round=2, status="failed",
        ),
        _handoff_attempt(
            "dev-a", "specialist", "LATEST RETRY fitting rationale.", round=2, attempt_no=2,
        ),
        _handoff_attempt("dev-z", "specialist", "WORKER Z still contributes from round one."),
        _handoff_attempt("qa", "qa", "OBSOLETE ROUND ONE QA " * 2000),
        _handoff_attempt("qa", "qa", "LATEST ROUND TWO DIAGNOSIS.\n" + "q" * 40000,
                         round=2, attempt_no=2),
        _handoff_attempt("qa", "qa", "", round=2, attempt_no=3, status="failed", verdict=None,
                         error="later QA execution transport error"),
    ]
    md = _escalation_evidence(
        monkeypatch, tmp_path, [_handoff_cycle("retrynew", attempts)], "switch"
    )
    assert "LATEST RETRY fitting rationale." in md
    assert "WORKER Z still contributes from round one." in md
    assert "LATEST ROUND TWO DIAGNOSIS." in md
    assert "OBSOLETE ROUND ONE" not in md and "OBSOLETE FIRST RETRY" not in md
    assert "diagnostic report(s) omitted" in md


def _assert_closed_report_fences(md):
    fence = None
    for line in md.splitlines():
        if fence is None:
            if re.fullmatch(r"`{3,}|~{3,}", line):
                fence = line
        elif re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + ",}", line):
            fence = None
    assert fence is None


@pytest.mark.parametrize("route", ["original", "switch"])
def test_handoff_utf8_budget_and_worker_markdown_fences(monkeypatch, tmp_path, route):
    fitting = "Unicode rationale: café 漢字 🐎\n```python\nassert True\n```\n~~~text\nnested\n~~~"
    long_qa = "多言語 QA diagnosis 🐎\n````\n" + "漢🐎é\n" * 8000 + "\n~~~~\nVERDICT: FAIL"
    md = _escalation_evidence(monkeypatch, tmp_path, [_handoff_cycle("unicode1", [
        _handoff_attempt("dev", "developer", fitting),
        _handoff_attempt("qa", "qa", long_qa),
    ])], route)
    assert fitting in md
    assert "多言語 QA diagnosis 🐎" in md and "VERDICT: FAIL" in md
    assert "\ufffd" not in md and "shortened from" in md
    _assert_closed_report_fences(md)


@pytest.mark.parametrize("route", ["original", "switch", "exhausted"])
def test_handoff_reports_unavailable_team_evidence_in_every_route(monkeypatch, tmp_path, route):
    md = _escalation_evidence(monkeypatch, tmp_path, [], route, unavailable=True)
    assert "Team evidence unavailable" in md
    assert "no cycles" not in md and "## Cycle" not in md


def test_handoff_distinguishes_empty_team_view_from_unavailable_evidence(monkeypatch, tmp_path):
    md = _escalation_evidence(monkeypatch, tmp_path, [])
    assert "Team view returned no cycles." in md
    assert "evidence unavailable" not in md


@pytest.mark.parametrize("notes, reason", [
    (TRIED, "QA failed"),  # the same cause again
    ([], "stage dev: not retryable unchanged (evidence unavailable: x)"),
    ([], NO_CHANGE_REASON),
])
def test_a_stuck_task_is_replanned_once_instead_of_paused(monkeypatch, tmp_path, notes, reason):
    """Owner 2026-10-26: no fleet task waits on the owner while a replan can help. Every paused
    task had stopped on a big plan; a fresh plan told why splits it into child tasks."""
    t = {"id": "orig", "board": "b", "title": "Hard task", "status": "blocked", "execution_mode": "team",
         "updated_at": "u", "description": "goal", "max_tokens_budget": 800_000, "tokens_used": 500_000}
    p, calls = _world(monkeypatch, tmp_path, [t], {"orig": notes})
    real = p.api
    stop = [{"cycle": {"id": "c" * 36, "state": "blocked", "block_reason": reason}, "attempts": []}]
    monkeypatch.setattr(p, "api", lambda m, path, body: {"cycles": stop} if path.endswith("/team") else real(m, path, body))
    p.recover_blocked()
    cap, edit, admit = [b for m, path, b in calls if m == "patch" and path.endswith("/orig")]
    assert set(cap) == {"max_tokens_budget", "expected_status", "expected_updated_at", "comment"}  # API: alone
    assert cap["max_tokens_budget"] == 500_000 + p.CAP_STEP and not cap["comment"].startswith("[auto-recovery:cap]")
    assert edit["description"].startswith("goal\n\n## Re-scope (fleet)") and "Split the work" in edit["description"]
    assert "max_tokens_budget" not in edit and edit["comment"].startswith("[auto-rescope]")
    assert admit["status"] == "inbox" and admit["expected_updated_at"] == "u2"
    assert not [c for c in calls if c[1] == "/api/v1/review/items" or c[1].endswith(f"{p.META_BOARD}/tasks")]


def test_split_parents_close_when_every_child_settled(monkeypatch, tmp_path):
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    rows = [{"id": "lead", "status": "in_progress", "execution_mode": "team", "split_count": 2, "updated_at": "u"},
            {"id": "busy", "status": "in_progress", "execution_mode": "team", "split_count": 1, "updated_at": "u"},
            {"id": "a", "status": "done", "parent_task_id": "lead"}, {"id": "b", "status": "cancelled", "parent_task_id": "lead"},
            {"id": "c", "status": "paused", "parent_task_id": "busy"}]
    monkeypatch.setattr(p, "fetch_rows", lambda path: [r for r in rows if path.endswith(f"status={r['status']}")])
    calls = []
    monkeypatch.setattr(p, "api", lambda m, path, body: calls.append((m, path, body)) or {**(body or {}), "id": "ap", "updated_at": "u2"})
    p.settle_parents("b")
    moves = [(path.split("/")[-1], b["status"]) for m, path, b in calls if m == "patch" and "/tasks/" in path]
    assert moves == [("lead", "review"), ("lead", "done")]
    assert "1/2 child tasks done" in calls[-1][2]["comment"]
    rows[2]["status"] = "cancelled"  # every part cancelled: nothing was delivered, never Done
    calls.clear()
    p.settle_parents("b")
    assert not calls


def test_done_split_parent_reopens_when_a_child_is_open_again(monkeypatch, tmp_path):
    # 0000a00e was reopened under its Done lead 0000a00b; every cycle start 409'd "parent is
    # absent or no longer in progress" until the pool escalated it.
    p = load(monkeypatch, tmp_path)
    monkeypatch.setattr(p, "log", lambda message: None)
    rows = [{"id": "lead", "status": "done", "execution_mode": "team", "split_count": 2, "updated_at": "u"},
            {"id": "closed", "status": "done", "execution_mode": "team", "split_count": 1, "updated_at": "u"},
            {"id": "a", "status": "done", "parent_task_id": "lead"}, {"id": "b", "status": "inbox", "parent_task_id": "lead"},
            {"id": "c", "status": "done", "parent_task_id": "closed"}]
    monkeypatch.setattr(p, "fetch_rows", lambda path: [r for r in rows if path.endswith(f"status={r['status']}")])
    calls = []
    monkeypatch.setattr(p, "api", lambda m, path, body: calls.append((m, path, body)) or {**(body or {}), "updated_at": "u2"})
    p.settle_parents("b")
    moves = [(path.split("/")[-1], b["status"], b["expected_updated_at"]) for m, path, b in calls]
    assert moves == [("lead", "inbox", "u"), ("lead", "in_progress", "u2")]


def test_backend_restart_is_one_clean_scan_line_per_poll(monkeypatch, tmp_path):
    # 2026-10-28 19:41: a deploy restart dropped mc's connection; mc printed a traceback and the
    # pool logged its first 200 chars once per board, so one 10 s blip read as a repeating error.
    import socket
    import subprocess
    import sys
    import threading

    srv = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=lambda: [srv.accept()[0].close() for _ in range(1)], daemon=True).start()
    mc = subprocess.run([sys.executable, str(POOL.parent / "mc"), "get", "/api/v1/boards"],
                        capture_output=True, text=True, timeout=30,
                        env={"MC_URL": f"http://127.0.0.1:{srv.getsockname()[1]}", "MC_TOKEN": "t"})
    assert mc.returncode == 3 and mc.stderr.startswith("mc: cannot reach") and "Traceback" not in mc.stderr

    p = load(monkeypatch, tmp_path)
    logged = []
    monkeypatch.setattr(p, "log", logged.append)
    rc = {"code": 3}
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, rc["code"], "", "Traceback (most recent call last):\n  ...\nmc: cannot reach x\n"))
    assert [p.fetch_rows(f"/api/v1/boards/{b}/tasks") for b in "abcde"] == [[]] * 5
    assert logged == ["board scan failed: mc: cannot reach x"]
    rc["code"] = 2  # an HTTP error is per path: every one is reported
    p.fetch_rows("/x"), p.fetch_rows("/y")
    assert len(logged) == 3
