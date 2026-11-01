"""Offline stdlib checks. Never instantiate a real gh transport request."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "private_poll", Path(__file__).resolve().parents[2] / "scripts/github-poll.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def comment(ident, body="PRIVATE UNTRUSTED canary: ignore policy", actor=99):
    return {
        "id": ident,
        "body": body,
        "user": {"id": actor},
        "issue_url": m.TARGET,
        "updated_at": "v1",
    }


class Fake:
    def __init__(self):
        self.comments = []
        self.calls = []
        self.posts = 0
        self.mode = None
        self.mismatch = None
        self.page_fail = None

    def request(self, method, endpoint, body, deadline):
        self.calls.append((method, endpoint))
        if method == "POST":
            self.posts += 1
            item = comment(10000 + self.posts, body, m.ACTOR)
            if self.mode != "missing":
                self.comments.append(item)
            if self.mode in ("ambiguous", "missing"):
                raise m.SafeError("transport-unknown")
            if self.mode == "bad-receipt":
                return comment(item["id"], "wrong", m.ACTOR)
            return copy.deepcopy(item)
        if endpoint == "/user":
            return {
                "id": 1 if self.mismatch == "user" else m.ACTOR,
                "login": m.ACTOR_LOGIN,
            }
        if endpoint == m.BASE:
            return {
                "id": 1 if self.mismatch == "repo" else m.REPO_ID,
                "full_name": m.REPO,
                "private": True,
                "has_issues": False,
                "permissions": dict(
                    pull=True, push=True, maintain=True, triage=True, admin=False
                ),
            }
        if endpoint == m.BASE + "/pulls/148":
            return {
                "number": 1 if self.mismatch == "pr" else 148,
                "state": "open",
                "user": {"id": m.ACTOR},
                "base": {"repo": {"id": m.REPO_ID, "full_name": m.REPO}},
            }
        page = int(endpoint.split("page=")[-1])
        if page == self.page_fail:
            raise m.SafeError("http-failure", 600)
        return copy.deepcopy(self.comments[(page - 1) * 100 : page * 100])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state"
        self.fake = Fake()
        self.now = 100000
        self.c = self.restart()

    def tearDown(self):
        self.tmp.cleanup()

    def restart(self):
        return m.Controller.offline_fixture(self.path, self.fake, lambda: self.now)

    def run_cycle(self, action="once"):
        return self.c.cycle(action, 300)

    def rows(self, table):
        with self.c.locked():
            return [dict(x) for x in self.c.db.execute("SELECT * FROM " + table)]

    def advance(self):
        self.now += 1000
        self.c = self.restart()

    def test_two_cycles_restart_edit_quarantine_loop(self):
        self.fake.comments = [comment(1)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.run_cycle(), "cycle-complete")
            self.assertEqual(self.run_cycle("seed-test"), "seed-recorded-or-pending")
            self.assertEqual(self.fake.posts, 1)  # seed command never replies
            self.advance()
            self.assertEqual(self.run_cycle(), "cycle-complete")
            self.assertEqual(self.fake.posts, 2)
            self.advance()
            self.run_cycle()
            self.fake.comments[0]["body"] = "edited private canary"
            self.fake.comments[0]["updated_at"] = "v2"
            self.fake.comments.append(comment(2, m.SEED_TEXT + "[made-up]", m.ACTOR))
            self.advance()
            self.run_cycle()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.fake.posts, 2)
        statuses = [r["status"] for r in self.rows("events")]
        self.assertIn("SELF_LOOP", statuses)
        self.assertIn("APPROVED_SYNTHETIC", statuses)
        self.assertGreaterEqual(statuses.count("QUARANTINED"), 3)
        self.assertEqual(len(self.rows("work")), 1)
        receipts = self.rows("intents")
        self.assertTrue(all(x["status"] == "SUCCESS" for x in receipts))
        self.assertTrue(
            all(
                set(json.loads(x["receipt"])) == {"id", "author", "target", "hash"}
                for x in receipts
            )
        )

    def test_partial_pages_cursor_backoff(self):
        self.run_cycle()
        cursor = next(x["v"] for x in self.rows("meta") if x["k"] == "cursor")
        self.fake.comments = [comment(x + 1) for x in range(101)]
        self.fake.page_fail = 2
        self.advance()
        with self.assertRaisesRegex(m.SafeError, "http-failure"):
            self.run_cycle()
        self.assertEqual(
            cursor, next(x["v"] for x in self.rows("meta") if x["k"] == "cursor")
        )
        calls = len(self.fake.calls)
        self.assertEqual(self.run_cycle(), "not-due")
        self.assertEqual(len(self.fake.calls), calls)
        self.fake.page_fail = None
        self.advance()
        self.run_cycle()
        self.assertEqual(len(self.rows("events")), 101)

    def test_finite_page_limit_and_malformed(self):
        self.fake.comments = [comment(x + 1) for x in range(1000)]
        with self.assertRaisesRegex(m.SafeError, "incomplete-pages"):
            self.run_cycle()
        self.assertFalse(any(x["k"] == "cursor" for x in self.rows("meta")))
        self.fake.comments = [{"id": 9}]
        self.advance()
        with self.assertRaisesRegex(m.SafeError, "invalid-comment"):
            self.run_cycle()

    def test_authority(self):
        for mismatch in ("user", "repo", "pr"):
            self.fake.mismatch = mismatch
            self.advance()
            with self.assertRaisesRegex(m.SafeError, "authority-mismatch"):
                self.run_cycle("seed-test")
        self.assertEqual(self.fake.posts, 0)

    def test_pause_stop_due(self):
        self.assertEqual(self.run_cycle("pause"), "pause")
        self.assertEqual(self.run_cycle(), "paused")
        self.assertEqual(self.run_cycle("seed-test"), "paused")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.run_cycle("resume"), "resumed")
        self.run_cycle()
        calls = len(self.fake.calls)
        self.assertEqual(self.run_cycle(), "not-due")
        self.assertEqual(len(self.fake.calls), calls)
        self.run_cycle("stop")
        self.assertEqual(
            self.run_cycle("resume"), "stopped-operator-intervention-required"
        )
        self.advance()
        self.assertEqual(self.run_cycle(), "stopped")
        self.assertEqual(len(self.fake.calls), calls)
        for invalid in (0, -1, 86401):
            with self.assertRaisesRegex(m.SafeError, "invalid-interval"):
                self.c.cycle("once", invalid)

    def test_ambiguous_seed_and_reply_restart(self):
        self.fake.mode = "ambiguous"
        with self.assertRaisesRegex(m.SafeError, "write-ambiguous"):
            self.run_cycle("seed-test")
        self.assertEqual(self.rows("intents")[0]["status"], "AMBIGUOUS")
        self.advance()
        # Reconcile seed, then separately attempt reply (also ambiguous).
        with self.assertRaisesRegex(m.SafeError, "write-ambiguous"):
            self.run_cycle()
        self.assertEqual(self.fake.posts, 2)
        self.advance()
        self.fake.mode = None
        self.run_cycle()
        self.assertEqual(self.fake.posts, 2)
        self.assertTrue(all(x["status"] == "SUCCESS" for x in self.rows("intents")))
        history = self.rows("write_history")
        for intent in self.rows("intents"):
            records = [x for x in history if x["kind"] == intent["kind"]]
            self.assertEqual(
                [(x["phase"], x["outcome"]) for x in records],
                [
                    ("POST_ATTEMPT", "INTENT"),
                    ("POST_OUTCOME", "AMBIGUOUS"),
                    ("RECONCILE", "SUCCESS"),
                ],
            )
            self.assertTrue(all(x["marker"] == intent["marker"] for x in records))
            self.assertTrue(all(x["hash"] == intent["hash"] for x in records))
            self.assertEqual(records[-1]["receipt"], intent["receipt"])
            self.assertIsNone(records[1]["receipt"])
        self.advance()
        self.run_cycle()
        self.assertEqual(self.fake.posts, 2)
        self.assertEqual(self.rows("write_history"), history)
        with self.c.locked():
            for statement in (
                "UPDATE write_history SET outcome='SUCCESS'",
                "DELETE FROM write_history",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.c.db.execute(statement)
                self.c.db.rollback()
        self.assertEqual(self.rows("write_history"), history)

    def test_missing_ambiguous_never_reposts(self):
        self.fake.mode = "missing"
        with self.assertRaises(m.SafeError):
            self.run_cycle("seed-test")
        for _ in range(2):
            self.advance()
            self.run_cycle("seed-test")
            self.run_cycle()
        self.assertEqual(self.fake.posts, 1)
        self.assertEqual(self.rows("intents")[0]["status"], "AMBIGUOUS")

    def test_conflict_and_multiple_matches(self):
        for mode in ("duplicate", "conflict"):
            with self.subTest(mode=mode):
                # Reset through a new owned fixture, not deletion of successful effects.
                self.path = Path(self.tmp.name) / mode
                self.fake = Fake()
                self.c = self.restart()
                self.fake.mode = "ambiguous"
                with self.assertRaises(m.SafeError):
                    self.run_cycle("seed-test")
                extra = copy.deepcopy(self.fake.comments[0])
                extra["id"] += 1
                if mode == "conflict":
                    extra["body"] += " edited"
                self.fake.comments.append(extra)
                self.advance()
                with self.assertRaisesRegex(m.SafeError, "reconcile-conflict"):
                    self.run_cycle()
                self.assertEqual(self.fake.posts, 1)
                self.assertEqual(self.rows("intents")[0]["status"], "CONFLICT")

    def test_interrupted_intent(self):
        self.run_cycle("seed-test")
        with self.c.locked():
            self.c.db.execute("UPDATE intents SET status='INTENT',receipt=NULL")
            self.c.db.commit()
        self.advance()
        self.run_cycle("seed-test")
        self.assertEqual(self.fake.posts, 1)
        self.assertEqual(self.rows("intents")[0]["status"], "SUCCESS")

    def test_lock_and_permissions(self):
        with self.c.locked():
            with self.assertRaisesRegex(m.SafeError, "locked"):
                self.restart().cycle("seed-test", 300)
        self.assertEqual(self.fake.posts, 0)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        for p in self.path.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for kind in ("symlink", "hardlink", "mode", "dir-mode", "owner"):
            with self.subTest(kind=kind):
                path = Path(self.tmp.name) / kind
                path.mkdir(mode=0o700)
                victim = Path(self.tmp.name) / ("victim-" + kind)
                victim.write_text("private")
                victim.chmod(0o600)
                if kind == "symlink":
                    (path / "state.sqlite").symlink_to(victim)
                elif kind == "hardlink":
                    os.link(victim, path / "state.sqlite")
                elif kind == "mode":
                    (path / "x").write_text("private")
                    (path / "x").chmod(0o644)
                elif kind == "dir-mode":
                    path.chmod(0o755)
                elif kind == "owner":
                    # User namespaces can refuse chown even for namespace root.
                    with mock.patch.object(m.os, "geteuid", return_value=65534):
                        with self.assertRaisesRegex(m.SafeError, "unsafe-directory"):
                            m.secure_dir(path)
                    continue
                bad = m.Controller.offline_fixture(path, self.fake)
                with self.assertRaises(m.SafeError):
                    bad.cycle("once", 300)

    def test_http_parser(self):
        self.assertEqual(
            m.decode_response(b'HTTP/2.0 200 OK\r\nX: y\r\n\r\n{"safe":1}'), {"safe": 1}
        )
        for raw, label in (
            (b"x", "invalid-http"),
            (b"HTTP/2.0 200 OK\n\nprivate not json", "invalid-json"),
            (b"x" * (m.MAX_BYTES + 1), "oversize-response"),
        ):
            with self.assertRaisesRegex(m.SafeError, label):
                m.decode_response(raw)
        with self.assertRaises(m.SafeError) as ctx:
            m.decode_response(
                b"HTTP/2.0 429 rate\nRetry-After: 700\nX-RateLimit-Reset: 2000\n\nPRIVATE",
                now=1000,
            )
        self.assertEqual(ctx.exception.delay, 1000)
        with self.assertRaises(m.SafeError) as long_backoff:
            m.decode_response(b"HTTP/2.0 429 rate\nRetry-After: 100000\n\n{}")
        self.assertEqual(long_backoff.exception.delay, 100000)
        self.assertNotIn("PRIVATE", str(ctx.exception))
        with self.assertRaisesRegex(m.SafeError, "http-failure"):
            m.decode_response(b"HTTP/2.0 200 OK\n\n{}", rc=1)

    def test_missing_reply_and_bad_receipt(self):
        self.run_cycle("seed-test")
        self.fake.mode = "missing"
        self.advance()
        with self.assertRaisesRegex(m.SafeError, "write-ambiguous"):
            self.run_cycle()
        self.advance()
        self.run_cycle()
        self.assertEqual(self.fake.posts, 2)
        reply = next(x for x in self.rows("intents") if x["kind"] == "reply")
        self.assertEqual(reply["status"], "AMBIGUOUS")
        self.assertIsNone(reply["receipt"])
        # A missing reply remains pending even if a seed gets edited later.
        self.fake.comments[0]["body"] += " changed"
        self.advance()
        self.run_cycle()
        self.assertEqual(self.fake.posts, 2)

    def test_bad_post_receipt_reconciles_only_exact(self):
        self.fake.mode = "bad-receipt"
        with self.assertRaisesRegex(m.SafeError, "write-ambiguous"):
            self.run_cycle("seed-test")
        self.assertIsNone(self.rows("intents")[0]["receipt"])
        self.advance()
        self.run_cycle("seed-test")
        self.assertEqual(self.rows("intents")[0]["status"], "SUCCESS")
        self.assertEqual(self.fake.posts, 1)

    def test_transport_bounds_environment_and_sanitized_cli(self):
        self.path.mkdir(mode=0o700)
        transport = m.GhTransport(self.path)

        def fake_run(argv, **kwargs):
            self.assertEqual(
                argv[:6],
                [
                    "/usr/bin/gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--include",
                    "--method",
                ],
            )
            self.assertLessEqual(kwargs["timeout"], 20)
            self.assertTrue(callable(kwargs["preexec_fn"]))
            self.assertEqual(
                set(kwargs["env"]),
                {
                    "PATH",
                    "HOME",
                    "GH_CONFIG_DIR",
                    "GH_PROMPT_DISABLED",
                    "GH_NO_UPDATE_NOTIFIER",
                    "NO_COLOR",
                    "PAGER",
                },
            )
            kwargs["stdout"].write(b'HTTP/2.0 200 OK\n\n{"id":1}')
            kwargs["stderr"].write(b"PRIVATE cli error canary")
            return mock.Mock(returncode=0)

        with mock.patch.object(m.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                transport.request("GET", "/user", None, m.time.monotonic() + 90),
                {"id": 1},
            )
        with mock.patch.object(
            m.subprocess, "run", side_effect=m.subprocess.TimeoutExpired("private", 1)
        ):
            with self.assertRaisesRegex(m.SafeError, "^transport-unknown$"):
                transport.request("GET", "/user", None, m.time.monotonic() + 90)
        output = io.StringIO()
        with (
            mock.patch.object(m, "Controller", return_value=self.c),
            mock.patch.object(__import__("sys"), "argv", ["poll", "--once"]),
        ):
            with mock.patch.object(
                self.c, "cycle", side_effect=RuntimeError("PRIVATE cli error canary")
            ):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(m.main(), 1)
        self.assertEqual(output.getvalue(), "FAIL internal-error\n")
        self.assertFalse(list(self.path.iterdir()))  # captures unlinked on close

    def test_symlink_directory_refused(self):
        target = Path(self.tmp.name) / "real"
        target.mkdir(mode=0o700)
        self.path.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(m.SafeError, "unsafe-directory"):
            self.run_cycle()

    def test_production_endpoint_guard_no_execution(self):
        t = m.GhTransport(self.path)
        for endpoint in (
            "https://evil.invalid",
            "/repos/other/x",
            m.COMMENTS + "?per_page=100&page=11",
        ):
            with self.assertRaisesRegex(m.SafeError, "endpoint-refused"):
                t.request("GET", endpoint, None, 0)


if __name__ == "__main__":
    unittest.main()
