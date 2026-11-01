"""Task attachments: upload, list, download.

Files land on the shared volume under a task-scoped directory so the
coding-harness runner reads the real file from disk. Nothing streams bytes
through a board comment.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_user_or_agent
from app.db.session import get_session
from app.core.logging import get_logger
from app.models.task_attachments import TaskAttachment

logger = get_logger(__name__)
router = APIRouter(prefix="/boards/{board_id}/tasks", tags=["tasks"])

SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)

# Same volume the harness runner can read.
ATTACHMENT_ROOT = Path(os.environ.get("TASK_ATTACHMENT_ROOT", "/traffic-data/attachments"))
MAX_ATTACHMENT_BYTES = int(os.environ.get("TASK_ATTACHMENT_MAX_BYTES", str(64 * 1024 * 1024)))
_CHUNK = 1024 * 1024

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Media types we are willing to serve verbatim on download. The stored
# content_type came from the client at upload, so anything not on this list
# (html, svg, xml, unknown, empty...) is served as a binary blob: the browser
# must never render a client-chosen type from this origin.
_DOWNLOAD_SAFE_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/zip",
        "image/apng",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)


def served_media_type(stored: str | None) -> str:
    """Map a stored (client-supplied) type to the type we actually serve."""
    candidate = (stored or "").strip().split(";")[0].strip().lower()
    return candidate if candidate in _DOWNLOAD_SAFE_TYPES else "application/octet-stream"


def safe_filename(raw: str) -> str:
    """Reduce an uploaded name to something safe to place on disk.

    Never trust the client: strip directories, collapse anything exotic, and
    refuse names that would escape the task directory.
    """
    name = Path(raw or "").name
    name = _SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return name[:120]


def task_dir(task_id: UUID) -> Path:
    return ATTACHMENT_ROOT / str(task_id)


@router.post("/{task_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    board_id: UUID,
    task_id: UUID,
    file: UploadFile = File(...),
    session: AsyncSession = SESSION_DEP,
    actor=ACTOR_DEP,
) -> dict:
    """Attach one file to a task."""
    row = (
        await session.execute(
            text("SELECT board_id FROM tasks WHERE id = :t"), {"t": str(task_id)}
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    directory = task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = safe_filename(file.filename or "file")
    target = directory / f"{uuid4().hex[:8]}-{name}"

    digest = hashlib.sha256()
    written = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if written > MAX_ATTACHMENT_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"Attachment exceeds {MAX_ATTACHMENT_BYTES} bytes",
                    )
                digest.update(chunk)
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not store attachment") from exc

    attachment = TaskAttachment(
        task_id=task_id,
        board_id=board_id,
        filename=name,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=written,
        storage_path=str(target),
        sha256=digest.hexdigest(),
        created_by_type=getattr(actor, "actor_type", None),
        created_by_name=getattr(actor, "actor_name", None),
    )
    session.add(attachment)
    await session.commit()
    await session.refresh(attachment)
    logger.info(
        "task_attachment.stored task=%s name=%s bytes=%d", str(task_id)[:8], name, written
    )
    return _serialize(attachment)


@router.get("/{task_id}/attachments")
async def list_attachments(
    board_id: UUID,
    task_id: UUID,
    session: AsyncSession = SESSION_DEP,
    actor=ACTOR_DEP,
) -> dict:
    rows = (
        await session.execute(
            text(
                "SELECT id, filename, content_type, size_bytes, storage_path, sha256,"
                " created_by_name, created_at FROM task_attachments"
                " WHERE task_id = :t ORDER BY created_at"
            ),
            {"t": str(task_id)},
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r[0]),
                "filename": r[1],
                "content_type": r[2],
                "size_bytes": r[3],
                # The absolute path is the point: an agent opens the real file.
                "storage_path": r[4],
                "sha256": r[5],
                "created_by_name": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]
    }


@router.get("/{task_id}/attachments/{attachment_id}/content")
async def download_attachment(
    board_id: UUID,
    task_id: UUID,
    attachment_id: UUID,
    session: AsyncSession = SESSION_DEP,
    actor=ACTOR_DEP,
) -> FileResponse:
    row = (
        await session.execute(
            text(
                "SELECT storage_path, filename, content_type FROM task_attachments"
                " WHERE id = :a AND task_id = :t"
            ),
            {"a": str(attachment_id), "t": str(task_id)},
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    path = Path(row[0])
    # The stored path is ours, but re-check containment before serving it.
    if not path.is_file() or ATTACHMENT_ROOT.resolve() not in path.resolve().parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment file is missing")
    # media_type is never taken from untrusted input alone: only allowlisted
    # types survive; everything else downloads as application/octet-stream.
    # FileResponse sets Content-Disposition: attachment (filename is always
    # present) and handles Range requests itself (206/416).
    return FileResponse(path, media_type=served_media_type(row[2]), filename=row[1])


@router.delete("/{task_id}/attachments/{attachment_id}")
async def delete_attachment(
    board_id: UUID,
    task_id: UUID,
    attachment_id: UUID,
    session: AsyncSession = SESSION_DEP,
    actor=ACTOR_DEP,
) -> dict:
    row = (
        await session.execute(
            text("SELECT storage_path FROM task_attachments WHERE id = :a AND task_id = :t"),
            {"a": str(attachment_id), "t": str(task_id)},
        )
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    Path(row[0]).unlink(missing_ok=True)
    await session.execute(
        text("DELETE FROM task_attachments WHERE id = :a"), {"a": str(attachment_id)}
    )
    await session.commit()
    return {"ok": True}


def _serialize(a: TaskAttachment) -> dict:
    return {
        "id": str(a.id),
        "task_id": str(a.task_id),
        "filename": a.filename,
        "content_type": a.content_type,
        "size_bytes": a.size_bytes,
        "storage_path": a.storage_path,
        "sha256": a.sha256,
        "created_at": a.created_at.isoformat(),
    }
