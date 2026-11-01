"""The live ETL writer enforces TRAFFIC_MAX_ROWS (llm_flows.db had grown to 5.8 GB)."""

import sqlite3

import pytest

from app.api import traffic
from app.services import traffic_etl


@pytest.mark.asyncio
async def test_poll_trims_to_cap(monkeypatch, tmp_path):
    db = tmp_path / "flows.db"
    conn = sqlite3.connect(db)
    conn.executescript(traffic._DDL)
    conn.execute("ALTER TABLE flows ADD COLUMN omniroute_id TEXT")  # added after _DDL
    conn.executemany("INSERT INTO flows (ts) VALUES (?)", [(f"2026-01-01T00:00:{i:05d}",) for i in range(1100)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(traffic_etl, "_FLOWS_DB", str(db))
    monkeypatch.setattr(traffic, "_MAX_ROWS", 50)

    async def fake_logs(_limit):
        return [{"id": "new-1"}]

    async def fake_detail(_id):
        return None

    monkeypatch.setattr(traffic_etl, "_fetch_call_logs", fake_logs)
    monkeypatch.setattr(traffic_etl, "_fetch_call_log_detail", fake_detail)
    monkeypatch.setattr(traffic_etl, "_insert_flow_with_bodies", lambda r, d: True)

    assert await traffic_etl.poll_once() == 1
    conn = sqlite3.connect(db)
    count, oldest = conn.execute("SELECT COUNT(*), MIN(ts) FROM flows").fetchone()
    assert count == 50
    assert oldest == "2026-01-01T00:00:01050"  # newest rows kept
