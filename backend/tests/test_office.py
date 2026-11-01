"""Tests for the office tab backend state detection and API endpoints."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette import status as http_status

from app.api.office import (
    _agent_department,
    _DEPARTMENTS,
    _detect_state,
    _last_meaningful_line,
    _latest_memory_file,
    _read_agents,
    _remap_path,
    router as office_router,
)
from app.core import auth as auth_module
from app.core.auth_mode import AuthMode
from app.core.config import settings
from app.db.session import get_session


class TestAgentDepartment:
    """Unit tests for the _agent_department helper.

    T28 (2026-05-14): refactored to test by `agent_name` instead of `agent_id`
    after T13's pattern-based dept mapping (legacy ID-prefix patterns were
    replaced with case-insensitive substring patterns on agent name).
    """

    @pytest.mark.parametrize(
        ("agent_name", "expected_dept"),
        [
            ("Atlas", "management"),
            ("Board Lead 1", "management"),
            ("Reverse Engineer", "research"),
            ("Binary Analyst", "research"),
            ("Security Architect", "security"),
            ("Red Team Operator", "security"),
            ("Software Architect", "development"),
            ("QA Engineer", "development"),
            ("Project Manager", "project-management"),
            ("Gateway Agent", "system"),
            ("Random Unknown", "development"),  # default
        ],
    )
    def test_agent_department_mapping(self, agent_name: str, expected_dept: str) -> None:
        assert _agent_department(agent_id="x", agent_name=agent_name) == expected_dept


class TestDetectState:
    """Unit tests for the _detect_state helper that maps memory contents to statuses."""

    now = time.time()

    @pytest.mark.parametrize(
        ("content", "age_min", "expected_status", "expected_label"),
        [
            # Critical — Token Burn keywords are highest priority
            ("token-burning detected", 0, "problem", "Critical — Token Burn"),
            ("requires immediate human action", 0, "problem", "Critical — Token Burn"),
            # Critical (non-token-burn)
            ("critical: disk full", 5, "problem", "Critical"),
            ("40+ hours idle", 5, "problem", "Critical"),
            # Working
            ("in_progress", 5, "working", "Working"),
            # Stale working (>60m) falls through to age-based
            ("in_progress", 70, "standby", "Standby"),
            # Heartbeat
            ("heartbeat_ok", 5, "heartbeat", "Idle Loop"),
            # Standby keywords
            ("stood down", 5, "standby", "Standby"),
            ("standing down", 5, "standby", "Standby"),
            ("deep standby", 5, "standby", "Standby"),
            ("zero tasks", 5, "standby", "Standby"),
            # Idle
            ("idle", 5, "idle", "Idle"),
            # Age-based fallback
            ("", 3000, "offline", "Offline"),   # >48h
            ("", 800, "standby", "Deep Standby"), # >12h
            ("", 70, "standby", "Standby"),        # >1h
            ("", 5, "active", "Active"),             # <15m
            ("", 20, "idle", "Idle"),                # 15m-1h
        ],
    )
    def test_detect_state(self, content: str, age_min: float, expected_status: str, expected_label: str) -> None:
        mtime = self.now - (age_min * 60)
        status, label = _detect_state(content, mtime)
        assert status == expected_status
        assert label == expected_label


class TestLastMeaningfulLine:
    """Unit tests for the _last_meaningful_line helper."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("# header\nshort\nThis is a meaningful line of text\n", "This is a meaningful line of text"),
            ("# comment\n", ""),
            ("", ""),
            ("A\nB\n", ""),  # lines < 15 chars are skipped
            ("A very long line that exceeds fifteen characters\n", "A very long line that exceeds fifteen characters"),
        ],
    )
    def test_last_meaningful_line(self, content: str, expected: str) -> None:
        assert _last_meaningful_line(content) == expected


class TestLatestMemoryFile:
    """Unit tests for the _latest_memory_file helper."""

    def test_returns_none_when_memory_dir_missing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent"
        workspace.mkdir()
        assert _latest_memory_file(workspace) is None

    def test_returns_latest_md_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent"
        mem_dir = workspace / "memory"
        mem_dir.mkdir(parents=True)
        old = mem_dir / "2026-04-20.md"
        new = mem_dir / "2026-04-23.md"
        old.write_text("old")
        new.write_text("new")
        # Ensure mtime ordering
        time.sleep(0.1)
        new.write_text("newer")
        result = _latest_memory_file(workspace)
        assert result == new

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent"
        mem_dir = workspace / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "notes.txt").write_text("text")
        (mem_dir / "2026-04-23.md").write_text("md")
        assert _latest_memory_file(workspace) == (mem_dir / "2026-04-23.md")


