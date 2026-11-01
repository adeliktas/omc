"""Existing native task budget gate; observed I/O only, not a streaming ceiling."""

from __future__ import annotations

import json
import re
import subprocess
import time

UNREACHABLE_RETRIES = 18  # x 5 s rides out a backend redeploy
FINISH_GRACE = 600_000  # I/O a started cycle may spend past the cap (= the pool's CAP_STEP)


class ApiError(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(f"HTTP {code}: {text[:200]}")
        self.code = code
        self.text = text


def request(mc: str, method: str, path: str, body: dict | None = None):
    # Keep large completion bodies on stdin, using the normal mc client.
    for left in range(UNREACHABLE_RETRIES, -1, -1):
        p = subprocess.run(
            [mc, method, path] + (["-"] if body is not None else []) + ["--json"],
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not p.returncode:
            return json.loads(p.stdout) if p.stdout.strip() else None
        m = re.search(r"HTTP (\d+)", p.stderr)
        if m or not left:
            raise ApiError(int(m.group(1)) if m else 599, p.stderr.strip())
        # No HTTP status: backend unreachable (deploy restart). Failing here made a
        # heartbeat cancel live workers and discarded finished results (2026-10-22).
        time.sleep(5)


def exhausted(task: dict) -> bool:
    cap = task.get("max_tokens_budget")
    return cap is not None and cap > 0 and (task.get("tokens_used") or 0) >= cap


def check(api, board: str, task_id: str, expected: dict) -> dict | None:
    """Fresh read, then atomic status/generation CAS + durable cap comment.

    Task PATCH fences active cycles via the existing lifecycle API. Do not first
    block a cycle: that could mutate work after a lost owner-generation CAS.
    Blocked tasks are never patched again, including from stale pool scans.
    """
    base = f"/api/v1/boards/{board}/tasks"
    offset = 0
    while True:
        data = api("get", f"{base}?limit=200&offset={offset}")
        rows = data if isinstance(data, list) else data.get("items", [])
        task = next((t for t in rows if t["id"] == task_id), None)
        if task is not None or len(rows) < 200:
            break
        offset += 200
    if task is None or task.get("execution_mode") != "team":
        return None
    if (
        task.get("status") not in ("inbox", "in_progress")
        or task.get("updated_at") != expected.get("updated_at")
        or task.get("status") != expected.get("status")
        or (task.get("execution_lane") or "harness") != "harness"
        or task.get("split_count")
        or task.get("is_blocked")
        or task.get("blocked_by_task_ids")
    ):
        return None
    if not exhausted(task):
        return task
    # Publication spends no inference. Stopping a QA-passed cycle cancelled it and threw
    # the verified work away, which then paid for a cap raise and a full replay (8 QA
    # PASSes in 4 days, e.g. 0000a00d on 2026-10-24, where the QA's own usage crossed the cap).
    cycles = (api("get", f"{base}/{task_id}/team") or {}).get("cycles") or []
    state = cycles[0]["cycle"].get("state") if cycles else None
    if state in ("qa_passed", "publishing"):
        return task
    # The cap gates starting a cycle; a started one finishes within FINISH_GRACE over it.
    # Stopping mid-cycle cancelled it, and the pool's +600k raise then paid for a replay from
    # PM that hit the cap at the same point: 60 cycles (~450k I/O each) discarded in 5 days,
    # 0000a014 twice in a row on 2026-10-25. A whole published cycle costs <= 760k.
    over = task["tokens_used"] - task["max_tokens_budget"]
    if state in ("planning", "developing", "qa") and over < FINISH_GRACE:
        return task
    reason = (
        f"Task input/output cap reached; observed I/O {task['tokens_used']}; "
        f"configured limit {task['max_tokens_budget']}. Further inference stopped."
    )
    stop = {
        "status": "blocked",
        "expected_status": task["status"],
        "expected_updated_at": task["updated_at"],
        "comment": reason,
    }
    try:
        api("patch", f"{base}/{task_id}", stop)
    except ApiError as exc:
        if exc.code != 409:
            raise
        # A concurrent owner/lifecycle update or a publication reservation (no inference
        # left to stop) wins. Still no admission.
    return None
