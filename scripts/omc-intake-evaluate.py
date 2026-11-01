#!/usr/bin/env python3
"""Local half of the fleet intake: raw pulls -> redacted evaluation + todos for sync.py.

Runs on this host only, against WORK/raw/<stamp>/{jira,confluence,mattermost}.json written by the
local pull session. Local Qwen (WORK/redact.py's endpoint) evaluates changed pages and posts; the
result must pass redact.py's leak scan before evaluation-<stamp>.md is written. Only Jira items of
the configured project (fleet.json intake.jira_project) become todos: on 2026-10-28 the pages' "next
steps" were about the agent platform itself, which must never become issues in the product repository.
Prints counts only.

  omc-intake-evaluate.py <work> <stamp>
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import load_fleet_config  # noqa: E402

WORK, STAMP = sys.argv[1], sys.argv[2]
RAW = os.path.join(WORK, "raw", STAMP)
ns = {"__file__": os.path.join(WORK, "redact.py")}
exec(open(ns["__file__"]).read().split("d = json.load(open(os.path.join(WORK, 'todos.json')))")[0], ns)
URL, KEY, MODEL, SCAN = ns["URL"], ns["KEY"], ns["MODEL"], ns["SCAN"]
INTAKE = load_fleet_config().get("intake") or {}
SYSTEM = """You evaluate internal engineering notes for the TEAM, which runs an autonomous AI agent fleet that implements GitHub issues.
Output ONLY one JSON object:
{"decisions": [str], "agent_fleet_changes": [str], "priorities": [str], "open_questions": [str], "next_steps": [str]}
- decisions: decisions taken. agent_fleet_changes: requested changes to how AI agents or the autonomous fleet should work.
- priorities: priorities and deadlines. open_questions: unresolved questions. next_steps: concrete actions.
- One sentence per entry, technical and neutral.
- REMOVE completely: personal names (use roles like "the team" or "an engineer"), customer or company names other than your own organization, e-mail addresses, hostnames, IP addresses, URLs, credentials and tokens. Never copy sentences that contain any of these."""
SYSTEM = SYSTEM.replace("the TEAM", "the " + INTAKE.get("team", "engineering team"))
SECTIONS = (("Decisions", "decisions"), ("Requested changes to the AI agent fleet", "agent_fleet_changes"),
            ("Priorities and deadlines", "priorities"), ("Open questions", "open_questions"), ("Next steps", "next_steps"))


def load(name: str) -> list:
    try:
        data = json.load(open(os.path.join(RAW, f"{name}.json")))
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def write(name: str, text: str) -> None:
    fd = os.open(os.path.join(WORK, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def evaluate(pages: list, posts: list) -> dict:
    """Every page and post is read: batches fit the Jetson's context (a full refresh is ~160 pages)."""
    merged: dict = {}
    batches = [(pages[i:i + 5], []) for i in range(0, len(pages), 5)] + [([], posts[i:i + 40]) for i in range(0, len(posts), 40)]
    for batch_pages, batch_posts in batches:
        for key, values in _evaluate(batch_pages, batch_posts).items():
            merged.setdefault(key, [])
            merged[key] += [v for v in values if isinstance(v, str) and v not in merged[key]]
    return merged


def _evaluate(pages: list, posts: list) -> dict:
    material = "\n\n".join(f"PAGE: {p.get('title')}\n{str(p.get('text'))[:6000]}" for p in pages)
    material += "\n\nMATTERMOST POSTS:\n" + "\n".join(f"- [{p.get('channel')}] {str(p.get('message'))[:1500]}" for p in posts)
    body = {"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": material}],
            "temperature": 0.2, "max_tokens": 4000, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    text = json.loads(urllib.request.urlopen(req, timeout=900).read())["choices"][0]["message"]["content"]
    try:
        found = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (AttributeError, ValueError):
        return {}  # one unreadable batch must not lose the others
    return {k: v for k, v in found.items() if isinstance(v, list)}


def main() -> int:
    jira, pages, posts = load("jira"), load("confluence"), load("mattermost")
    counts = {"jira": len(jira), "pages": len(pages), "posts": len(posts)}
    if pages or posts:
        ev = evaluate(pages, posts)
        hits = [name for name, pattern in SCAN.items() if re.search(pattern, json.dumps(ev, ensure_ascii=False))]
        counts["evaluation"] = f"rejected by leak scan: {hits}" if hits else "written"
        if not hits:
            lines = [f"# Intake evaluation {STAMP} (local Qwen, redacted)", "",
                     f"Sources: {len(pages)} changed Confluence pages, {len(posts)} Mattermost posts.", ""]
            for title, key in SECTIONS:
                lines += [f"## {title}", ""] + [f"- {x}" for x in ev.get(key) or ["none stated"]] + [""]
            write(f"evaluation-{STAMP}.md", "\n".join(lines))
    todos = [{"key": i["key"], "title": i.get("summary"), "type": i.get("type"), "status": i.get("status"),
              "priority": i.get("priority"), "labels": i.get("labels"), "parent": i.get("parent"),
              "summary": str(i.get("description") or "")[:1500], "link": f"{INTAKE.get('jira_url', '').rstrip('/')}/browse/{i['key']}"}
             for i in jira if str(i.get("key", "")).startswith(INTAKE.get("jira_project", "") + "-") and i.get("status") != "Done"]
    write("todos.json", json.dumps({"source": f"intake-{STAMP}", "todos": todos}))
    counts["todos"] = len(todos)
    print("COUNTS", json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