class TestRemapPath:
    """Unit tests for the _remap_path helper."""

    def test_remaps_host_path_to_container(self) -> None:
        with patch("app.api.office._OPENCLAW_HOME", Path("/home/host_openclaw")):
            raw = "/home/user/.openclaw/workspace-mc-xxx/memory"
            expected = Path("/home/host_openclaw/workspace-mc-xxx/memory")
        assert _remap_path(raw) == expected

    def test_returns_none_for_empty_path(self) -> None:
        assert _remap_path("") is None

    def test_returns_none_when_openclaw_not_in_path(self) -> None:
        assert _remap_path("/some/other/path") is None


class TestReadAgents:
    """Integration-style unit tests for _read_agents with mocked filesystem."""

    def _make_openclaw_json(self, agents: list[dict[str, Any]], home: Path) -> Path:
        cfg = {"agents": {"list": agents}}
        cfg_path = home / "openclaw.json"
        cfg_path.write_text(json.dumps(cfg))
        return cfg_path

    @patch.dict("os.environ", {"OPENCLAW_HOME": "/tmp/fake_openclaw"}, clear=True)
    async def test_returns_empty_list_when_config_missing(self) -> None:
        assert await _read_agents() == []

    async def test_reads_agents_and_detects_offline_when_no_workspace(self, tmp_path: Path) -> None:
        home = tmp_path / "openclaw"
        home.mkdir()
        self._make_openclaw_json(
            [{"id": "mc-test-1", "name": "Test Agent", "workspace": "/nowhere"}],
            home,
        )
        with patch("app.api.office._OPENCLAW_HOME", home):
            agents = await _read_agents()
        assert len(agents) == 1
        assert agents[0]["status"] == "offline"
        assert agents[0]["label"] == "Offline"
        # T28 (2026-05-14): default fallback dept is now "development" after T13 refactor.
        assert agents[0]["department"] == "development"

    async def test_reads_agents_with_memory_and_detects_problem(self, tmp_path: Path) -> None:
        home = tmp_path / "openclaw"
        home.mkdir()
        workspace = home / "workspace-test"
        mem_dir = workspace / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "2026-04-23.md").write_text(
            "This session is token-burning. Requires immediate human action."
        )
        self._make_openclaw_json(
            [{"id": "mc-problem-1", "name": "Problem Agent", "workspace": str(workspace)}],
            home,
        )
        with patch("app.api.office._OPENCLAW_HOME", home):
            agents = await _read_agents()
        assert len(agents) == 1
        assert agents[0]["status"] == "problem"
        assert agents[0]["label"] == "Critical — Token Burn"
        assert agents[0]["last_message"] == "This session is token-burning. Requires immediate human action."

    async def test_reads_agents_with_working_memory(self, tmp_path: Path) -> None:
        home = tmp_path / "openclaw"
        home.mkdir()
        workspace = home / "workspace-test"
        mem_dir = workspace / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "2026-04-23.md").write_text("Current task is in_progress.")
        self._make_openclaw_json(
            [{"id": "mc-working-1", "name": "Working Agent", "workspace": str(workspace)}],
            home,
        )
        with patch("app.api.office._OPENCLAW_HOME", home):
            agents = await _read_agents()
        assert len(agents) == 1
        assert agents[0]["status"] == "working"
        assert agents[0]["label"] == "Working"

    async def test_skips_main_agent_id(self, tmp_path: Path) -> None:
        home = tmp_path / "openclaw"
        home.mkdir()
        self._make_openclaw_json(
            [{"id": "main", "name": "Main Agent"}, {"id": "mc-real", "name": "Real Agent"}],
            home,
        )
        with patch("app.api.office._OPENCLAW_HOME", home):
            agents = await _read_agents()
        ids = [a["id"] for a in agents]
        assert "main" not in ids
        assert "mc-real" in ids

    @pytest.mark.asyncio
    async def test_reads_model_as_string_or_dict(self, tmp_path: Path) -> None:
        home = tmp_path / "openclaw"
        home.mkdir()
        self._make_openclaw_json(
            [
                {"id": "mc-model-str", "name": "A", "model": "gpt-4"},
                {"id": "mc-model-dict", "name": "B", "model": {"primary": "gpt-4"}},
            ],
            home,
        )
        with patch("app.api.office._OPENCLAW_HOME", home):
            agents = await _read_agents()
        models = {a["id"]: a["model"] for a in agents}
        assert models["mc-model-str"] == "gpt-4"
        assert models["mc-model-dict"] == "gpt-4"


