"""Installed skills on this host (owner 2026-10-28).

Every skill any agent here can load, where it lives on the host, who loads it, and which skills the
fleet's runs actually reported. Skill files open as Markdown and can be edited in place; a save is an
audited admin change guarded by the content hash the editor loaded, so a concurrent change is refused.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelSession

from app.api.deps import get_session as _db_dep
from app.api.deps import require_org_admin
from app.services import mc_sidecar
from app.services.activity_log import AdminChangeFailed, audited_admin_change
from app.services.organizations import OrganizationContext

router = APIRouter(prefix="/installed-skills", tags=["skills"])

# source -> (path in this container, path on the host, who loads skills from there)
ROOTS: dict[str, tuple[str, str, str]] = {
    "claude": ("/home/host_skills/claude", "/root/.claude/skills",
               "Claude Code with user settings: interactive sessions and the watchdog's Opus runs"),
    "claude-plugins": ("/home/host_skills/claude-plugins", "/root/.claude/plugins",
                       "Claude Code plugins, when the plugin is enabled"),
    "pi-packages": ("/home/host_skills/pi-packages", "/root/.pi/agent/git",
                    "Pi sessions with the default agent dir (watchdog gpt-6.1-sol, intake); the fleet "
                    "runner indexes a superpowers subset per role"),
    "pi": ("/home/host_skills/pi", "/root/.pi/agent/skills", "Pi sessions with the default agent dir"),
    "openclaw": ("/home/host_openclaw/skills", "/root/.openclaw/skills", "OpenClaw gateway agents (board allowlists)"),
    "openclaw-workspace": ("/home/host_openclaw/workspace/skills", "/root/.openclaw/workspace/skills",
                           "OpenClaw workspace agents"),
}
SKIP = {".git", "node_modules", ".trash", "cache", "__pycache__", ".venv"}
EDITABLE = {".md", ".markdown", ".txt"}
MAX_FILE = 512 * 1024


def _frontmatter(text: str, key: str) -> str:
    found = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', text.split("\n---", 1)[0], re.M)
    return found[1].strip() if found else ""


def _walk(root: Path):
    """SKILL.md files under root, pruning vendored and cache trees (plugin stores are large)."""
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = sorted(here.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir() and entry.name not in SKIP:
                stack.append(entry)
            elif entry.name == "SKILL.md":
                yield entry


def _resolve(source: str, rel: str) -> Path:
    """A file inside a known root, never outside it (no '..' or symlink escape)."""
    if source not in ROOTS:
        raise HTTPException(status_code=404, detail="Unknown skill source")
    root = Path(ROOTS[source][0]).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Skill file not found")
    return path


def _observed() -> dict[str, list[str]]:
    """Reported skill -> engines whose fleet runs loaded it (runner usage reports)."""
    out: dict[str, list[str]] = {}
    for provider, models in (mc_sidecar.load_sidecar().get("models") or {}).items():
        for model, meta in (models or {}).items():
            for skill in ((meta or {}).get("observed_harness") or {}).get("skills") or [] if isinstance(meta, dict) else []:
                out.setdefault(skill, []).append(f"{provider}/{model}")
    return out


@router.get("", summary="Every installed skill, its location and its observed fleet use")
async def list_installed(_admin: OrganizationContext = Depends(require_org_admin)) -> dict[str, Any]:
    observed = _observed()
    skills = []
    for source, (inside, host, loaded_by) in ROOTS.items():
        root = Path(inside)
        if not root.is_dir():
            continue
        for skill_md in _walk(root):
            folder = skill_md.parent
            text = skill_md.read_text(errors="replace")[:4000]
            name = _frontmatter(text, "name") or folder.name
            rel = folder.relative_to(root).as_posix()
            fleet = sorted({e for key, engines in observed.items() for e in engines
                            if key == f"superpowers:{folder.name}" or (folder.name == "ponytail" and key.startswith("ponytail:"))})
            skills.append({
                "id": f"{source}:{rel}", "source": source, "name": name,
                "description": _frontmatter(text, "description")[:400],
                "package": "/".join(Path(rel).parts[:3]) if source == "pi-packages" else None,
                "host_path": f"{host}/{rel}", "dir": rel,
                "files": sorted(p.relative_to(root).as_posix() for p in folder.rglob("*")
                                if p.is_file() and p.suffix in EDITABLE and not SKIP & set(p.parts))[:40],
                "fleet_engines": fleet,
            })
    return {"roots": [{"source": s, "host_path": h, "loaded_by": who, "present": Path(i).is_dir()}
                      for s, (i, h, who) in ROOTS.items()],
            "skills": skills}


@router.get("/file", summary="One skill file's content")
async def read_file(source: str = Query(...), path: str = Query(...),
                    _admin: OrganizationContext = Depends(require_org_admin)) -> dict[str, Any]:
    target = _resolve(source, path)
    data = target.read_bytes()
    if len(data) > MAX_FILE:
        raise HTTPException(status_code=413, detail="File too large to show")
    return {"source": source, "path": path, "host_path": f"{ROOTS[source][1]}/{path}",
            "content": data.decode("utf-8", errors="replace"), "sha256": hashlib.sha256(data).hexdigest(),
            "editable": target.suffix in EDITABLE}


class SkillFileWrite(BaseModel):
    source: str
    path: str
    content: str = Field(max_length=MAX_FILE)
    expected_sha256: str = Field(min_length=64, max_length=64)


@router.put("/file", summary="Save one skill file (admin, audited, refused if it changed meanwhile)")
async def write_file(payload: SkillFileWrite, admin: OrganizationContext = Depends(require_org_admin),
                     session: SQLModelSession = Depends(_db_dep)) -> dict[str, Any]:
    target = _resolve(payload.source, payload.path)
    if target.suffix not in EDITABLE:
        raise HTTPException(status_code=422, detail="Only Markdown and text skill files are editable")
    if hashlib.sha256(target.read_bytes()).hexdigest() != payload.expected_sha256:
        raise HTTPException(status_code=409, detail="The file changed since it was loaded; reload it first")
    data = payload.content.encode("utf-8")
    try:
        await audited_admin_change(
            session, admin.member, f"Skill file {ROOTS[payload.source][1]}/{payload.path} edited ({len(data)} bytes)",
            apply=lambda: target.write_bytes(data),
            state=lambda: hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    except AdminChangeFailed:
        raise HTTPException(status_code=503, detail="Skill file could not be saved")
    return {"ok": True, "sha256": hashlib.sha256(data).hexdigest()}
