"""T111 — traffic-error provider gate: rates, decision, cooldown, breakdown."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services import provider_failure_gate as gate


@pytest.fixture()
def flows_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "llm_flows.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            model TEXT, status_code INTEGER, finish_reason TEXT)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(gate, "_flows_db_path", lambda: str(db))
    return db


def _flow(db: Path, model: str, status: int, ts: str = "2100-01-01T00:00:00") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO flows (ts, model, status_code, finish_reason) VALUES (?,?,?,?)",
        (ts, model, status, "" if status < 400 else "boom"),
    )
    conn.commit()
    conn.close()


def test_failure_rates_and_gate_decision(flows_db):
    for _ in range(12):
        _flow(flows_db, "llamacpp/qwen3.8-27b", 429)
    _flow(flows_db, "vllm/Qwen3.8-27B-FP8", 200)
    _flow(flows_db, "vllm/Qwen3.8-27B-FP8", 200)

    rates = gate.provider_failure_rates()
    assert rates["llamacpp"]["total"] == 12
    assert rates["llamacpp"]["error_pct"] == 100.0
    assert rates["vllm"]["error_pct"] == 0.0

    should, reason = gate.gate_decision("llamacpp", rates)
    assert should and "100.0% of 12" in reason
    # healthy provider not gated
    assert gate.gate_decision("vllm", rates)[0] is False
    # unknown provider not gated
    assert gate.gate_decision("nope", rates)[0] is False


def test_min_flows_threshold(flows_db):
    for _ in range(9):  # below PROVIDER_GATE_MIN_FLOWS=10
        _flow(flows_db, "llamacpp/x", 429)
    rates = gate.provider_failure_rates()
    assert gate.gate_decision("llamacpp", rates)[0] is False


def test_gate_cooldown_cycle():
    gate._gate_flipped_at.clear()
    assert not gate.in_gate_cooldown("p", now=1000.0)
    gate.mark_gated("p")
    assert gate.in_gate_cooldown("p")  # real clock, fresh flip
    # expired flip gets pruned
    gate._gate_flipped_at["p"] = 1000.0
    assert not gate.in_gate_cooldown("p", now=1000.0 + gate._GATE_COOLDOWN_SECONDS + 1)
    assert "p" not in gate._gate_flipped_at
    gate.mark_gated("p")
    gate.clear_gate("p")
    assert not gate.in_gate_cooldown("p")


def test_error_breakdown(flows_db):
    for _ in range(5):
        _flow(flows_db, "llamacpp/qwen3.8-27b", 429)
    for _ in range(3):
        _flow(flows_db, "vllm/Qwen3.8-27B-FP8", 200)
    b = gate.error_breakdown()
    assert b["total"] == 8
    assert b["errors"] == 5
    assert b["error_pct"] == 62.5
    assert b["by_status"] == {"429": 5}
    assert b["top_errors"][0]["message"].startswith("boom")
    assert b["providers"]["llamacpp"]["error_pct"] == 100.0


def test_breakdown_tolerates_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_flows_db_path", lambda: str(tmp_path / "nope.db"))
    b = gate.error_breakdown()
    assert b["total"] == 0 and b["providers"] == {}
    assert gate.provider_failure_rates() == {}