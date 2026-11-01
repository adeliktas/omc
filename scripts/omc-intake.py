#!/usr/bin/env python3
"""Fleet intake: new Jira/Confluence/Mattermost work -> GitHub issues, without cloud eyes on raw data.

Owner 2026-10-28: the fleet sat idle with both plans free because the only intake (2026-10-22) had
been a one-off. omc-next-batch starts this when the backlog is drained; it runs at most every
INTAKE_EVERY and never twice at once.

1. One local Pi session per source on the Jetson Qwen (through OmniRoute, LAN only) follows
   omc-intake-instructions.md: it pulls items changed since the last good intake through the
   logged-in camoufox profile into WORK/raw/<stamp>/<source>.json, nothing more. One session for all
   three outgrew the local model's context and stalled in compaction (2026-10-28).
2. omc-intake-evaluate.py (local Qwen, leak-scanned) writes a redacted evaluation of changed
   pages and posts, and todos.json with the open SCC Jira items only.
3. WORK/sync.py redacts each new todo again with local Qwen, leak-scans it and opens a GitHub
   issue (label omc-fleet); keys it already handled are skipped (its checkpoint).
4. omc-next-batch seeds the next [backlog] lead from those issues on its next tick.

Raw content and the session's event stream stay in WORK (mode 0600). LOG gets metadata only, so a
run is always inspectable without reading content (owner 2026-10-28: "there cannot be some unknown
progress"): every pull turn (tokens, raw file sizes) and tool call (name, ok/error), a heartbeat every
10 min in every phase, and how each phase ended. A pull with no output for INTAKE_STALL_MIN is killed.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import HOME, load_fleet_config, log_path, state_dir  # noqa: E402

HERE = Path(__file__).resolve().parent
INTAKE = load_fleet_config().get("intake") or {}
WORK = Path(os.environ.get("INTAKE_WORK") or INTAKE.get("work_dir") or state_dir("omc-intake") / "work")
STATE = Path(os.environ.get("INTAKE_STATE", str(state_dir("omc-intake") / "state.json")))
LOG = Path(os.environ.get("INTAKE_LOG", str(log_path("omc-intake"))))
INTAKE_EVERY = timedelta(hours=float(os.environ.get("INTAKE_EVERY_HOURS", "12")))
SOURCES = tuple(x for x in ("confluence", "jira", "mattermost") if INTAKE.get(f"{x}_url"))  # configured ones
MAX_BACK = timedelta(days=21)  # owner 2026-10-28: only new items, at most about 3 weeks back
STALL = 60 * float(os.environ.get("INTAKE_STALL_MIN", "15"))
PI = [str(HOME / ".pi/agent/bin/pi"), "-p", "--approve", "--mode", "json", "--provider", "omniroute-api",
      "--model", os.environ.get("INTAKE_MODEL", "vllm/Qwen3.8-27B-FP8"), "--thinking", "low"]


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def pulled_at(source: str) -> float:
    """When an earlier run (Claude or Pi, any file name) last pulled `source` into WORK/raw; 0 if never.
    A killed session's unreadable file is no pull."""
    times = []
    for f in (WORK / "raw").glob(f"*/{source}*.json"):
        try:
            json.loads(f.read_text())
            times.append(f.stat().st_mtime)
        except (OSError, ValueError):
            pass
    return max(times, default=0)