class TestOfficeApiEndpoint:
    """Integration tests for the /api/v1/office/agents endpoint."""

    @pytest.fixture
    async def client(self, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
        monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
        monkeypatch.setattr(settings, "local_auth_token", "test-office-token")
        monkeypatch.setattr(auth_module, "LOCAL_AUTH_USER_ID", "office-test-user")
        monkeypatch.setattr(auth_module, "LOCAL_AUTH_EMAIL", "office@localhost")
        monkeypatch.setattr(auth_module, "LOCAL_AUTH_NAME", "Office Test")

        from app.api.users import router as users_router

        app = FastAPI()
        api_v1 = APIRouter(prefix="/api/v1")
        api_v1.include_router(office_router)
        api_v1.include_router(users_router)
        app.include_router(api_v1)

        # T28 (2026-05-14): provide a real in-memory SQLite session because
        # `require_user_or_agent` now does a user lookup via crud. Yielding
        # None previously worked when auth was token-only.
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlmodel import SQLModel
        from sqlmodel.ext.asyncio.session import AsyncSession
        _engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with _engine.begin() as _conn:
            await _conn.run_sync(SQLModel.metadata.create_all)

        async def _override_get_session():
            async with AsyncSession(_engine) as _s:
                yield _s

        app.dependency_overrides[get_session] = _override_get_session
        app.dependency_overrides[auth_module.get_session] = _override_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

    @pytest.mark.asyncio
    async def test_office_agents_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/office/agents")
        assert resp.status_code == http_status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_office_agents_returns_structure_with_valid_auth(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_agents = [
            {
                "id": "mc-office-1",
                "name": "Office Bot",
                "department": "development",
                "status": "working",
                "label": "Working",
                "last_active": time.time(),
                "last_message": "All good",
                "heartbeat_interval": "10m",
                "model": "gpt-4",
            }
        ]
        async def _mock_read_agents():
            return fake_agents
        monkeypatch.setattr("app.api.office._read_agents", _mock_read_agents)

        resp = await client.get(
            "/api/v1/office/agents",
            headers={"Authorization": "Bearer test-office-token"},
        )
        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()
        assert "departments" in data
        assert "agents" in data
        assert data["total"] == 1
        assert data["problem_count"] == 0
        assert data["active_count"] == 1
        assert "development" in data["departments"]
        assert data["departments"]["development"]["agents"][0]["id"] == "mc-office-1"

    @pytest.mark.asyncio
    async def test_office_agents_counts_problems_and_active_correctly(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_agents = [
            {"id": "a1", "name": "A1", "department": "re-lab", "status": "problem", "label": "Critical", "last_active": None, "last_message": "", "heartbeat_interval": "", "model": ""},
            {"id": "a2", "name": "A2", "department": "re-lab", "status": "working", "label": "Working", "last_active": None, "last_message": "", "heartbeat_interval": "", "model": ""},
            {"id": "a3", "name": "A3", "department": "re-lab", "status": "active", "label": "Active", "last_active": None, "last_message": "", "heartbeat_interval": "", "model": ""},
            {"id": "a4", "name": "A4", "department": "re-lab", "status": "offline", "label": "Offline", "last_active": None, "last_message": "", "heartbeat_interval": "", "model": ""},
        ]
        async def _mock_read_agents():
            return fake_agents
        monkeypatch.setattr("app.api.office._read_agents", _mock_read_agents)

        resp = await client.get(
            "/api/v1/office/agents",
            headers={"Authorization": "Bearer test-office-token"},
        )
        data = resp.json()
        assert data["total"] == 4
        assert data["problem_count"] == 1
        assert data["active_count"] == 2  # working + active
