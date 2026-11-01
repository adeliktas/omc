#!/usr/bin/env python3
"""Fixed-scope deterministic connector. External bytes never become instructions."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import resource
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omc_paths import HOME, load_fleet_config, state_dir  # noqa: E402
from typing import Any, cast

ROOT = state_dir("private-local-review")
STATE = ROOT / "github"
# fleet.json github_poll: the one private PR whose owner comments are polled (unset: off).
POLL = load_fleet_config().get("github_poll") or {}
REPO, REPO_ID, PR = POLL.get("repo", ""), POLL.get("repo_id", 0), POLL.get("pr", 0)
ACTOR, ACTOR_LOGIN = POLL.get("actor_id", 0), POLL.get("actor_login", "")
BASE = "/repos/" + REPO
COMMENTS = f"{BASE}/issues/{PR}/comments"
TARGET = f"https://api.github.com/repos/{REPO}/issues/{PR}"
MAX_BYTES = 1024 * 1024
MAX_PAGES = 10
DEADLINE = 90
SEED_TEXT = "private-board NON-SENSITIVE SYNTHETIC COMMUNICATION TEST. No project content. "
REPLY_TEXT = "private-board NON-SENSITIVE SYNTHETIC REPLY. Deterministic receipt only; not useful-task acceptance. "
WORK = (
    "Investigate: exact approved synthetic seed verified. Plan: fixed safe reply only. "
    "Implement: durable intent before effect. Verify: exact identity/target/body receipt; live acceptance separate."
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SafeError(Exception):
    def __init__(self, status: str, delay: float = 300) -> None:
        self.status = status
        self.delay = delay
        super().__init__(status)


def secure_file(path: Path) -> None:
    s = path.lstat()
    if (
        not stat.S_ISREG(s.st_mode)
        or s.st_uid != os.geteuid()
        or s.st_nlink != 1
        or stat.S_IMODE(s.st_mode) != 0o600
    ):
        raise SafeError("unsafe-file")


def secure_dir(path: Path, create: bool = False) -> None:
    # Never follow an ancestor symlink, including at the offline interface.
    for part in [*reversed(path.parents), path]:
        if part.exists() or part.is_symlink():
            s = part.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid not in (0, os.geteuid()):
                raise SafeError("unsafe-directory")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    s = path.lstat()
    if s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise SafeError("unsafe-directory")


def decode_response(raw: bytes, rc: int = 0, now: float | None = None) -> Any:
    if len(raw) > MAX_BYTES:
        raise SafeError("oversize-response")
    # gh --include emits HTTP headers followed by JSON, never parse status as JSON.
    try:
        head, body = raw.replace(b"\r\n", b"\n").split(b"\n\n", 1)
        lines = head.decode("ascii").splitlines()
        code = int(lines[0].split()[1])
        headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        headers = {k.lower().strip(): v.strip() for k, v in headers.items()}
    except (ValueError, IndexError, UnicodeError):
        raise SafeError("invalid-http") from None
    if code < 200 or code >= 300 or rc:
        delay: float = 300
        try:
            delay = max(
                delay,
                int(headers.get("retry-after", "0")),
                int(headers.get("x-ratelimit-reset", "0")) - int(now or time.time()),
            )
        except ValueError:
            pass
        # Never shorten a server-requested backoff. Implausibly long values
        # become an indefinite manual hold rather than overflowing a timestamp.
        if delay > 315360000:
            delay = float("inf")
        raise SafeError("http-failure", delay)
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise SafeError("invalid-json") from None


class GhTransport:
    def __init__(self, state: Path) -> None:
        self.state = state

    def request(
        self, method: str, endpoint: str, body: str | None, deadline: float
    ) -> Any:
        allowed = endpoint in ("/user", BASE, f"{BASE}/pulls/{PR}", COMMENTS)
        if endpoint.startswith(COMMENTS + "?per_page=100&page="):
            page = endpoint.removeprefix(COMMENTS + "?per_page=100&page=")
            allowed = page.isdigit() and 1 <= int(page) <= MAX_PAGES
        if (
            not allowed
            or method not in ("GET", "POST")
            or (method == "POST" and endpoint != COMMENTS)
        ):
            raise SafeError("endpoint-refused")
        remaining = min(20, deadline - time.monotonic())
        if remaining <= 0:
            raise SafeError("deadline")
        argv = [
            "/usr/bin/gh",
            "api",
            "--hostname",
            "github.com",
            "--include",
            "--method",
            method,
            endpoint,
        ]
        if body is not None:
            argv += ["--input", "-"]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(HOME),
            "GH_CONFIG_DIR": str(HOME / ".config/gh"),
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
            "PAGER": "cat",
        }

        def limit() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES + 1, MAX_BYTES + 1))

        with (
            tempfile.TemporaryFile(dir=self.state) as out,
            tempfile.TemporaryFile(dir=self.state) as err,
        ):
            try:
                result = subprocess.run(
                    argv,
                    input=None if body is None else json.dumps({"body": body}).encode(),
                    stdout=out,
                    stderr=err,
                    env=env,
                    timeout=remaining,
                    preexec_fn=limit,
                )
                out.seek(0)
                raw = out.read(MAX_BYTES + 1)
            except (OSError, subprocess.TimeoutExpired):
                raise SafeError("transport-unknown") from None
        return decode_response(raw, result.returncode)


class Controller:
    def __init__(self) -> None:
        self.path = STATE
        self.transport = GhTransport(STATE)
        self.clock = time.time
        self.offline = False

    @classmethod
    def offline_fixture(
        cls, path: str | Path, transport: Any, clock: Callable[[], float] = time.time
    ) -> Controller:
        """Imported test interface only; no CLI state/transport/auth override."""
        obj = cls()
        obj.path = Path(path)
        obj.transport = transport
        obj.clock = clock
        obj.offline = True
        return obj

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        old = os.umask(0o077)
        fd = None
        db = None
        try:
            if not self.offline:
                secure_dir(ROOT, create=True)
            secure_dir(self.path, create=True)
            # Includes all SQLite auxiliaries and private interrupted capture artifacts.
            for child in self.path.iterdir():
                secure_file(child)
            fd = os.open(
                self.path / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            secure_file(self.path / "lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafeError("locked") from None
            # Revalidate after lock (all cooperating invocations use this same lock).
            for child in self.path.iterdir():
                secure_file(child)
            db = sqlite3.connect(self.path / "state.sqlite", timeout=1)
            db.row_factory = sqlite3.Row
            db.executescript("""
              PRAGMA journal_mode=DELETE;
              CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intents(kind TEXT PRIMARY KEY, marker TEXT NOT NULL,
                body TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT);
              CREATE TABLE IF NOT EXISTS write_history(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                marker TEXT NOT NULL, hash TEXT NOT NULL, phase TEXT NOT NULL,
                outcome TEXT NOT NULL, receipt TEXT, observed_at REAL NOT NULL);
              CREATE TRIGGER IF NOT EXISTS write_history_no_update
                BEFORE UPDATE ON write_history BEGIN
                  SELECT RAISE(ABORT, 'append-only');
                END;
              CREATE TRIGGER IF NOT EXISTS write_history_no_delete
                BEFORE DELETE ON write_history BEGIN
                  SELECT RAISE(ABORT, 'append-only');
                END;
              CREATE TABLE IF NOT EXISTS events(id INTEGER, version TEXT, hash TEXT, status TEXT,
                raw TEXT, PRIMARY KEY(id,version));
              CREATE TABLE IF NOT EXISTS work(id INTEGER PRIMARY KEY, record TEXT NOT NULL);
            """)
            self.db = db
            # Upgrade existing state without inventing prior outcomes or effects.
            with db:
                db.execute(
                    "INSERT INTO write_history "
                    "(kind,marker,hash,phase,outcome,receipt,observed_at) "
                    "SELECT kind,marker,hash,'LEGACY_SNAPSHOT',status,receipt,? FROM intents i "
                    "WHERE NOT EXISTS (SELECT 1 FROM write_history h WHERE h.kind=i.kind)",
                    (self.clock(),),
                )
            yield
        finally:
            if db:
                db.close()
            if fd is not None:
                os.close(fd)
            os.umask(old)

    def get(self, key: str, default: str = "0") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return cast(str, row[0]) if row else default

    def put(self, key: str, value: str | int | float) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.db.commit()

    def request(self, method: str, endpoint: str, body: str | None = None) -> Any:
        if time.monotonic() >= self.deadline:
            raise SafeError("deadline")
        return self.transport.request(method, endpoint, body, self.deadline)

    def qualify(self) -> None:
        user = self.request("GET", "/user")
        repo = self.request("GET", BASE)
        pr = self.request("GET", f"{BASE}/pulls/{PR}")
        try:
            valid = (
                user["id"] == ACTOR
                and user["login"] == ACTOR_LOGIN
                and repo["id"] == REPO_ID
                and repo["full_name"] == REPO
                and repo["private"] is True
                and repo["has_issues"] is False
                and all(
                    repo["permissions"][x] is True
                    for x in ("pull", "push", "maintain", "triage")
                )
                and repo["permissions"]["admin"] is False
                and pr["number"] == PR
                and pr["state"] == "open"
                and pr["user"]["id"] == ACTOR
                and pr["base"]["repo"]["id"] == REPO_ID
                and pr["base"]["repo"]["full_name"] == REPO
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise SafeError("authority-mismatch")

    def pages(self) -> list[dict[str, Any]]:
        comments: dict[int, dict[str, Any]] = {}
        for page in range(1, MAX_PAGES + 1):
            data = self.request("GET", COMMENTS + "?per_page=100&page=" + str(page))
            if not isinstance(data, list) or len(data) > 100:
                raise SafeError("invalid-page")
            for item in data:
                try:
                    valid = (
                        type(item["id"]) is int
                        and item["id"] > 0
                        and type(item["user"]["id"]) is int
                        and isinstance(item["body"], str)
                        and item["issue_url"] == TARGET
                        and isinstance(item["updated_at"], str)
                    )
                except (KeyError, TypeError):
                    valid = False
                if not valid:
                    raise SafeError("invalid-comment")
                ident = item["id"]
                if ident in comments and comments[ident] != item:
                    raise SafeError("page-conflict")
                comments[ident] = item
            if len(data) < 100:
                return list(comments.values())
        raise SafeError("incomplete-pages")

    def exact(self, item: dict[str, Any], intent: sqlite3.Row) -> bool:
        return (
            type(item.get("id")) is int
            and item["id"] > 0
            and item.get("user", {}).get("id") == ACTOR
            and item.get("issue_url") == TARGET
            and item.get("body") == intent["body"]
            and digest(item["body"]) == intent["hash"]
        )

    def history(
        self, intent: sqlite3.Row, phase: str, outcome: str, receipt: str | None = None
    ) -> None:
        # Caller commits atomically with current status; never retain exception text.
        self.db.execute(
            "INSERT INTO write_history "
            "(kind,marker,hash,phase,outcome,receipt,observed_at) VALUES (?,?,?,?,?,?,?)",
            (
                intent["kind"],
                intent["marker"],
                intent["hash"],
                phase,
                outcome,
                receipt,
                self.clock(),
            ),
        )

    def confirm(
        self, intent: sqlite3.Row, item: dict[str, Any], phase: str = "POST_OUTCOME"
    ) -> None:
        receipt = json.dumps(
            {"id": item["id"], "author": ACTOR, "target": PR, "hash": intent["hash"]},
            sort_keys=True,
        )
        with self.db:
            self.db.execute(
                "UPDATE intents SET status=?,receipt=? WHERE kind=?",
                ("SUCCESS", receipt, intent["kind"]),
            )
            self.history(intent, phase, "SUCCESS", receipt)

    def reconcile(self, comments: list[dict[str, Any]]) -> None:
        for intent in self.db.execute(
            "SELECT * FROM intents WHERE status != 'SUCCESS'"
        ).fetchall():
            matches = [x for x in comments if self.exact(x, intent)]
            conflicts = [
                x
                for x in comments
                if intent["marker"] in x["body"] and not self.exact(x, intent)
            ]
            if conflicts or len(matches) > 1:
                with self.db:
                    self.db.execute(
                        "UPDATE intents SET status=? WHERE kind=?",
                        ("CONFLICT", intent["kind"]),
                    )
                    self.history(intent, "RECONCILE", "CONFLICT")
                raise SafeError("reconcile-conflict")
            if intent["status"] == "CONFLICT":
                raise SafeError("reconcile-conflict")
            if len(matches) == 1:
                self.confirm(intent, matches[0], "RECONCILE")
            else:
                with self.db:
                    self.history(intent, "RECONCILE", "MISSING_PENDING")
            # Absence is never permission to resend, including an interrupted INTENT.

    def post(self, kind: str, body: str, marker: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,NULL)",
                (kind, marker, body, digest(body), "INTENT"),
            )
            intent = self.db.execute(
                "SELECT * FROM intents WHERE kind=?", (kind,)
            ).fetchone()
            self.history(intent, "POST_ATTEMPT", "INTENT")
        try:
            item = self.request("POST", COMMENTS, body)
            if not isinstance(item, dict) or not self.exact(item, intent):
                raise SafeError("write-unconfirmed")
            self.confirm(intent, item)
        except Exception as exc:
            with self.db:
                self.db.execute(
                    "UPDATE intents SET status=? WHERE kind=?", ("AMBIGUOUS", kind)
                )
                self.history(intent, "POST_OUTCOME", "AMBIGUOUS")
            raise SafeError(
                "write-ambiguous", exc.delay if isinstance(exc, SafeError) else 300
            ) from None

    def cycle(self, action: str, interval: int) -> str:
        if not 1 <= interval <= 86400:
            raise SafeError("invalid-interval")
        with self.locked():
            if action in ("pause", "stop"):
                self.put(action, 1)
                return action
            if action == "resume":
                if self.get("stop") == "1":
                    return "stopped-operator-intervention-required"
                self.put("pause", 0)
                return "resumed"
            if self.get("stop") == "1":
                return "stopped"
            if self.get("pause") == "1":
                return "paused"
            now = self.clock()
            if now < float(self.get("backoff")) or (
                action == "once" and now < float(self.get("due"))
            ):
                return "not-due"
            self.deadline = time.monotonic() + DEADLINE
            try:
                self.qualify()
                comments = self.pages()
                self.reconcile(comments)
                if action == "seed-test":
                    if not self.db.execute(
                        "SELECT 1 FROM intents WHERE kind='seed'"
                    ).fetchone():
                        marker = "[omc-gh-01:seed:" + uuid.uuid4().hex + "]"
                        self.post("seed", SEED_TEXT + marker, marker)
                    return "seed-recorded-or-pending"
                seed = self.db.execute(
                    "SELECT * FROM intents WHERE kind='seed' AND status='SUCCESS'"
                ).fetchone()
                reply = self.db.execute(
                    "SELECT * FROM intents WHERE kind='reply' AND status='SUCCESS'"
                ).fetchone()
                approved = None
                for item in comments:
                    status = "QUARANTINED"
                    if (
                        reply
                        and self.exact(item, reply)
                        and item["id"] == json.loads(reply["receipt"])["id"]
                    ):
                        status = "SELF_LOOP"
                    elif (
                        seed
                        and self.exact(item, seed)
                        and item["id"] == json.loads(seed["receipt"])["id"]
                    ):
                        status = "APPROVED_SYNTHETIC"
                        approved = item
                    raw = json.dumps(item, sort_keys=True)
                    self.db.execute(
                        "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                        (item["id"], digest(raw), digest(item["body"]), status, raw),
                    )
                self.db.commit()
                # Complete snapshot only. Failed page sequences never reach this checkpoint.
                self.put("cursor", digest(json.dumps(comments, sort_keys=True)))
                if approved:
                    self.db.execute(
                        "INSERT OR IGNORE INTO work VALUES (?,?)",
                        (approved["id"], WORK),
                    )
                    self.db.commit()
                    if not self.db.execute(
                        "SELECT 1 FROM intents WHERE kind='reply'"
                    ).fetchone():
                        marker = "[omc-gh-01:reply:" + seed["marker"].split(":")[-1]
                        self.post("reply", REPLY_TEXT + marker, marker)
                self.put("due", now + interval)
                return "cycle-complete"
            except SafeError as exc:
                self.put("backoff", now + exc.delay)
                raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ("once", "pause", "resume", "stop", "seed-test"):
        group.add_argument(
            "--" + action, dest="action", action="store_const", const=action
        )
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if not (REPO and PR and ACTOR):
        print("not-configured")  # set github_poll in fleet.json
        return 0
    try:
        print(Controller().cycle(args.action, args.interval))
        return 0
    except SafeError as exc:
        print("FAIL " + exc.status)
        return 1
    except Exception:
        print("FAIL internal-error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
