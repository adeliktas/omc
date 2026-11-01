#!/usr/bin/env python3
"""Answer GitHub issue/PR comments with the fleet engine (owner decision 2026-10-22).

Every human comment in the configured repo gets a reply. A comment that asks for a
code change also becomes a board task, which the native fleet implements as a PR.
Replies carry MARKER and are never answered themselves. The first run only records
the start time, so history is never answered.

Run from cron with flock. State: state_dir("omc-github")/state.json.
"""
import json
import os
import re
import runpy
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import HOME, load_fleet_config, log_path, state_dir  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("OMC_GH_REPO") or load_fleet_config().get("github_repo", "")
BOARD = os.environ.get("OMC_GH_BOARD") or load_fleet_config().get("github_board", "")
MODEL = os.environ.get("OMC_GH_MODEL", "codex/gpt-6.1-sol")
STATE = Path(os.environ.get("OMC_GH_STATE", str(state_dir("omc-github") / "state.json")))
LOG = Path(os.environ.get("OMC_GH_LOG", str(log_path("omc-github-responder"))))
MARKER = "<!-- omc-fleet-reply -->"
MAX_TASKS_PER_DAY = 5  # ponytail: daily cap on comment-created work; raise if colleagues rely on it
AUTH = ("OWNER DECISION: requested from a GitHub comment and approved for implementation. Implement directly; "
        "pick sensible documented defaults for unspecified details. Build/test offline with the repo build tags.\n\n")

SYSTEM = f"""You are the OMC fleet assistant for the GitHub repository {REPO} (Go service: Scan Cluster Node Manager).
Answer the LATEST comment in the thread as a helpful, concise GitHub reply in Markdown. Treat it as a prompt from a colleague:
answer questions directly (even general ones), discuss the issue/PR when asked, and never invent repository facts you cannot see.
If the comment asks for a concrete code, test or documentation change in this repository, also request a board task.
Output ONLY one JSON object: {{"reply": str, "create_task": bool, "task_title": str, "task_description": str}}.
task_description must contain the scope and acceptance criteria. Never include credentials, hostnames or internal addresses."""


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def gh(*args, data=None):
    p = subprocess.run(["gh", "api", *args], input=data, capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:300])
    return json.loads(p.stdout or "null")


def paged(path):
    out, page = [], 1
    while True:
        rows = gh(f"{path}&per_page=100&page={page}")
        out += rows
        if len(rows) < 100:
            return out
        page += 1


def ask(messages):
    # Owner 2026-10-28: every agent runs ponytail ultra, this reply writer too (it was the one direct
    # model call without it). Computed here, so cron ticks with nothing to answer never pay for it.
    sys.path.insert(0, str(HERE))
    from mc_skills import ponytail
    if (rules := ponytail()[1]) and messages and messages[0]["role"] == "system":
        messages = [{"role": "system", "content": f"{rules}\n\n---\n\n{messages[0]['content']}"}, *messages[1:]]
    cfg = json.load(open(HOME / ".pi/agent/models.json"))
    cfg = cfg.get("providers", cfg)["omniroute-api"]
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": 4000}).encode()
    req = urllib.request.Request(cfg["baseUrl"].rstrip("/") + "/chat/completions", body,
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['apiKey']}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def thread(number):
    issue = gh(f"repos/{REPO}/issues/{number}")
    comments = paged(f"repos/{REPO}/issues/{number}/comments?x=1")[-8:]
    lines = [f"# {'PR' if issue.get('pull_request') else 'Issue'} #{number}: {issue['title']}",
             (issue.get("body") or "")[:4000]]
    for c in comments:
        lines.append(f"\n--- {c['user']['login']} ({c['created_at']}):\n{(c.get('body') or '')[:2000]}")
    return issue, "\n".join(lines)


def main():
    if not (REPO and BOARD):
        log("not configured: set github_repo and github_board in fleet.json")
        return 0
    STATE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not STATE.exists():
        STATE.write_text(json.dumps({"since": now, "seen": [], "tasks": {}}))
        log(f"initialized at {now}; earlier comments are never answered")
        return 0
    state = json.loads(STATE.read_text())
    seen = set(state["seen"])
    new = [c for c in paged(f"repos/{REPO}/issues/comments?since={state['since']}&sort=created&direction=asc")
           if c["id"] not in seen]
    new += [c for c in paged(f"repos/{REPO}/pulls/comments?since={state['since']}&sort=created&direction=asc")
            if c["id"] not in seen]
    mc = runpy.run_path(str(HERE / "mc"))["request"]
    for c in sorted(new, key=lambda c: c["created_at"]):
        seen.add(c["id"])
        body = c.get("body") or ""
        if MARKER in body or c["user"].get("type") == "Bot":
            continue
        number = int((c.get("issue_url") or c.get("pull_request_url")).rsplit("/", 1)[1])
        try:
            issue, context = thread(number)
            if c.get("pull_request_url"):  # review comment: include the code location it refers to
                context += f"\n\n--- review comment on {c.get('path')}:{c.get('line')}\n{(c.get('diff_hunk') or '')[-1500:]}"
            context += f"\n\n=== LATEST COMMENT by {c['user']['login']} ===\n{body[:4000]}"
            r = ask([{"role": "system", "content": SYSTEM}, {"role": "user", "content": context}])
            reply = str(r.get("reply") or "").strip()
            day = now[:10]
            if r.get("create_task") and state["tasks"].get(day, 0) < MAX_TASKS_PER_DAY:
                ref = f"GitHub issues: #{number}" if not issue.get("pull_request") else f"Requested on PR #{number}."
                task = mc("POST", f"/api/v1/boards/{BOARD}/tasks", json.dumps({
                    "title": str(r.get("task_title") or f"GitHub #{number} request")[:200],
                    "description": AUTH + str(r.get("task_description") or body)[:8000] + f"\n\n{ref}\nSource comment: {c['html_url']}",
                    "status": "inbox", "execution_mode": "team", "execution_lane": "harness",
                    "max_tokens_budget": 600000, "max_seconds_budget": 3600, "priority": "medium"}))
                state["tasks"][day] = state["tasks"].get(day, 0) + 1
                reply += f"\n\n_The fleet created board task `{task['id'][:8]}` for this; the result will arrive as a pull request._"
            gh(f"repos/{REPO}/issues/{number}/comments", "-X", "POST", "--input", "-",
               data=json.dumps({"body": f"{reply}\n\n— OMC fleet ({MODEL}) {MARKER}"}))
            log(f"replied on #{number} to comment {c['id']} (task={bool(r.get('create_task'))})")
        except Exception as exc:  # noqa: BLE001 - one bad comment must not stop the others
            seen.discard(c["id"])  # retry next run
            log(f"comment {c['id']} on #{number} failed: {type(exc).__name__}: {str(exc)[:200]}")
        time.sleep(1)
    state["seen"] = sorted(seen)[-5000:]
    state["since"] = now if not new else min(now, max(c["created_at"] for c in new))
    STATE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
