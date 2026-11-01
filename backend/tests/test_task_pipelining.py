"""Task pipelining plumbing: explicit lane, per-task workspace, attachments."""

from pathlib import Path

import pytest

from app.api.task_attachments import (
    ATTACHMENT_ROOT,
    safe_filename,
    served_media_type,
    task_dir,
)

RUNNER = Path(__file__).resolve().parents[2] / "scripts/mc-claude-runner.sh"
WATCHDOG = Path(__file__).resolve().parents[1] / "app/services/watchdog_loop.py"


# ── attachment storage safety ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.png", "photo.png"),
        ("../../etc/passwd", "passwd"),
        ("/abs/path/report.pdf", "report.pdf"),
        ("weird name (1).JPG", "weird_name_1_.JPG"),
        ("", "file"),
        ("...", "file"),
    ],
)
def test_safe_filename_cannot_escape_the_task_directory(raw, expected):
    name = safe_filename(raw)
    assert name == expected
    assert "/" not in name and ".." not in name


def test_attachment_path_stays_under_the_root():
    from uuid import uuid4

    task = uuid4()
    path = task_dir(task) / safe_filename("../../escape.png")
    assert ATTACHMENT_ROOT.resolve() in path.resolve().parents


# ── attachment download safety (T-036) ──────────────────────────────────────


class _StubSession:
    """Minimal async session that answers one canned row for execute()."""

    def __init__(self, row):
        self._row = row

    async def execute(self, _statement, _params=None):
        stub = self

        class _Result:
            def first(self):
                return stub._row

        return _Result()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("image/png", "image/png"),
        ("application/pdf", "application/pdf"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("TEXT/PLAIN", "text/plain"),
        ("text/html", "application/octet-stream"),
        ("image/svg+xml", "application/octet-stream"),
        ("application/xml", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
        ("nonsense/type", "application/octet-stream"),
    ],
)
def test_served_media_type_never_trusts_stored_types(stored, expected):
    assert served_media_type(stored) == expected


