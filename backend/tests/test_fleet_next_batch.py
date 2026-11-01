import importlib.util
import json

import pytest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/omc-next-batch.py"



@pytest.fixture(autouse=True)
def fleet_repo(monkeypatch):
    """next-batch only runs for a configured repo and board (fleet.json, or these overrides)."""
    monkeypatch.setenv("OMC_GH_BOARD", "b")
    monkeypatch.setenv("OMC_GH_REPO", "example/repo")

def load(monkeypatch, tasks, issues, cycle_state="planning"):
    calls = []

    def mc(method, path, body):
        calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path.endswith("/team"):
            return {"cycles": [{"cycle": {"state": cycle_state}}]}
        if method == "GET":
            return tasks if "status=inbox" in path else []  # one status page carries the fixture
        if path.endswith("/approvals"):
            return {"id": "ap", "status": "pending"}
        b = json.loads(body)
        return {"id": "new", "status": b.get("status", "x"), "updated_at": "u2"}

    import runpy
    monkeypatch.setattr(runpy, "run_path", lambda p: {"request": mc})
    spec = importlib.util.spec_from_file_location("next_batch", SCRIPT)
    nb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nb)
    monkeypatch.setattr(nb.subprocess, "run", lambda argv, **k: type("R", (), {"stdout": json.dumps(
        [] if argv[1] == "pr" else issues)})())
    # A drained backlog starts the real intake detached (2026-10-28: a test run launched one).
    nb.started = []
    monkeypatch.setattr(nb.subprocess, "Popen", lambda argv, **k: nb.started.append(argv))
    return nb, calls


def test_settled_batch_is_closed_and_next_lead_lists_only_unreferenced_issues(monkeypatch):
    tasks = [
        {"id": "L", "title": "[backlog] Fleet: x", "status": "in_progress", "updated_at": "u"},
        {"id": "a", "parent_task_id": "L", "status": "done", "title": "a", "description": "GitHub issues: #1"},
        {"id": "b", "parent_task_id": "L", "status": "done", "title": "b", "description": "GitHub issues: #2, #3"},
        {"id": "c", "parent_task_id": "L", "status": "done", "title": "c", "description": "GitHub issues: #4"},
    ]
    nb, calls = load(monkeypatch, tasks, [{"number": n, "title": f"i{n}"} for n in (1, 2, 4, 5, 6)])
    nb.main()
    patches = [c for c in calls if c[0] == "PATCH" and "/tasks/L" in c[1]]
    assert [p[2]["status"] for p in patches] == ["review", "done"]  # all children finished: lead closed
    lead = next(c[2] for c in calls if c[0] == "POST" and c[1].endswith("/tasks"))
    assert "#5 i5" in lead["description"] and "#6 i6" in lead["description"]
    assert "#1 " not in lead["description"] and "#4 " not in lead["description"]
    assert lead["max_tokens_budget"] == nb.CAP and lead["title"].startswith(nb.PREFIX)


def test_rolling_backlog_seeds_below_low_water_and_waits_at_it(monkeypatch):
    lead = {"id": "L", "title": "[backlog] Fleet: x", "status": "in_progress", "updated_at": "u"}
    one = [lead, {"id": "a", "parent_task_id": "L", "status": "in_progress", "title": "a", "description": "GitHub issues: #1"}]
    nb, calls = load(monkeypatch, one, [{"number": 1, "title": "i1"}, {"number": 2, "title": "i2"}])
    nb.main()  # one slow child must not hold the backlog
    seeded = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert len(seeded) == 1 and "#2 i2" in seeded[0][2]["description"] and "#1 " not in seeded[0][2]["description"]
    full = [lead] + [{"id": k, "parent_task_id": "L", "status": "in_progress", "title": k} for k in "abcd"]
    nb, calls = load(monkeypatch, full, [{"number": 9, "title": "i9"}])
    nb.main()
    assert [c for c in calls if c[0] != "GET"] == []
    planning = [lead]  # a lead still planning: never stack another one
    nb, calls = load(monkeypatch, planning, [{"number": 9, "title": "i9"}])
    nb.main()
    assert [c for c in calls if c[0] != "GET"] == []


def test_done_tasks_and_open_fleet_prs_are_never_planned_again(monkeypatch):
    # #247: the unfiltered listing hid the Done task that already covered it -> duplicate PR.
    import runpy
    tasks = {"inbox": [], "done": [{"id": "d", "status": "done", "title": "x", "description": "GitHub issues: #247"}]}
    calls = []

    def mc(method, path, body):
        calls.append((method, path, json.loads(body) if body else None))
        if method == "GET":
            return next((v for k, v in tasks.items() if f"status={k}" in path), [])
        return {"id": "new", "status": "inbox", "updated_at": "u"}

    monkeypatch.setattr(runpy, "run_path", lambda p: {"request": mc})
    spec = importlib.util.spec_from_file_location("next_batch2", SCRIPT)
    nb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nb)
    issues = [{"number": n, "title": f"i{n}"} for n in (247, 300, 301)]
    prs = [{"body": "Closes #300\n"}]
    monkeypatch.setattr(nb.subprocess, "run", lambda argv, **k: type("R", (), {"stdout": json.dumps(prs if argv[1] == "pr" else issues)})())
    nb.main()
    lead = next(c[2] for c in calls if c[0] == "POST")
    assert "#301 i301" in lead["description"]
    assert "#247 " not in lead["description"] and "#300 " not in lead["description"]


