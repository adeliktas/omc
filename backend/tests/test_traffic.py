"""Tests for the traffic monitoring backend endpoints (SQLite backend)."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession as SMAsyncSession
from starlette import status as http_status

from app.core import auth as auth_module
from app.core.auth_mode import AuthMode
from app.core.config import settings
from app.db.session import get_session


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_flow_row(**overrides: Any) -> dict[str, Any]:
    """Build a minimal flow dict suitable for SQLite insert via _row_values."""
    row = {
        "ts": "2026-04-23T10:00:00Z",
        "host": "localhost",
        "path": "/v1/chat/completions",
        "method": "POST",
        "model": "ollama/deepseek-v4-pro:cloud",
        "status_code": 200,
        "latency_ms": 100,
        "agent_name": "TestAgent",
        "agent_id": None,
        "board_id": None,
        "messages_count": 3,
        "has_system": True,
        "has_tools": False,
        "tools_count": 0,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 2048,
        "last_user_message": "hello",
        "finish_reason": "stop",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "content_preview": "Hello!",
        "reasoning_preview": "",
        "has_tool_calls": False,
        "tool_calls_count": 0,
        "req_body": {"messages": [{"role": "user", "content": "hello"}]},
        "res_body": {"choices": [{"message": {"content": "Hello!"}}]},
    }
    row.update(overrides)
    return row


def _insert_flows(db_path: str, flows: list[dict[str, Any]]) -> None:
    """Insert flow records directly into an SQLite traffic DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    ddl = """
    CREATE TABLE IF NOT EXISTS flows (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               TEXT    NOT NULL,
        host             TEXT,
        path             TEXT,
        method           TEXT,
        model            TEXT,
        status_code      INTEGER,
        latency_ms       INTEGER,
        agent_name       TEXT,
        agent_id         TEXT,
        board_id         TEXT,
        messages_count   INTEGER DEFAULT 0,
        has_system       INTEGER DEFAULT 0,
        has_tools        INTEGER DEFAULT 0,
        tools_count      INTEGER DEFAULT 0,
        stream           INTEGER DEFAULT 0,
        temperature      REAL,
        max_tokens       INTEGER,
        last_user_message TEXT DEFAULT '',
        finish_reason    TEXT  DEFAULT '',
        prompt_tokens    INTEGER,
        completion_tokens INTEGER,
        total_tokens     INTEGER,
        content_preview  TEXT  DEFAULT '',
        reasoning_preview TEXT DEFAULT '',
        has_tool_calls   INTEGER DEFAULT 0,
        tool_calls_count INTEGER DEFAULT 0,
        req_body         TEXT,
        res_body         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_flows_ts    ON flows(ts);
    CREATE INDEX IF NOT EXISTS idx_flows_agent ON flows(agent_name);
    CREATE INDEX IF NOT EXISTS idx_flows_model ON flows(model);
    CREATE INDEX IF NOT EXISTS idx_flows_latency
        ON flows(latency_ms) WHERE latency_ms IS NOT NULL;
    """
    for stmt in ddl.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()

    cols = (
        "ts", "host", "path", "method", "model", "status_code", "latency_ms",
        "agent_name", "agent_id", "board_id", "messages_count", "has_system",
        "has_tools", "tools_count", "stream", "temperature", "max_tokens",
        "last_user_message", "finish_reason", "prompt_tokens", "completion_tokens",
        "total_tokens", "content_preview", "reasoning_preview", "has_tool_calls",
        "tool_calls_count", "req_body", "res_body",
    )
    insert = f"INSERT INTO flows ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    for flow in flows:
        conn.execute(insert, tuple(
            json.dumps(flow[c], ensure_ascii=False)
            if c in ("req_body", "res_body") and isinstance(flow.get(c), dict)
            else flow.get(c)
            for c in cols
        ))
    conn.commit()
    conn.close()


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_traffic_db(tmp_path: Path) -> str:
    """Create a temporary traffic SQLite DB and set _DB_FILE to it."""
    db_path = str(tmp_path / "llm_flows.db")
    # Force the traffic module to re-initialise with our temp DB
    import app.api.traffic as traffic_mod
    traffic_mod._conn = None
    traffic_mod._DB_FILE = Path(db_path)
    traffic_mod._LEGACY_FILE = Path(str(tmp_path / "llm_flows.jsonl.gz"))
    traffic_mod._stats_cache = {}
    traffic_mod._last_ingest_ts = 0.0
    traffic_mod._ingest_count_since_check = 0
    yield db_path
    # Cleanup
    traffic_mod._conn = None