@pytest.mark.asyncio
async def test_download_serves_html_attachment_as_octet_stream(monkeypatch, tmp_path):
    # T-036: content_type was client-supplied at upload; serving it verbatim
    # lets an attacker-chosen Content-Type (text/html) reach the browser.
    # The stored type must not decide the served type unless allowlisted.
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from app.api import task_attachments as ta
    from app.api.deps import ActorContext, require_user_or_agent
    from app.db.session import get_session
    from app.main import app

    root = tmp_path / "attachments"
    (root / str(uuid4())).mkdir(parents=True)
    file = root / str(list(root.iterdir())[0].name) / "x.html"
    file.write_text("<html><script>alert(1)</script></html>")
    monkeypatch.setattr(ta, "ATTACHMENT_ROOT", root)
    board, task, att = (uuid4() for _ in range(3))
    app.dependency_overrides[get_session] = lambda: _StubSession((str(file), "x.html", "text/html"))
    app.dependency_overrides[require_user_or_agent] = lambda: ActorContext(
        actor_type="user", user=None
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(
                f"/api/v1/boards/{board}/tasks/{task}/attachments/{att}/content"
            )
        assert resp.status_code == 200, resp.status_code
        # Untrusted stored types must not be served verbatim.
        assert resp.headers["content-type"].split(";")[0] == "application/octet-stream"
        # Browsers must save, not render, regardless of type.
        assert resp.headers["content-disposition"].startswith("attachment")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_download_of_allowlisted_type_keeps_type_and_attachment(monkeypatch, tmp_path):
    # Safe types keep their Content-Type, but still download (attachment).
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from app.api import task_attachments as ta
    from app.api.deps import ActorContext, require_user_or_agent
    from app.db.session import get_session
    from app.main import app

    root = tmp_path / "attachments"
    task_sub = root / str(uuid4())
    task_sub.mkdir(parents=True)
    file = task_sub / "photo.png"
    file.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ta, "ATTACHMENT_ROOT", root)
    board, task, att = (uuid4() for _ in range(3))
    app.dependency_overrides[get_session] = lambda: _StubSession(
        (str(file), "photo.png", "image/png")
    )
    app.dependency_overrides[require_user_or_agent] = lambda: ActorContext(
        actor_type="user", user=None
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(
                f"/api/v1/boards/{board}/tasks/{task}/attachments/{att}/content"
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"].split(";")[0] == "image/png"
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.content == b"\x89PNG\r\n\x1a\n"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_download_handles_range_requests(monkeypatch, tmp_path):
    # Starlette FileResponse advertises accept-ranges; verify a single byte
    # range is served as 206 with the right slice, and an unsatisfiable range
    # is refused with 416 rather than silently ignored.
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from app.api import task_attachments as ta
    from app.api.deps import ActorContext, require_user_or_agent
    from app.db.session import get_session
    from app.main import app

    root = tmp_path / "attachments"
    task_sub = root / str(uuid4())
    task_sub.mkdir(parents=True)
    file = task_sub / "data.bin"
    file.write_bytes(b"0123456789")
    monkeypatch.setattr(ta, "ATTACHMENT_ROOT", root)
    board, task, att = (uuid4() for _ in range(3))
    app.dependency_overrides[get_session] = lambda: _StubSession(
        (str(file), "data.bin", "application/octet-stream")
    )
    app.dependency_overrides[require_user_or_agent] = lambda: ActorContext(
        actor_type="user", user=None
    )
    url = f"/api/v1/boards/{board}/tasks/{task}/attachments/{att}/content"
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            partial = await client.get(url, headers={"Range": "bytes=2-5"})
            assert partial.status_code == 206
            assert partial.content == b"2345"
            assert partial.headers["content-range"] == "bytes 2-5/10"
            assert partial.headers["content-disposition"].startswith("attachment")

            unsatisfiable = await client.get(url, headers={"Range": "bytes=99-"})
            assert unsatisfiable.status_code == 416
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_download_zero_byte_file(monkeypatch, tmp_path):
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from app.api import task_attachments as ta
    from app.api.deps import ActorContext, require_user_or_agent
    from app.db.session import get_session
    from app.main import app

    root = tmp_path / "attachments"
    task_sub = root / str(uuid4())
    task_sub.mkdir(parents=True)
    file = task_sub / "empty.txt"
    file.write_bytes(b"")
    monkeypatch.setattr(ta, "ATTACHMENT_ROOT", root)
    board, task, att = (uuid4() for _ in range(3))
    app.dependency_overrides[get_session] = lambda: _StubSession(
        (str(file), "empty.txt", "text/plain")
    )
    app.dependency_overrides[require_user_or_agent] = lambda: ActorContext(
        actor_type="user", user=None
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(
                f"/api/v1/boards/{board}/tasks/{task}/attachments/{att}/content"
            )
        assert resp.status_code == 200
        assert resp.content == b""
        assert resp.headers["content-disposition"].startswith("attachment")
    finally:
        app.dependency_overrides.clear()


# ── lane ownership ───────────────────────────────────────────────────────────


def test_runner_claims_by_explicit_lane_then_falls_back_to_prefix():
    body = RUNNER.read_text()
    assert 'lane = (t.get("execution_lane") or "").strip().lower()' in body
    assert 'return lane == "harness"' in body
    # The legacy prefix still works when the lane is unset.
    assert 'return (t.get("title") or "").startswith(markers)' in body


def test_gateway_triage_skips_harness_tasks_by_lane():
    body = WATCHDOG.read_text()
    i = body.index("Coding-harness tasks belong to mc-claude-runner")
    window = body[i : i + 700]
    assert "COALESCE(t.execution_lane, '') <> 'harness'" in window


# ── per-task workspace ───────────────────────────────────────────────────────


def test_runner_honours_a_task_workspace_and_refuses_a_missing_one():
    body = RUNNER.read_text()
    assert 'if [ -n "${TASK_WORKSPACE:-}" ]; then' in body
    assert 'RUN_DIR="$TASK_WORKSPACE"' in body
    # A bad path must not redirect work into the runner's checkout at all.
    assert "workspace does not exist; leaving task unclaimed" in body


def test_runner_passes_attachment_paths_to_the_agent():
    body = RUNNER.read_text()
    assert "/attachments" in body
    assert "ATTACHMENT_BLOCK" in body
    assert "storage_path" in body


def test_task_workspace_runs_skip_the_git_publish_step():
    body = RUNNER.read_text()
    # The owner's own directory is often not a git repo, and the edited files
    # are the deliverable. Without this the publish guard failed the task and
    # requeued it forever, redoing the same edit every tick.
    assert (
        'elif [ "$RC" -eq 0 ] && [ "$APPLY" = "1" ] && [ -n "${TASK_WORKSPACE:-}" ]; then' in body
    )
    assert "no publish step" in body


def test_finished_task_follows_its_own_repeat_flag():
    body = RUNNER.read_text()
    # RUNNER_RECURRING is a global cron default; on its own it sent every
    # finished task back to inbox, so a one-shot task looped forever.
    assert 'if [ "${TASK_REPEATS:-}" = "1" ]; then' in body
    assert 'elif [ "${TASK_REPEATS:-}" = "0" ]; then' in body
    i = body.index('TASK_REPEATS:-}" = "0"')
    assert "NEXT=review" in body[i : i + 120]


def test_runner_will_not_claim_a_task_whose_upstream_is_unfinished():
    body = RUNNER.read_text()
    # The pipeline gate: step 3 waits until a human moves step 2 to done.
    # Without it every step fired at once and step 3 edited the site before
    # anyone had checked step 2 output.
    assert "def ready(t):" in body
    assert 't.get("is_blocked")' in body
    assert "blocked_by_task_ids" in body
    assert "and ready(t)" in body


def test_empty_claim_fields_cannot_shift_the_rest():
    body = RUNNER.read_text()
    # Tab is IFS whitespace, so bash read collapses runs of it: one empty
    # middle field silently shifted every later field left and the runner
    # ran the wrong model with the task title as its thinking level.
    assert "def field(value):" in body
    assert 'or "-"' in body
    assert "TASK_WORKSPACE TASK_PROVIDER TASK_MODEL TASK_THINKING" in body
