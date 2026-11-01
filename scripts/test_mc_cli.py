#!/usr/bin/env python3
"""Self-check for the mc CLI. No framework, no fixtures: python3 scripts/test_mc_cli.py"""

import importlib.machinery
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_loader(
    "mc", importlib.machinery.SourceFileLoader("mc", str(Path(__file__).with_name("mc")))
)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


# rows_of: the shapes the API actually returns
assert mc.rows_of([{"id": 1}]) == [{"id": 1}]
assert mc.rows_of({"items": [{"id": 1}]}) == [{"id": 1}]
assert mc.rows_of({"agents": [{"id": 2}]}) == [{"id": 2}]
assert mc.rows_of({"ok": True}) is None, "a non-collection dict must not be forced into a table"
assert mc.rows_of("plain text") is None

# render: table path picks identifying columns and counts rows
out = capture(mc.render, [{"id": "a1", "name": "scout", "extra": "ignored"}], False)
assert "ID" in out and "NAME" in out, out
assert "scout" in out and "1 row(s)" in out, out

# render: long values truncate rather than wrecking the table
out = capture(mc.render, [{"id": "x" * 100}], False)
assert "..." in out and max(len(l) for l in out.splitlines()) < 60, out

# render: nested values are JSON-encoded, not Python-repr'd
out = capture(mc.render, [{"id": 1, "name": {"a": 1}}], False)
assert '{"a": 1}' in out, out

# render: --json passes through untouched
out = capture(mc.render, {"ok": True}, True)
assert json.loads(out) == {"ok": True}

# render: empty and non-dict rows don't crash
assert "(no rows)" in capture(mc.render, [], False)
assert "abc" in capture(mc.render, ["abc"], False)

# arg parsing: --key value becomes a query param, not a positional
seen = {}
mc.request = lambda m, p, b=None, q=None: seen.update(method=m, path=p, body=b, query=q) or []
mc.main(["get", "/api/v1/agents", "--limit", "5"])
assert seen["query"] == {"limit": "5"}, seen
assert seen["path"] == "/api/v1/agents" and seen["method"] == "GET", seen

# tasks without --board must list boards, not build a broken /boards/None/tasks URL
seen.clear()
mc.main(["tasks"])
assert seen["path"] == "/api/v1/boards", seen
seen.clear()
mc.main(["tasks", "--board", "b7"])
assert seen["path"] == "/api/v1/boards/b7/tasks", seen

# aliases resolve
seen.clear()
mc.main(["ranking"])
assert seen["path"] == "/api/v1/ranking", seen

# unknown command exits non-zero rather than silently doing nothing
try:
    mc.main(["nonsense"])
    raise AssertionError("unknown command should exit")
except SystemExit as e:
    assert e.code == 1

print("all mc CLI checks passed")

# --- receipt --------------------------------------------------------------
# A failed sub-resource fetch must read "unavailable", never be reported as 0.
calls = []


def fake_request(method, path, body=None, query=None):
    calls.append(path)
    if path.endswith("/tasks"):
        return [{"id": "t1", "title": "Ship it", "status": "done", "created_at": "2026-01-01"}]
    if path.endswith("/activity-log"):
        assert query and query.get("limit") == 200, f"must cap at 200, got {query}"
        return {"items": [{"ts": f"2026-01-0{i}", "event_type": "x", "message": "m"} for i in range(1, 4)]}
    if path.endswith("/comments"):
        raise SystemExit(2)          # endpoint refuses -> unavailable, not zero
    if path.endswith("/children"):
        return []
    return None


mc.request = fake_request
out = capture(mc.cmd_receipt, "b1", "t1", False)
assert "FINISHED" in out and "NOT FINISHED" not in out, out
assert "3 events" in out, out
assert "unavailable" in out, "failed comments fetch must not read as 0: " + out
assert "0 subtasks" in out, out

# a status outside the terminal set must not be claimed as finished
mc.request = lambda m, p, b=None, q=None: (
    [{"id": "t1", "title": "WIP", "status": "in_progress"}] if p.endswith("/tasks") else None
)
out = capture(mc.cmd_receipt, "b1", "t1", False)
assert "NOT FINISHED" in out, out

# missing task exits non-zero rather than printing an empty receipt
try:
    mc.request = lambda m, p, b=None, q=None: []
    mc.cmd_receipt("b1", "nope", False)
    raise AssertionError("missing task should exit")
except SystemExit as e:
    assert e.code == 2

print("all mc receipt checks passed")