def run(label: str, argv: list[str], timeout: float, stall: float | None = None, on_line=None) -> tuple[int, str]:
    """One phase, logged as metadata: a heartbeat every 10 min, killed at `timeout` or after `stall`
    seconds without output. Returns (rc, output); output is kept only when no on_line consumes it."""
    proc = subprocess.Popen(argv, cwd=WORK, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    kept, seen, last = [], [0], [time.monotonic()]

    def pump():
        for line in proc.stdout:
            seen[0], last[0] = seen[0] + 1, time.monotonic()
            on_line(line) if on_line else kept.append(line)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    start = beat = time.monotonic()
    while True:
        try:
            proc.wait(timeout=min(30, stall or 30))
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
        quiet, took = now - last[0], int((now - start) // 60)
        if now - start > timeout or (stall and quiet > stall):
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            waiting = f"; waiting on {on_line.waiting}" if getattr(on_line, "waiting", None) else ""
            log(f"{label} {'timed out' if now - start > timeout else 'stalled'} after {took}m "
                f"(no output for {int(quiet // 60)}m{waiting}); killed")
            return 124, ""
        if now - beat >= 600:
            beat = now
            waiting = f", waiting on {on_line.waiting}" if getattr(on_line, "waiting", None) else ""
            log(f"{label} running {took}m: {seen[0]} output lines, last {int(quiet)}s ago{waiting}")
    reader.join(10)
    log(f"{label} exited rc={proc.returncode} after {int((time.monotonic() - start) // 60)}m")
    return proc.returncode, "".join(kept)


class PullProgress:
    """Pi's JSON events -> the full stream in WORK, metadata lines in LOG (names, sizes, tokens; no text)."""

    IDENT = re.compile(r"^[\w.:/-]{1,60}$")
    ROUTINE = {"session", "agent_start", "turn_start", "message_start", "message_end", "tool_execution_update"}

    def __init__(self, label: str, stream, raw: Path):
        self.label, self.stream, self.raw = label, stream, raw
        self.turns, self.tools, self.errors, self.started, self.waiting = 0, 0, 0, {}, "model"

    def __call__(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            event = {}
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "message_update":
            return  # streamed tokens: counted as liveness, too bulky to keep
        self.stream.write(line)
        if kind == "tool_execution_start":
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            parts = [event.get("toolName")] + [args.get(k) for k in ("server", "tool")]  # MCP proxy calls
            self.started[event.get("toolCallId")] = " ".join(p for p in parts if isinstance(p, str) and self.IDENT.match(p))
            self.waiting = f"tool {self.started[event.get('toolCallId')]}"
        elif kind == "tool_execution_end":
            self.tools += 1
            self.errors += event.get("isError") is True
            name = self.started.pop(event.get("toolCallId"), None) or "?"
            log(f"{self.label} tool {self.tools}: {name} {'error' if event.get('isError') is True else 'ok'}")
            self.waiting = "model"
        elif kind == "turn_end":
            self.turns += 1
            usage = (event.get("message") or {}).get("usage") or {}
            log(f"{self.label} turn {self.turns}: in {usage.get('input', '?')} out {usage.get('output', '?')} "
                f"tokens; {self.tools} tools ({self.errors} errors); raw: {self.files()}")
            self.waiting = "model"
        elif isinstance(kind, str) and kind not in self.ROUTINE and self.IDENT.match(kind):
            log(f"{self.label} event {kind}")  # compaction, retries, agent_end: named, never silent
            self.waiting = kind.removesuffix("_start") if kind.endswith("_start") else "exit" if kind == "agent_end" else "model"

    def files(self) -> str:
        found = sorted(self.raw.glob("*")) if self.raw.is_dir() else []
        return ", ".join(f"{p.name} {p.stat().st_size}B" for p in found if self.IDENT.match(p.name)) or "none yet"


def main() -> int:
    if not any(INTAKE.get(k) for k in ("jira_url", "confluence_url", "mattermost_url")):
        log("not configured: set intake.jira_url / confluence_url / mattermost_url in fleet.json")
        return 0
    STATE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(STATE.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # an intake is running
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now(timezone.utc)
    if state.get("last_try") and now - datetime.fromisoformat(state["last_try"]) < INTAKE_EVERY:
        return 0
    state["last_try"] = now.isoformat()
    STATE.write_text(json.dumps(state))
    # Only what is newer than the least recent source's last pull, never more than MAX_BACK back: a forced
    # re-pull of 2024 onwards ran 3h through the local model and stalled (2026-10-28). INTAKE_SINCE=YYYY-MM-DD
    # forces a wider window only when the owner asks for one.
    oldest = min((pulled_at(source) for source in SOURCES), default=0)
    since = (datetime.fromisoformat(os.environ["INTAKE_SINCE"]).date() if os.environ.get("INTAKE_SINCE") else
             max(datetime.fromtimestamp(oldest, timezone.utc), now - MAX_BACK).date())
    stamp = now.strftime("%Y%m%d%H%M")
    os.umask(0o077)
    (WORK / f"INSTRUCTIONS-{stamp}.md").write_text(
        (HERE / "omc-intake-instructions.md").read_text().format(work=WORK, since=since, stamp=stamp, jira_url=INTAKE.get("jira_url", ""),
            confluence_url=INTAKE.get("confluence_url", ""), mattermost_url=INTAKE.get("mattermost_url", ""),
            confluence_space=INTAKE.get("confluence_space", ""), jira_project=INTAKE.get("jira_project", ""),
            channel_terms=", ".join(f"`{t}`" for t in INTAKE.get("channel_terms", [])),
            search_terms=", ".join(f"`{t}`" for t in INTAKE.get("search_terms", []))))
    raw = WORK / "raw" / stamp
    raw.mkdir(parents=True, exist_ok=True)
    log(f"intake {stamp} pull started since {since} (model {PI[PI.index('--model') + 1]}, stall kill {STALL // 60:.0f}m)")
    turns = tools = 0
    for source in SOURCES:
        prompt = (f"Read {WORK}/INSTRUCTIONS-{stamp}.md. Do step 1 for the {source} site only, then only the "
                  f"raw/{stamp}/{source}.json part of step 2, then stop. WORK = {WORK}. Work step by step, keep "
                  "replies short and never paste raw content.")
        with (WORK / f"intake-{stamp}-{source}.log").open("w") as out:
            progress = PullProgress(f"intake {stamp} {source}", out, raw)
            run(f"intake {stamp} {source}", PI + [prompt], 2 * 3600, STALL, progress)
        turns, tools = turns + progress.turns, tools + progress.tools
    counts = {}
    for source in SOURCES:
        try:
            counts[source] = len(json.loads((raw / f"{source}.json").read_text()))
        except (OSError, ValueError, TypeError):
            pass
    missing = ""
    if len(counts) < len(SOURCES):
        blocked = sorted(p.name for p in WORK.glob("BLOCKED-*.md") if p.stat().st_mtime >= now.timestamp())
        missing = (f"intake {stamp} did not finish (missing or unreadable: {', '.join(x for x in SOURCES if x not in counts)}; "
                   f"blocked: {', '.join(blocked) or 'none'})")
        log(missing)
        if not counts:
            return 1
        # 2026-10-28: a confluence stall threw away the finished jira pull; the rest still counts.
    (WORK / f"DONE-{stamp}.md").write_text(json.dumps(counts) + "\n")
    # The pull session only fetches (2026-10-28: a local model following one long workflow
    # pulled fine but never finished the transform). Evaluation and todos are a local script.
    log(f"intake {stamp} pulled {json.dumps(counts)} in {turns} turns, {tools} tools; evaluation started")
    rc, ev = run(f"intake {stamp} evaluation", [sys.executable, str(HERE / "omc-intake-evaluate.py"), str(WORK), stamp], 3600)
    if rc:
        log(f"intake {stamp} pulled, but evaluation failed rc={rc}")
        return 1
    rc, sync = run(f"intake {stamp} sync", [sys.executable, str(WORK / "sync.py"), "--workers", "2"], 3 * 3600)
    if not missing:  # a partial pull keeps last_ok, so the next run pulls the missing source again
        state["last_ok"] = now.isoformat()
        STATE.write_text(json.dumps(state))
    log(f"{missing or f'intake {stamp} done'} since {since}; {ev.strip()[-200:]}; sync rc={rc}: {sync.strip()[-300:]}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