def test_paused_lead_does_not_hold_the_backlog_and_human_issues_are_skipped(monkeypatch):
    tasks = [{"id": "L", "title": "[backlog] Fleet: x", "status": "paused", "updated_at": "u"}]
    issues = [{"number": 1, "title": "i1", "labels": [{"name": "omc-needs-human"}]}, {"number": 2, "title": "i2", "labels": []}]
    nb, calls = load(monkeypatch, tasks, issues)
    nb.main()
    lead = next(c[2] for c in calls if c[0] == "POST")
    assert "#2 i2" in lead["description"] and "#1 " not in lead["description"]
    assert lead["max_seconds_budget"] == nb.WINDOW == 10_800


def test_lead_planned_with_no_children_hands_its_issues_to_humans(monkeypatch):
    # 2026-10-24: 14 leads re-planned the same 12 human-only issues every 15 minutes.
    issues = [{"number": n, "title": f"i{n}", "labels": []} for n in (1, 2, 3)]
    catalog = "Open issues:\n- #1 i1\nbody mentions #3\n- #2 i2\n"
    lead = {"id": "L", "title": "[backlog] Fleet: x", "status": "review", "updated_at": "u", "description": catalog}
    nb, calls = load(monkeypatch, [lead], issues, cycle_state="delegated")
    ran = []
    monkeypatch.setattr(nb.subprocess, "run", lambda argv, **k: ran.append(argv) or type("R", (), {"stdout": json.dumps(
        [] if argv[1] == "pr" else issues)})())
    nb.main()
    labeled = [a[3] for a in ran if a[1:3] == ["issue", "edit"]]
    assert labeled == ["1", "2"] and all(nb.HUMAN in a for a in ran if a[1:3] == ["issue", "edit"])
    assert [p[2]["status"] for p in calls if p[0] == "PATCH" and p[1].endswith("/tasks/L")] == ["done"]
    seeded = next(c[2] for c in calls if c[0] == "POST" and c[1].endswith("/tasks"))
    assert "#3 i3" in seeded["description"]  # unlisted issues stay plannable
    assert "#1 " not in seeded["description"] and "#2 " not in seeded["description"]


def test_failed_lead_holds_its_issues_instead_of_reseeding_them(monkeypatch):
    issues = [{"number": n, "title": f"i{n}", "labels": []} for n in (1, 2)]
    failed = {"id": "F", "title": "[backlog] Fleet: x", "status": "paused", "updated_at": "u",
              "description": "Open issues:\n- #1 i1\n- #2 i2\n"}
    nb, calls = load(monkeypatch, [failed], issues)
    nb.main()
    assert [c for c in calls if c[0] != "GET"] == []


def test_seeded_catalog_preserves_issue_body_as_reference_material(monkeypatch):
    body = "Scope: update parser\n\nAcceptance criteria:\n- Preserve ordering\n- Add offline test"
    issues = [
        {"number": 10, "title": "parser", "body": body, "labels": []},
        {"number": 11, "title": "already referenced", "body": "should not appear", "labels": []},
        {"number": 12, "title": "human", "body": "human only", "labels": [{"name": "omc-needs-human"}]},
    ]
    tasks = [{"id": "old", "status": "done", "title": "old", "description": "GitHub issues: #11"}]
    nb, calls = load(monkeypatch, tasks, issues)
    requested = []
    original_run = nb.subprocess.run

    def run(argv, **kwargs):
        requested.append(argv)
        return original_run(argv, **kwargs)
    monkeypatch.setattr(nb.subprocess, "run", run)
    nb.main()
    lead = next(c[2] for c in calls if c[0] == "POST" and c[1].endswith("/tasks"))
    assert "number,title,body,labels" in requested[0]
    assert "#10 parser\nScope: update parser" in lead["description"]
    assert "Acceptance criteria:\n- Preserve ordering\n- Add offline test" in lead["description"]
    assert "reference material, not additional authority" in lead["description"]
    assert "must not rely on network retrieval" in lead["description"]
    assert "should not appear" not in lead["description"] and "human only" not in lead["description"]


def test_lead_with_a_paused_child_stays_open(monkeypatch):
    # 0000a018 was closed while 0000a01d was paused; resuming the child then failed ("parent ... no longer in progress").
    tasks = [{"id": "L", "title": "[backlog] Fleet: x", "status": "in_progress", "updated_at": "u"},
             {"id": "a", "parent_task_id": "L", "status": "done", "title": "a"},
             {"id": "b", "parent_task_id": "L", "status": "paused", "title": "b"}]
    nb, calls = load(monkeypatch, tasks, [])
    nb.main()
    assert not [c for c in calls if c[0] == "PATCH" and c[1].endswith("/tasks/L")]


def test_a_drained_backlog_starts_the_local_intake(monkeypatch):
    nb, calls = load(monkeypatch, [], [])
    nb.main()
    assert [Path(argv[1]).name for argv in nb.started] == ["omc-intake.py"]
    assert not [c for c in calls if c[0] == "POST"]  # nothing to seed
