#!/usr/bin/env python3
"""Streaming todo sync: raw item -> local Qwen redaction (parallel) -> GitHub issue (as soon as ready).

Step 3 of omc-intake.py (moved here from WORK/sync.py, 2026-10-31). Every result is checkpointed per item:
  todos-redacted.jsonl  one line per processed key: {"key", "status": kept|skipped|scan_rejected|epic, "hash", "todo"?}
  issues-map.json       {key: issue number}
A key is redacted again when its Jira fields change (hash). 2026-10-31: the board idled because 12 of 19
changed tickets were known keys, and their updates never reached the fleet. A changed todo updates its
issue and drops omc-needs-human, so next-batch judges the new text.
Prints counts only; raw ticket text never leaves this host except as Qwen-rewritten todos.
redact.py (the local-Qwen call and the private leak-scan patterns) stays in WORK, outside git.

  python3 omc-todo-sync.py [WORK] [--workers 4] [--no-issues]
"""
import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import load_fleet_config, state_dir  # noqa: E402

CFG = load_fleet_config()
REPO, LABEL, HUMAN = os.environ.get("OMC_GH_REPO") or CFG.get("github_repo", ""), "omc-fleet", "omc-needs-human"
FIELDS = ("title", "type", "status", "priority", "labels", "parent", "summary", "page_title", "text")


def fingerprint(item):
    """The fields Qwen redacts, minus status: a ticket moving column is no new requirement."""
    raw = {k: item.get(k) for k in FIELDS if k != "status"}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def gh(*a):
    return subprocess.run(["gh", *a], capture_output=True, text=True, timeout=120, check=True).stdout


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="?", default=(CFG.get("intake") or {}).get("work_dir") or str(state_dir("omc-intake") / "work"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-issues", action="store_true")
    args = ap.parse_args(argv)
    work = args.work
    ns = {"__file__": os.path.join(work, "redact.py")}
    exec(open(ns["__file__"]).read().split("d = json.load(open(os.path.join(WORK, 'todos.json')))")[0], ns)
    ask, scan = ns["ask"], ns["SCAN"]  # reuse the tested local-Qwen call and leak scan

    jsonl, map_path = os.path.join(work, "todos-redacted.jsonl"), os.path.join(work, "issues-map.json")
    done = {}
    if os.path.exists(jsonl):
        for line in open(jsonl):
            r = json.loads(line)
            done[r["key"]] = r
    mapping = json.load(open(map_path)) if os.path.exists(map_path) else {}
    lock, issues_q, stats = threading.Lock(), queue.Queue(), {}

    def count(name):
        with lock:
            stats[name] = stats.get(name, 0) + 1

    def record(r):
        with lock:
            with open(os.open(jsonl, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            done[r["key"]] = r

    def redact(i):
        key, h = i.get("key"), fingerprint(i)
        if i.get("type") == "Epic":
            return record({"key": key, "status": "epic", "hash": h})
        try:
            r = ask({k: i.get(k) for k in FIELDS})
        except Exception:  # noqa: BLE001 - not recorded, so a rerun retries it
            return count("failed")
        if r.get("skip"):
            return record({"key": key, "status": "skipped", "hash": h})
        todo = {"key": key, "type": i.get("type") or "Confluence task", "priority": i.get("priority"),
                "title": str(r.get("title", ""))[:120], "summary": str(r.get("summary", ""))[:1200],
                "acceptance": [str(a)[:300] for a in (r.get("acceptance") or [])][:5], "link": i.get("link")}
        if any(re.search(p, json.dumps({k: todo[k] for k in ("title", "summary", "acceptance")})) for p in scan.values()):
            return record({"key": key, "status": "scan_rejected", "hash": h})
        record({"key": key, "status": "kept", "hash": h, "todo": todo})
        issues_q.put((todo, True))

    def issue_writer():
        """Single writer: GitHub content-creation limits punish parallel bursts."""
        existing = json.loads(gh("issue", "list", "-R", REPO, "--state", "all", "--limit", "1000", "--json", "number,title,body"))
        subprocess.run(["gh", "label", "create", LABEL, "-R", REPO, "--color", "5319e7",
                        "--description", "Created for the OMC orchestration fleet"], capture_output=True, timeout=60)
        while (job := issues_q.get()) is not None:
            t, fresh = job
            key = t["key"]
            if key not in mapping:
                hit = next((i for i in existing if key in (i["title"] or "") or key in (i["body"] or "")), None)
                if hit:
                    mapping[key] = hit["number"]
                    count("issue_existing")
            if key in mapping and not fresh:
                continue
            body = (f"{t['summary']}\n\n### Acceptance criteria\n" + "".join(f"- [ ] {a}\n" for a in t["acceptance"])
                    + f"\nSource: {key} ({t['type']}, priority {t.get('priority') or 'n/a'}) — {t['link']}\n\n"
                    + "_Redacted engineering summary created by the OMC todo sync; raw ticket content stays in Jira/Confluence._\n")
            title = f"[{key}] {t['title']}"
            # 2026-10-30: one create failed with no reason logged and waited 12h for the next intake.
            # Retry (GitHub's secondary limit rejects quick bursts) and name gh's error in STATS.
            for attempt in range(3):
                try:
                    if key in mapping:
                        gh("issue", "edit", str(mapping[key]), "-R", REPO, "--title", title, "--body", body, "--remove-label", HUMAN)
                        count("issue_updated")
                    else:
                        url = gh("issue", "create", "-R", REPO, "--title", title, "--body", body, "--label", LABEL)
                        mapping[key] = int(url.strip().rsplit("/", 1)[1])
                        count("issue_created")
                    time.sleep(2)
                    break
                except (subprocess.SubprocessError, ValueError) as e:
                    lines = (getattr(e, "stderr", None) or str(e)).strip().splitlines() or [type(e).__name__]
                    stats["issue_error"] = re.sub(r"[^\w .:()-]", "", lines[-1])[:120]
                    if attempt < 2:
                        time.sleep(60)
            else:
                count("issue_failed")
            with lock:
                json.dump(mapping, open(map_path, "w"), indent=1)

    items = json.load(open(os.path.join(work, "todos.json")))
    items = items["todos"] if isinstance(items, dict) else items
    writer = None if args.no_issues else threading.Thread(target=issue_writer)
    if writer:
        writer.start()
        for r in done.values():  # already-redacted items still need their issue
            if r["status"] == "kept":
                issues_q.put((r["todo"], False))
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(redact, [i for i in items if done.get(i.get("key"), {}).get("hash") != fingerprint(i)]))
    if writer:
        issues_q.put(None)
        writer.join()
    statuses = {}
    for r in done.values():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print("STATS", json.dumps({"items": len(items), **statuses, **stats, "issues_mapped": len(mapping)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
