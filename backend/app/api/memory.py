"""Memory API — structured daily journal.

Reads `wiki-vault/journal/YYYY-MM-DD.md` and parses sections:
"What we discussed", "Key findings", "Recommendations given", "Decisions",
"Results". Appends a new entry by injecting timestamped sub-sections
under the matching H2 header. Also exposes a flat per-day listing.

T104 (2026-05-24).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user_or_agent

router = APIRouter(prefix="/memory", tags=["memory"])


_VAULT = Path(
    os.environ.get("OBSIDIAN_VAULT_PATH")
    or (Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw")) / "wiki-vault").as_posix()
)
_JOURNAL_DIR = _VAULT / "journal"

# Canonical section headers — keep in sync with frontend Memory tab.
_SECTIONS = (
    "What we discussed",
    "Key findings",
    "Recommendations given",
    "Decisions",
    "Results",
)


def _safe_date(s: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise HTTPException(status_code=400, detail="bad date — expect YYYY-MM-DD")
    return s


def _file_for_date(date: str) -> Path:
    return _JOURNAL_DIR / f"{_safe_date(date)}.md"


def _parse_sections(md: str) -> list[dict[str, Any]]:
    """Split markdown into H2 (## …) sections; preserve order."""
    out: list[dict[str, Any]] = []
    if not md:
        return out
    lines = md.split("\n")
    cur_h: str | None = None
    cur_body: list[str] = []
    h1_seen = False
    for ln in lines:
        if ln.startswith("# ") and not h1_seen:
            h1_seen = True
            continue
        if ln.startswith("## "):
            if cur_h is not None:
                out.append({"heading": cur_h, "body": "\n".join(cur_body).strip()})
            cur_h = ln[3:].strip()
            cur_body = []
        else:
            cur_body.append(ln)
    if cur_h is not None:
        out.append({"heading": cur_h, "body": "\n".join(cur_body).strip()})
    return out


def _classify(heading: str) -> str:
    h = heading.lower().strip()
    for s in _SECTIONS:
        if s.lower() in h:
            return s
    # legacy headings ("Dawn", "Morning", "Noon"…) → "Results" as catch-all
    return "Results"


class EntryAppend(BaseModel):
    date: str  # YYYY-MM-DD
    sections: dict[str, str]  # {"What we discussed": "…", ...} — empty values skipped


class NewJournal(BaseModel):
    date: str


@router.get("/days", summary="List all available journal days")
async def list_days(_: None = Depends(require_user_or_agent)) -> list[dict[str, Any]]:
    if not _JOURNAL_DIR.exists():
        return []
    days: list[dict[str, Any]] = []
    for f in sorted(_JOURNAL_DIR.glob("*.md"), reverse=True):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f.name):
            stat = f.stat()
            days.append({
                "date": f.stem,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
    return days


@router.get("/day/{date}", summary="Get parsed journal for one day")
async def get_day(
    date: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    p = _file_for_date(date)
    if not p.exists():
        return {
            "date": date,
            "exists": False,
            "raw": "",
            "sections": [],
            "grouped": {s: [] for s in _SECTIONS},
        }
    raw = p.read_text()
    sections = _parse_sections(raw)
    grouped: dict[str, list[dict[str, Any]]] = {s: [] for s in _SECTIONS}
    for sec in sections:
        bucket = _classify(sec["heading"])
        grouped.setdefault(bucket, []).append(sec)
    return {
        "date": date,
        "exists": True,
        "raw": raw,
        "sections": sections,
        "grouped": grouped,
        "size_bytes": p.stat().st_size,
        "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
    }


@router.post("/new", summary="Create a journal day if missing (no body yet)")
async def new_day(
    payload: NewJournal,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    p = _file_for_date(payload.date)
    if p.exists():
        return {"created": False, "date": payload.date}
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# Journal — {payload.date}\n\n", encoding="utf-8")
    return {"created": True, "date": payload.date}


@router.post("/append", summary="Append timestamped entry to today's (or other) day")
async def append_entry(
    payload: EntryAppend,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Inject a new H2 block per non-empty section. Each block is timestamped
    so the day-file becomes a chronological log:

    ```
    ## What we discussed — 14:23
    <body>

    ## Key findings — 14:23
    <body>
    ```
    """
    p = _file_for_date(payload.date)
    if not p.exists():
        _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Journal — {payload.date}\n\n", encoding="utf-8")
    body = p.read_text()
    now = datetime.now().strftime("%H:%M")
    blocks: list[str] = []
    written: list[str] = []
    for sec in _SECTIONS:
        content = (payload.sections.get(sec) or "").strip()
        if not content:
            continue
        blocks.append(f"## {sec} — {now}\n{content}\n")
        written.append(sec)
    if not blocks:
        raise HTTPException(status_code=400, detail="all sections empty")
    if not body.endswith("\n\n"):
        body = body.rstrip() + "\n\n"
    body += "\n".join(blocks) + "\n"
    p.write_text(body, encoding="utf-8")
    return {"ok": True, "date": payload.date, "written": written, "at": now}


@router.post(
    "/auto-summary/{date}",
    summary="Force-write the auto-generated daily summary section (T107)",
)
async def force_daily_summary(
    date: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    """Owner-triggered: regenerate Achievements/Open-todos/Issues block."""
    from app.services.journal_writer import write_daily_summary
    from app.db.session import async_session_maker
    _safe_date(date)
    async with async_session_maker() as db:
        return await write_daily_summary(db, date)


@router.delete("/day/{date}", summary="Delete a journal day file")
async def delete_day(
    date: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    p = _file_for_date(date)
    if not p.exists():
        raise HTTPException(status_code=404, detail="day not found")
    p.unlink()
    return {"ok": True, "date": date}
