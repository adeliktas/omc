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
3. omc-todo-sync.py redacts each new or changed todo again with local Qwen, leak-scans it and opens
   or updates its GitHub issue (label omc-fleet); unchanged keys are skipped (its checkpoint).
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
import socket
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
MAX_TOOLS = 1000  # a pull's tool calls; normal pulls use under 160 (2026-11-04: a runaway loop made 10,543)
PI = [str(HOME / ".pi/agent/bin/pi"), "-p", "--approve", "--mode", "json", "--provider", "omniroute-api",
      "--model", os.environ.get("INTAKE_MODEL", "vllm/Qwen3.8-27B-FP8"), "--thinking", "low"]
BROWSERS = HOME / ".local/share/camoufox-mcp"  # mcp-camoufox's default data dir, which Pi's MCP config uses
CAMOUFOX = Path(os.environ.get("CAMOUFOX_DATA_DIR") or BROWSERS)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def live_browser_daemon() -> int | None:
    """Pid of the camoufox daemon a new MCP client reaches, or None when none answers."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(str(CAMOUFOX / "daemon" / "daemon.sock"))
            s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            reply = b"".join(iter(lambda: s.recv(65536), b""))
        return int(json.loads(reply.split(b"\r\n\r\n", 1)[1])["pid"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def reap_orphan_browsers(stamp: str, proc: Path = Path("/proc")) -> None:
    """Stop camoufox daemons on the pull's data dir that no MCP client can reach any more.

    2026-11-03: a client that could not reach the running daemon started a second one. The first kept its
    browser on profile `default` (the logins) open, so it never idled out, and its profile lock made the
    confluence pull fail as logged out; on 11-02 the local model had killed such a daemon by hand. Nothing
    reaches a daemon whose address is gone, so stopping it loses nothing and frees its profiles.
    """
    live = live_browser_daemon()
    uptime = float((proc / "uptime").read_text().split()[0])
    for d in sorted(proc.glob("[0-9]*"), key=lambda d: int(d.name)):
        try:
            if b"camoufox_mcp.daemon" not in (d / "cmdline").read_bytes() or int(d.name) == live:
                continue
            env = dict(x.split(b"=", 1) for x in (d / "environ").read_bytes().split(b"\0") if b"=" in x)
            age = uptime - int((d / "stat").read_text().rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
            if Path(os.fsdecode(env.get(b"CAMOUFOX_DATA_DIR", b"")) or BROWSERS) != CAMOUFOX or age < 120:
                continue  # another data dir, or just spawned and not yet published
            os.kill(int(d.name), signal.SIGTERM)
        except (OSError, ValueError, IndexError):
            continue
        log(f"intake {stamp}: stopped camoufox daemon {d.name} ({age / 3600:.0f}h old, unreachable; it held browser profiles)")


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
        why = ("timed out" if now - start > timeout else getattr(on_line, "stop", None)
               or ("stalled" if stall and quiet > stall else None))
        if why:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            waiting = f"; waiting on {on_line.waiting}" if getattr(on_line, "waiting", None) else ""
            log(f"{label} {why} after {took}m "
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
        self.gave_up = False  # the last model call failed after all of Pi's retries
        self.stop = None  # why run() kills the pull

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
            if self.tools > MAX_TOOLS:
                self.stop = f"ran away (over {MAX_TOOLS} tool calls)"
            self.waiting = "model"
        elif kind == "turn_end":
            self.turns += 1
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            # 2026-10-30: a provider refusal Pi does not retry (a switched-off connection) ended the pull unnamed.
            error = (f"; model error: {re.sub(r'[^\w .:/()-]', '', str(message.get('errorMessage')))[:100]}"
                     if message.get("stopReason") == "error" else "")
            log(f"{self.label} turn {self.turns}: in {usage.get('input', '?')} out {usage.get('output', '?')} "
                f"tokens; {self.tools} tools ({self.errors} errors); raw: {self.files()}{error}")
            self.waiting = "model"
        elif isinstance(kind, str) and kind not in self.ROUTINE and self.IDENT.match(kind):
            # A retry names the provider's error (2026-10-29: a 429 from a held local slot was invisible).
            reason = re.sub(r"[^\w .:/()-]", "", str(event.get("errorMessage")))[:100] if kind == "auto_retry_start" else ""
            if kind == "auto_retry_end":
                self.gave_up = event.get("success") is False
            log(f"{self.label} event {kind}{': ' + reason if reason else ''}")  # compaction, retries, agent_end: named, never silent
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
    silent = []  # sources whose session ended on a provider error Pi gave up retrying
    for source in SOURCES:
        prompt = (f"Read {WORK}/INSTRUCTIONS-{stamp}.md. Do step 1 for the {source} site only, then only the "
                  f"raw/{stamp}/{source}.json part of step 2, then stop. WORK = {WORK}. Work step by step, keep "
                  "replies short and never paste raw content.")
        for attempt in (1, 2):
            reap_orphan_browsers(stamp)
            with (WORK / f"intake-{stamp}-{source}.log").open("a") as out:
                progress = PullProgress(f"intake {stamp} {source}", out, raw)
                rc, _ = run(f"intake {stamp} {source}", PI + [prompt], 2 * 3600, STALL, progress)
            turns, tools = turns + progress.turns, tools + progress.tools
            # 2026-11-04: one runaway model loop crashed Pi (rc=-6) and cost jira 12 h; a model's loop is no outage.
            if attempt == 2 or not (rc < 0 or progress.stop):
                break
            log(f"intake {stamp} {source} pull {progress.stop or f'crashed (rc={rc})'}; one fresh session")
        if progress.gave_up:
            silent.append(source)
    counts = {}
    blocked = sorted(p.name for p in WORK.glob("BLOCKED-*.md") if p.stat().st_mtime >= now.timestamp())
    for source in SOURCES:
        if any(source in name.lower() for name in blocked) and (raw / f"{source}.json").exists():
            # 2026-10-31: a logged-out site's `[]` read as a quiet day and moved pulled_at past unpulled days.
            (raw / f"{source}.json").rename(raw / f"{source}.json.blocked")
        try:
            counts[source] = len(json.loads((raw / f"{source}.json").read_text()))
        except (OSError, ValueError, TypeError):
            pass
    missing = ""
    if len(counts) < len(SOURCES):
        absent = [x for x in SOURCES if x not in counts]
        if all(x in silent for x in absent):
            # 2026-10-29: an interactive session held the local model's only OmniRoute slot, every request
            # got a 429 and the run took the next 12 h of intake with it. A source the model stopped answering is
            # a wait, so the run's summary must not read "did not finish" (the watchdog's failure line) either.
            state.pop("last_try", None)
            STATE.write_text(json.dumps(state))
            missing = f"intake {stamp}: the local model stopped answering {', '.join(absent)}; retrying on the next tick"
        else:
            missing =(f"intake {stamp} did not finish (missing or unreadable: {', '.join(absent)}; "
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
        error = re.sub(r"[^\w .:/()-]", "", (ev.strip().splitlines() or [""])[-1])[:100]  # the exception, never content
        log(f"intake {stamp} pulled, but evaluation failed rc={rc}{': ' + error if error else ''}")
        return 1
    rc, sync = run(f"intake {stamp} sync", [sys.executable, str(HERE / "omc-todo-sync.py"), str(WORK), "--workers", "2"], 3 * 3600)
    if not missing:  # a partial pull keeps last_ok, so the next run pulls the missing source again
        state["last_ok"] = now.isoformat()
        STATE.write_text(json.dumps(state))
    log(f"{missing or f'intake {stamp} done'} since {since}; {ev.strip()[-200:]}; sync rc={rc}: {sync.strip()[-300:]}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
