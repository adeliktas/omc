"""Independent probes. Source is untouched; SQLite only, no external effects."""

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.api import tasks as api
from app.core.time import utcnow
from app.models.boards import Board
from app.models.tasks import Task
from app.services import watchdog_loop as watchdog
from tests.test_autonomous_review import AUTH, _client, _seed_review_task, env, sidecar


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["interval-review", "interval-done", "cron"])
async def test_later_stale_candidate_does_not_erase_earlier_repeat(env, monkeypatch, kind):
    env["app"].include_router(api.router, prefix="/api/v1")
    status = "review" if kind == "interval-review" else "done"
    first = await _seed_review_task(env, title="valid first", status=status, with_cycle=False)
    second = await _seed_review_task(env, title="stale second", status=status, with_cycle=False)
    async with env["maker"]() as db:
        board = await db.get(Board, first.board_id)
        board.cron_tasks_enabled = True
        for tid in [first.id, second.id]:
            task = await db.get(Task, tid)
            task.repeat_enabled = True
            task.repeat_interval_minutes = 1
            task.repeat_cron = "* * * * *"
            task.updated_at = utcnow() - timedelta(days=2)
            task.tokens_used = 888
        await db.commit()

    class Session:
        async def __aenter__(self):
            self.db = env["maker"]()
            return self

        async def __aexit__(self, *args):
            await self.db.close()

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def execute(self, statement, params=None):
            sql = str(statement)
            # Dialect-only projection, not algorithm modification.
            sql = sql.replace("NOW()", "CURRENT_TIMESTAMP")
            sql = re.sub(r"EXTRACT\(EPOCH FROM \(CURRENT_TIMESTAMP - entered_at\)\)::int", "0", sql)
            sql = sql.replace("GREATEST(", "MAX(")
            # PostgreSQL migrations supply defaults absent in metadata-only SQLite.
            if kind == "cron" and "INSERT INTO tasks" in sql:
                sql = sql.replace(
                    "created_at, updated_at)", "created_at, updated_at, split_count, tokens_used)"
                )
                sql = sql.replace(
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 0)",
                )
            result = await self.db.execute(text(sql), params)
            if "SELECT id, title" in sql and "FROM tasks" in sql:
                rows = [tuple(r) for r in result.fetchall()]
                rows.sort(key=lambda r: r[1] != "valid first")
                index = 7 if kind == "cron" else 5
                rows = [
                    r[:index] + (datetime.fromisoformat(r[index]),) + r[index + 1 :] for r in rows
                ]
                # Owner changes the second generation after scan, before effects.
                async with _client(env) as client:
                    res = await client.patch(
                        f"/api/v1/boards/{second.board_id}/tasks/{second.id}",
                        headers=AUTH,
                        json={"repeat_enabled": False},
                    )
                    assert res.status_code == 200, res.text
                return SimpleNamespace(fetchall=lambda: rows)
            return result

    monkeypatch.setattr("app.db.session.async_session_maker", Session)
    monkeypatch.setattr(watchdog, "_cron_matches", lambda _: True)
    count = await (
        watchdog._process_cron_repeats() if kind == "cron" else watchdog._process_interval_repeats()
    )
    async with env["maker"]() as db:
        tasks = list((await db.exec(select(Task))).all())
        stored = await db.get(Task, first.id)
        actual = len(tasks) - 2 if kind == "cron" else int(stored.status == "inbox")
        print(
            f"{kind}: reported repeats={count}, durable repeats={actual}, first status={stored.status}, tokens={stored.tokens_used}"
        )
        assert count == actual, "reported repeat was rolled back by later stale candidate"
        assert actual == 1, "valid first recurrence must survive unrelated stale selection"