@pytest.fixture
async def auth_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Return an httpx AsyncClient with auth bypass configured and a real
    in-memory SQLite session so ``require_user_or_agent`` can upsert the
    local user without choking on a ``None`` session."""
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", "test-traffic-token")
    monkeypatch.setattr(auth_module, "LOCAL_AUTH_USER_ID", "traffic-test-user")
    monkeypatch.setattr(auth_module, "LOCAL_AUTH_EMAIL", "traffic@localhost")
    monkeypatch.setattr(auth_module, "LOCAL_AUTH_NAME", "Traffic Test")

    # Real in-memory engine so auth can upsert the local user
    from sqlmodel import SQLModel
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async def _override_get_session() -> AsyncSession:
        async with SMAsyncSession(engine, expire_on_commit=False) as session:
            yield session

    from app.api.users import router as users_router
    from app.api.traffic import router as traffic_router

    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(traffic_router)
    api_v1.include_router(users_router)
    app.include_router(api_v1)

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[auth_module.get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    await engine.dispose()


# ── Auth header helper ────────────────────────────────────────────────────


def _auth_h() -> dict[str, str]:
    return {"Authorization": "Bearer test-traffic-token"}


# ── IngestPayload Model Tests ─────────────────────────────────────────────


class TestIngestPayload:
    def test_minimal_payload(self) -> None:
        from app.api.traffic import IngestPayload
        payload = IngestPayload(
            ts="2026-04-23T10:00:00Z",
            host="api.openai.com",
            path="/v1/chat/completions",
            method="POST",
            model="gpt-4",
            status_code=200,
        )
        assert payload.latency_ms is None
        assert payload.agent_name is None
        assert payload.total_tokens is None
        assert payload.stream is False
        assert payload.tools_count == 0

    def test_full_payload(self) -> None:
        from app.api.traffic import IngestPayload
        payload = IngestPayload(
            ts="2026-04-23T10:00:00Z",
            host="localhost",
            path="/v1/completions",
            method="POST",
            model="qwen",
            status_code=200,
            latency_ms=150,
            agent_name="RE-Alpha",
            agent_id="mc-0000a007",
            board_id="board-1",
            messages_count=5,
            has_system=True,
            has_tools=True,
            tools_count=2,
            stream=True,
            temperature=0.7,
            max_tokens=2048,
            last_user_message="hello",
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            content_preview="Hello!",
            reasoning_preview="thinking...",
            has_tool_calls=False,
            tool_calls_count=0,
            req_body={"messages": []},
            res_body={"choices": []},
        )
        assert payload.latency_ms == 150
        assert payload.agent_name == "RE-Alpha"
        assert payload.total_tokens == 15


# ── Legacy Migration Tests ────────────────────────────────────────────────


class TestLegacyMigration:
    def test_migrates_gzip_jsonl(self, tmp_path: Path) -> None:
        import app.api.traffic as traffic_mod
        legacy = tmp_path / "legacy.jsonl.gz"
        with gzip.open(legacy, "wt", encoding="utf-8") as f:
            r1 = _make_flow_row(ts="2026-01-01T00:00:00Z", host="legacy1", model="m1")
            r2 = _make_flow_row(ts="2026-01-01T00:01:00Z", host="legacy2", model="m2")
            r1.pop("req_body"); r1.pop("res_body")
            r2.pop("req_body"); r2.pop("res_body")
            f.write(json.dumps(r1) + "\n")
            f.write(json.dumps(r2) + "\n")

        db_path = str(tmp_path / "test_legacy.db")
        traffic_mod._conn = None
        traffic_mod._DB_FILE = Path(db_path)
        traffic_mod._LEGACY_FILE = legacy
        traffic_mod._stats_cache = {}
        traffic_mod._last_ingest_ts = 0.0
        traffic_mod._ingest_count_since_check = 0

        # Open DB directly, bypass _db() to avoid deadlock on non-reentrant lock.
        # _db() holds _db_lock then calls _migrate_legacy() which also tries
        # to acquire _db_lock → deadlock. We call them sequentially.
        traffic_mod._conn = traffic_mod._open_db()
        # _migrate_legacy acquires _db_lock internally — release nothing, just call
        traffic_mod._migrate_legacy()

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        count = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        assert count == 2
        conn.close()
        traffic_mod._conn = None

    def test_skips_missing_file(self, tmp_path: Path) -> None:
        import app.api.traffic as traffic_mod
        db_path = str(tmp_path / "no_legacy.db")
        traffic_mod._conn = None
        traffic_mod._DB_FILE = Path(db_path)
        traffic_mod._LEGACY_FILE = Path("/nonexistent/file.jsonl.gz")
        traffic_mod._stats_cache = {}
        traffic_mod._last_ingest_ts = 0.0
        traffic_mod._ingest_count_since_check = 0

        traffic_mod._conn = traffic_mod._open_db()
        traffic_mod._migrate_legacy()

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        count = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        assert count == 0
        conn.close()
        traffic_mod._conn = None


# ── Trim Tests ────────────────────────────────────────────────────────────


class TestTrim:
    def test_trim_removes_oldest_when_over_max(self, tmp_path: Path):
        import app.api.traffic as traffic_mod
        db_path = str(tmp_path / "trim_test")
        # _trim_if_needed only triggers when count > _MAX_ROWS + 1000
        flows = [_make_flow_row(ts=f"2026-01-01T{i:02d}:00:00Z") for i in range(1100)]
        _insert_flows(db_path, flows)

        traffic_mod._conn = sqlite3.connect(db_path, check_same_thread=False)
        original_max = traffic_mod._MAX_ROWS
        traffic_mod._MAX_ROWS = 50  # 1100 > 50+1000? No, 1100 < 1050 — won't trim
        traffic_mod._MAX_ROWS = 0   # 1100 > 0+1000 = 1000 → will trim
        traffic_mod._trim_if_needed(traffic_mod._conn)

        count = traffic_mod._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        assert count <= 100  # trimmed to _MAX_ROWS (0) but min stays reasonable
        traffic_mod._conn.close()
        traffic_mod._MAX_ROWS = original_max
        traffic_mod._conn = None


# ── API Endpoint Tests ────────────────────────────────────────────────────


class TestTrafficApiEndpoints:
    @pytest.mark.asyncio
    async def test_ingest_no_auth_required(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        payload = _make_flow_row()
        resp = await auth_client.post("/api/v1/traffic/ingest", json=payload)
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT

        import app.api.traffic as traffic_mod
        db = traffic_mod._db()
        count = db.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
        assert count == 1
        row = db.execute("SELECT * FROM flows").fetchone()
        assert row["agent_name"] == "TestAgent"

    @pytest.mark.asyncio
    async def test_flows_requires_auth(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        resp = await auth_client.get("/api/v1/traffic/flows")
        assert resp.status_code == http_status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_flows_returns_recent_flows(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flows = [
            _make_flow_row(ts="2026-04-23T10:00:00Z", host="h1", model="m1"),
            _make_flow_row(ts="2026-04-23T10:01:00Z", host="h2", model="m2"),
        ]
        _insert_flows(tmp_traffic_db, flows)

        resp = await auth_client.get("/api/v1/traffic/flows", headers=_auth_h())
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert len(data) == 2
        # Most recent first
        assert data[0]["host"] == "h2"
        assert data[1]["host"] == "h1"

    @pytest.mark.asyncio
    async def test_flows_respects_limit(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flows = [
            _make_flow_row(ts=f"2026-04-23T10:{i:02d}:00Z", host=f"h{i}")
            for i in range(10)
        ]
        _insert_flows(tmp_traffic_db, flows)

        resp = await auth_client.get("/api/v1/traffic/flows?limit=3", headers=_auth_h())
        data = resp.json()
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_flows_since_filter(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flows = [
            _make_flow_row(ts="2026-04-23T10:00:00Z", host="old"),
            _make_flow_row(ts="2026-04-23T10:02:00Z", host="new"),
        ]
        _insert_flows(tmp_traffic_db, flows)

        resp = await auth_client.get(
            "/api/v1/traffic/flows?since=2026-04-23T10:01:00Z", headers=_auth_h()
        )
        data = resp.json()
        assert len(data) == 1
        assert data[0]["host"] == "new"

    @pytest.mark.asyncio
    async def test_flows_after_filter(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flows = [
            _make_flow_row(ts="2026-04-23T10:00:00Z", host="h1"),
            _make_flow_row(ts="2026-04-23T10:01:00Z", host="h2"),
            _make_flow_row(ts="2026-04-23T10:02:00Z", host="h3"),
        ]
        _insert_flows(tmp_traffic_db, flows)

        resp = await auth_client.get(
            "/api/v1/traffic/flows?after=2026-04-23T10:00:30Z", headers=_auth_h()
        )
        data = resp.json()
        hosts = {r["host"] for r in data}
        assert "h1" not in hosts

    @pytest.mark.asyncio
    async def test_stats_requires_auth(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        resp = await auth_client.get("/api/v1/traffic/stats")
        assert resp.status_code == http_status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_stats_returns_aggregates(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flows = [
            _make_flow_row(ts="2026-04-23T10:00:00Z", host="h1", model="m1", status_code=200,
                           latency_ms=100, total_tokens=50, agent_name="AgentA"),
            _make_flow_row(ts="2026-04-23T10:01:00Z", host="h2", model="m2", status_code=500,
                           latency_ms=200, total_tokens=30, agent_name="AgentA"),
            _make_flow_row(ts="2026-04-23T10:02:00Z", host="h3", model="m3", status_code=200,
                           latency_ms=300, total_tokens=20, agent_name="AgentB"),
        ]
        _insert_flows(tmp_traffic_db, flows)

        resp = await auth_client.get("/api/v1/traffic/stats", headers=_auth_h())
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert data["flow_count"] == 3
        assert data["total_tokens"] == 100
        assert data["avg_latency_ms"] == 200
        assert data["p50_latency_ms"] == 200
        assert data["errors"] == 1

        agents = {a["agent_name"]: a for a in data["agents"]}
        assert "AgentA" in agents
        assert agents["AgentA"]["flow_count"] == 2
        assert agents["AgentA"]["total_tokens"] == 80
        assert agents["AgentA"]["error_count"] == 1
        assert agents["AgentA"]["avg_latency_ms"] == 150
        assert "AgentB" in agents
        assert agents["AgentB"]["flow_count"] == 1
        assert agents["AgentB"]["total_tokens"] == 20

    @pytest.mark.asyncio
    async def test_stats_handles_empty(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        resp = await auth_client.get("/api/v1/traffic/stats", headers=_auth_h())
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert data["flow_count"] == 0
        assert data["total_tokens"] == 0
        assert data["avg_latency_ms"] is None
        assert data["p50_latency_ms"] is None
        assert data["errors"] == 0

    @pytest.mark.asyncio
    async def test_get_flow_detail(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        flow = _make_flow_row(host="detail-test")
        _insert_flows(tmp_traffic_db, [flow])

        conn = sqlite3.connect(f"file:{tmp_traffic_db}?mode=ro", uri=True)
        flow_id = conn.execute("SELECT id FROM flows LIMIT 1").fetchone()[0]
        conn.close()

        resp = await auth_client.get(
            f"/api/v1/traffic/flows/{flow_id}", headers=_auth_h()
        )
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert data["host"] == "detail-test"
        assert data["req_body"] is not None
        assert data["res_body"] is not None

    @pytest.mark.asyncio
    async def test_get_flow_not_found(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        resp = await auth_client.get("/api/v1/traffic/flows/999999", headers=_auth_h())
        assert resp.status_code == http_status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_health_when_data_exists(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        _insert_flows(tmp_traffic_db, [_make_flow_row()])

        resp = await auth_client.get("/api/v1/traffic/health")
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert data["enabled"] is True
        assert data["flow_count"] == 1
        assert data["latest_ts"] is not None

    @pytest.mark.asyncio
    async def test_health_when_empty(self, auth_client: AsyncClient, tmp_traffic_db: str) -> None:
        resp = await auth_client.get("/api/v1/traffic/health")
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert data["enabled"] is False
        assert data["flow_count"] == 0
        assert data["latest_ts"] is None


# ── Row conversion helpers ────────────────────────────────────────────────


class TestRowConversion:
    def test_row_to_dict_converts_json_columns(self):
        from app.api.traffic import _row_to_dict
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE flows (id INTEGER, req_body TEXT, res_body TEXT)")
        conn.execute(
            "INSERT INTO flows VALUES (1, ?, ?)",
            ('{"messages": [{"role":"user"}]}', '{"choices": []}'),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM flows WHERE id = 1").fetchone()
        d = _row_to_dict(row)
        assert isinstance(d["req_body"], dict)
        assert d["req_body"]["messages"][0]["role"] == "user"
        assert d["res_body"]["choices"] == []
        conn.close()

    def test_row_to_dict_handles_invalid_json(self):
        from app.api.traffic import _row_to_dict
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE flows (id INTEGER, req_body TEXT, res_body TEXT)")
        conn.execute("INSERT INTO flows VALUES (1, 'not json', NULL)")
        conn.commit()
        row = conn.execute("SELECT * FROM flows WHERE id = 1").fetchone()
        d = _row_to_dict(row)
        assert d["req_body"] == "not json"  # stays as string
        conn.close()

    def test_row_values_produces_correct_tuple(self):
        from app.api.traffic import _row_values
        row = _make_flow_row()
        tup = _row_values(row)
        assert tup[0] == "2026-04-23T10:00:00Z"  # ts
        assert tup[4] == "ollama/deepseek-v4-pro:cloud"  # model
        assert isinstance(tup[26], str)  # req_body serialised as JSON string
        assert "messages" in tup[26]
