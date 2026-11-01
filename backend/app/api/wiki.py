"""Wiki served from the obsidian-wiki vault.

The vault directory holds markdown pages maintained by agents via the
`wiki-update` / `wiki-query` skills, plus owner edits. This router exposes:

  GET  /wiki/tree                    — directory tree
  GET  /wiki/page/{path}             — raw markdown content
  PUT  /wiki/page/{path}             — create/overwrite page (owner edit)
  DELETE /wiki/page/{path}           — remove page
  GET  /wiki/search?q=…              — substring search
  GET  /wiki/backlinks/{path}        — pages that wikilink here

Path rules: relative to vault root; `.md` optional; traversal blocked.
Writes are guarded by `require_user_or_agent`; the FS owner mount-write bit
is the ultimate authority.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user_or_agent

router = APIRouter(prefix="/wiki", tags=["wiki"])

_VAULT = Path(os.environ.get(
    "OBSIDIAN_VAULT_PATH", "/home/host_openclaw/wiki-vault"
)).resolve()

_WIKILINK_RE = re.compile(r"\[\[([^\]\|]+)(?:\|([^\]]+))?\]\]")
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB hard cap per page

# T19 (2026-05-14): atomic versioning + soft delete.
# Every PUT overwriting an existing page copies the prior content to
# `_VAULT/.history/<path>.<ts>.md`. Every DELETE moves the page to
# `_VAULT/_trash/<ts>/<path>` instead of unlinking. Tunable via env.
_HISTORY_DIR_NAME = os.environ.get("WIKI_HISTORY_DIR", ".history")
_TRASH_DIR_NAME = os.environ.get("WIKI_TRASH_DIR", "_trash")
_HISTORY_KEEP = int(os.environ.get("WIKI_HISTORY_KEEP", "10"))  # last N versions per page


def _history_dir() -> Path:
    p = _VAULT / _HISTORY_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _trash_root() -> Path:
    p = _VAULT / _TRASH_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshot_history(target: Path) -> Path | None:
    """Copy `target` to `_VAULT/.history/<rel>/<basename>.<ts>.md` before overwrite.

    Returns the snapshot path on success, None if target doesn't exist or copy fails.
    Prunes oldest snapshots beyond `_HISTORY_KEEP` for this page.
    """
    if not target.exists():
        return None
    try:
        rel = target.relative_to(_VAULT)
        # Mirror the directory layout so different files don't collide.
        dest_dir = _history_dir() / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        snap = dest_dir / f"{target.stem}.{ts}.md"
        snap.write_bytes(target.read_bytes())
        # Prune older snapshots beyond _HISTORY_KEEP for this page.
        siblings = sorted(
            (p for p in dest_dir.iterdir()
             if p.is_file() and p.name.startswith(target.stem + ".")
             and p.name.endswith(".md")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in siblings[_HISTORY_KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
        return snap
    except OSError:
        return None


def _move_to_trash(target: Path) -> Path | None:
    """Move `target` to `_VAULT/_trash/<ts>/<rel>` instead of unlinking. Returns
    the new path on success, None on failure.
    """
    if not target.exists():
        return None
    try:
        rel = target.relative_to(_VAULT)
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        dest_dir = _trash_root() / ts / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / target.name
        target.rename(dest)
        return dest
    except OSError:
        return None


def _safe_resolve(rel: str) -> Path:
    """Resolve a vault-relative path, blocking traversal and symlinks."""
    rel = rel.strip().lstrip("/")
    if not rel.endswith(".md"):
        rel = rel + ".md"
    target = (_VAULT / rel).resolve()
    # Use realpath to catch symlinks pointing outside the vault
    real = target.resolve()
    if str(_VAULT) not in str(real):
        raise HTTPException(status_code=400, detail="path escapes vault")
    if _VAULT not in real.parents and real != _VAULT:
        raise HTTPException(status_code=400, detail="path escapes vault")
    return real


# ── tree ───────────────────────────────────────────────────────────────────


class TreeNode(BaseModel):
    name: str
    path: str
    is_dir: bool
    children: list["TreeNode"] | None = None


def _build_tree(root: Path, base: Path) -> list[TreeNode]:
    nodes: list[TreeNode] = []
    if not root.exists():
        return nodes
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return nodes
    for entry in entries:
        if entry.name.startswith(".") and entry.name not in ("_raw",):
            continue
        # T19: hide trash from the wiki tree; access via direct path if needed.
        if entry.name == _TRASH_DIR_NAME:
            continue
        rel = str(entry.relative_to(base))
        if entry.is_dir():
            nodes.append(
                TreeNode(
                    name=entry.name, path=rel, is_dir=True,
                    children=_build_tree(entry, base),
                )
            )
        elif entry.suffix.lower() == ".md":
            # T40c (2026-05-14): filter `.tmp` and `.tmp.*` sidecars left
            # behind by interrupted atomic writes.
            if entry.name.endswith(".tmp") or ".tmp." in entry.name:
                continue
            nodes.append(
                TreeNode(name=entry.name, path=rel, is_dir=False, children=None)
            )
    return nodes


@router.get("/tree", dependencies=[Depends(require_user_or_agent)])
def get_tree() -> list[TreeNode]:
    if not _VAULT.exists():
        raise HTTPException(status_code=503, detail=f"vault not found at {_VAULT}")
    return _build_tree(_VAULT, _VAULT)


# ── single page ────────────────────────────────────────────────────────────


class PageRead(BaseModel):
    path: str
    title: str
    content: str
    size: int
    mtime: float
    links: list[str]
    # T40c (2026-05-14): convenience metadata derived from content. Avoids
    # every consumer reimplementing the same regexes.
    word_count: int = 0
    headings: list[dict[str, object]] = []


def _extract_links(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        if target not in seen:
            seen.add(target)
            out.append(target)
    return out


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _extract_headings(text: str) -> list[dict[str, object]]:
    """T40c: extract markdown headings as `{level, text}` records. Skips
    headings inside fenced code blocks (best-effort via fence toggle).
    """
    in_fence = False
    out: list[dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        out.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return out


def _word_count(text: str) -> int:
    """Cheap whitespace-split word count, fenced code blocks skipped."""
    in_fence = False
    n = 0
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        n += len(line.split())
    return n


@router.get("/page/{path:path}", dependencies=[Depends(require_user_or_agent)])
def get_page(path: str) -> PageRead:
    target = _safe_resolve(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="page not found")
    if target.stat().st_size > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="page too large")
    text = target.read_text(encoding="utf-8", errors="replace")
    title = target.stem
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            break
    rel = str(target.relative_to(_VAULT))
    return PageRead(
        path=rel, title=title, content=text, size=target.stat().st_size,
        mtime=target.stat().st_mtime, links=_extract_links(text),
        word_count=_word_count(text),
        headings=_extract_headings(text),
    )


# ── write / delete ─────────────────────────────────────────────────────────


class PageWrite(BaseModel):
    content: str


@router.put("/page/{path:path}", dependencies=[Depends(require_user_or_agent)])
def put_page(
    path: str,
    payload: PageWrite,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> PageRead:
    target = _safe_resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if len(payload.content.encode("utf-8")) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="content too large")
    # T27b (2026-05-14): If-Match optimistic concurrency. Client supplies the
    # mtime they last saw; we reject the write if the file has been modified
    # since. Compare to whole-second resolution to dodge sub-second drift.
    if if_match and target.exists():
        try:
            client_mtime = float(if_match.strip('"').strip())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="If-Match must be a numeric mtime (seconds since epoch)",
            )
        server_mtime = target.stat().st_mtime
        if abs(server_mtime - client_mtime) > 1.0:
            raise HTTPException(
                status_code=412,
                detail={
                    "error": "precondition_failed",
                    "message": "page modified since last read; reload and retry",
                    "client_mtime": client_mtime,
                    "server_mtime": server_mtime,
                },
            )
    # T19: snapshot current content before overwrite.
    if target.exists():
        _snapshot_history(target)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload.content, encoding="utf-8")
    tmp.replace(target)
    return get_page(path)


@router.delete("/page/{path:path}", dependencies=[Depends(require_user_or_agent)])
def delete_page(path: str) -> dict[str, Any]:
    target = _safe_resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="page not found")
    # T19: soft-delete via move to `_trash/<ts>/<rel>`. Recovery: copy file
    # back from the trash dir.
    rel = str(target.relative_to(_VAULT))
    moved = _move_to_trash(target)
    if moved is None:
        # FS rename failed → fall back to hard delete so caller still sees
        # the page gone (preserves prior contract).
        target.unlink()
        return {"deleted": rel, "trashed_to": None}
    return {
        "deleted": rel,
        "trashed_to": str(moved.relative_to(_VAULT)),
    }


# ── search ─────────────────────────────────────────────────────────────────


class SearchHit(BaseModel):
    path: str
    title: str
    snippet: str
    score: int


# T27c (2026-05-14): sqlite FTS5 search index over the vault.
# Persistent DB so a backend restart doesn't reindex. Each row tracks the
# file's mtime; before every search we refresh stale rows. Falls back to the
# previous linear-scan implementation if FTS5 is unavailable on this sqlite
# build.
_FTS_DB = Path(os.environ.get(
    "WIKI_FTS_DB", "/traffic-data/wiki_fts.db",
))
_fts_lock = threading.Lock()
_fts_supported: bool | None = None


def _open_fts() -> sqlite3.Connection | None:
    global _fts_supported
    if _fts_supported is False:
        return None
    try:
        _FTS_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_FTS_DB), check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS file_meta ("
                     "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, title TEXT)")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "path UNINDEXED, title, body, tokenize='unicode61')"
        )
        _fts_supported = True
        return conn
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            _fts_supported = False
        return None


def _index_path_into_fts(conn: sqlite3.Connection, md: Path) -> None:
    try:
        st = md.stat()
        if st.st_size > _MAX_FILE_BYTES:
            return
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    rel = str(md.relative_to(_VAULT))
    title = md.stem
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            break
    # Replace any prior row for this path.
    conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
    conn.execute(
        "INSERT INTO docs (path, title, body) VALUES (?, ?, ?)",
        (rel, title, text),
    )
    conn.execute(
        "INSERT INTO file_meta (path, mtime, size, title) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
        "size=excluded.size, title=excluded.title",
        (rel, st.st_mtime, st.st_size, title),
    )


def _refresh_fts_index(conn: sqlite3.Connection) -> None:
    """Bring the FTS index up to date with the vault. Cheap because we skip
    files whose mtime + size match what's in `file_meta`.
    """
    known: dict[str, tuple[float, int]] = {}
    for row in conn.execute("SELECT path, mtime, size FROM file_meta"):
        known[row[0]] = (row[1] or 0.0, row[2] or 0)
    seen: set[str] = set()
    for md in _VAULT.rglob("*.md"):
        rel_parts = md.relative_to(_VAULT).parts
        if any(p.startswith(".") for p in rel_parts):
            continue
        if rel_parts and rel_parts[0] == "_trash":
            continue
        rel = str(md.relative_to(_VAULT))
        seen.add(rel)
        try:
            st = md.stat()
        except OSError:
            continue
        prior = known.get(rel)
        if prior and abs(prior[0] - st.st_mtime) < 1.0 and prior[1] == st.st_size:
            continue
        _index_path_into_fts(conn, md)
    # Drop rows whose source file vanished.
    if known:
        stale = [p for p in known if p not in seen]
        for p in stale:
            conn.execute("DELETE FROM docs WHERE path = ?", (p,))
            conn.execute("DELETE FROM file_meta WHERE path = ?", (p,))
    conn.commit()


@router.get("/search", dependencies=[Depends(require_user_or_agent)])
def search(q: str, limit: int = 20) -> list[SearchHit]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    if not _VAULT.exists():
        return []
    with _fts_lock:
        conn = _open_fts()
    if conn is not None:
        try:
            with _fts_lock:
                _refresh_fts_index(conn)
            # FTS5 match expression: AND-combine words.
            tokens = [t for t in re.split(r"\s+", q) if t]
            if not tokens:
                return []
            match_expr = " ".join(f"\"{t.replace(chr(34), '')}\"" for t in tokens)
            rows = conn.execute(
                "SELECT path, title, snippet(docs, 2, '', '', '...', 12) AS snip, "
                "rank FROM docs WHERE docs MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match_expr, limit),
            ).fetchall()
            hits: list[SearchHit] = []
            for path, title, snip, rank in rows:
                # FTS5 rank is negative; convert to positive int for the UI.
                score = max(1, int(round(-(rank or 0) * 10)))
                hits.append(SearchHit(
                    path=path, title=title or path, snippet=snip or "", score=score,
                ))
            return hits
        except sqlite3.OperationalError:
            # Index temporarily unavailable — fall through to linear scan.
            pass

    # Linear-scan fallback (FTS5 unavailable or temp failure).
    needle = q.lower()
    hits: list[SearchHit] = []
    for md in _VAULT.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(_VAULT).parts):
            continue
        try:
            if md.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text_lower = text.lower()
        score = text_lower.count(needle)
        if score == 0:
            continue
        idx = text_lower.find(needle)
        start = max(0, idx - 40)
        end = min(len(text), idx + len(needle) + 80)
        snippet = text[start:end].replace("\n", " ")
        title = md.stem
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
        hits.append(
            SearchHit(
                path=str(md.relative_to(_VAULT)),
                title=title, snippet=snippet, score=score,
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


# ── backlinks ─────────────────────────────────────────────────────────────

# T40d (2026-05-14): precomputed backlinks index. Cached in-memory keyed by
# vault mtime so PUT/DELETE invalidate naturally. Falls back to linear scan
# if mtime can't be read.
_backlinks_index: dict[str, list[dict[str, str]]] = {}
_backlinks_built_at: float = 0.0
_backlinks_lock = threading.Lock()


def _vault_max_mtime() -> float:
    """Cheap proxy for vault freshness: max mtime of any .md outside hidden dirs."""
    if not _VAULT.exists():
        return 0.0
    best = 0.0
    try:
        for md in _VAULT.rglob("*.md"):
            parts = md.relative_to(_VAULT).parts
            if any(p.startswith(".") for p in parts):
                continue
            if parts and parts[0] == _TRASH_DIR_NAME:
                continue
            try:
                m = md.stat().st_mtime
            except OSError:
                continue
            if m > best:
                best = m
    except OSError:
        pass
    return best


def _build_backlinks_index() -> None:
    """Walk the vault once and fill `_backlinks_index[target] = [{path, title}]`.
    Keyed by both the page stem and the full relative path. Invalidated by
    mtime check at the top of `get_backlinks`.
    """
    out: dict[str, list[dict[str, str]]] = {}
    if not _VAULT.exists():
        _backlinks_index.clear()
        return
    for md in _VAULT.rglob("*.md"):
        parts = md.relative_to(_VAULT).parts
        if any(p.startswith(".") for p in parts):
            continue
        if parts and parts[0] == _TRASH_DIR_NAME:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(md.relative_to(_VAULT))
        title = md.stem
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
        seen_local: set[str] = set()
        for m in _WIKILINK_RE.finditer(text):
            link = m.group(1).strip()
            if not link or link in seen_local:
                continue
            seen_local.add(link)
            # Index under the link target both as-is and as basename — caller
            # resolves via either.
            for key in (link, link.removesuffix(".md")):
                out.setdefault(key, []).append({"path": rel, "title": title})
    _backlinks_index.clear()
    _backlinks_index.update(out)


@router.get("/backlinks/{path:path}", dependencies=[Depends(require_user_or_agent)])
def get_backlinks(path: str) -> list[dict[str, str]]:
    """Return pages whose `[[wikilinks]]` resolve to `path`.

    T40d (2026-05-14): served from precomputed index; rebuilt on mtime change.
    Match is loose: target stem (basename without `.md`) OR full relative path.
    """
    global _backlinks_built_at
    target = _safe_resolve(path)
    target_rel = str(target.relative_to(_VAULT))
    target_stem = target.stem
    if not _VAULT.exists():
        return []
    # Refresh index if vault has changed.
    current_mtime = _vault_max_mtime()
    with _backlinks_lock:
        if current_mtime > _backlinks_built_at or not _backlinks_index:
            _build_backlinks_index()
            _backlinks_built_at = current_mtime
    # Probe all reasonable aliases.
    target_rel_no_ext = target_rel.removesuffix(".md")
    seen_paths: set[str] = set()
    out: list[dict[str, str]] = []
    for key in (target_stem, target_rel, target_rel_no_ext):
        for entry in _backlinks_index.get(key, []):
            if entry["path"] == target_rel:
                continue  # self
            if entry["path"] in seen_paths:
                continue
            seen_paths.add(entry["path"])
            out.append(entry)
    return out


class WikilinkResolveResult(BaseModel):
    """T40c (2026-05-14): server-side wikilink resolver result."""

    input: str
    matched: bool
    path: str | None = None
    title: str | None = None
    candidates: list[dict[str, str]] = []


@router.get("/resolve", dependencies=[Depends(require_user_or_agent)])
def resolve_wikilink(q: str) -> WikilinkResolveResult:
    """Resolve a wikilink target → page path. Tries exact relative-path match,
    then unique basename match. Returns `candidates` when multiple files share
    the basename so callers can disambiguate.
    """
    q = (q or "").strip().lstrip("/")
    if not q:
        return WikilinkResolveResult(input=q, matched=False)
    norm = q if q.endswith(".md") else f"{q}.md"
    candidate_path = _VAULT / norm
    if candidate_path.exists() and candidate_path.is_file():
        return WikilinkResolveResult(
            input=q, matched=True,
            path=str(candidate_path.relative_to(_VAULT)),
            title=candidate_path.stem,
        )
    # Basename fallback.
    matches: list[dict[str, str]] = []
    for md in _VAULT.rglob("*.md"):
        parts = md.relative_to(_VAULT).parts
        if any(p.startswith(".") for p in parts):
            continue
        if parts and parts[0] == _TRASH_DIR_NAME:
            continue
        if md.stem == q or md.stem == q.removesuffix(".md"):
            matches.append({"path": str(md.relative_to(_VAULT)), "title": md.stem})
    if len(matches) == 1:
        return WikilinkResolveResult(
            input=q, matched=True,
            path=matches[0]["path"], title=matches[0]["title"],
        )
    return WikilinkResolveResult(input=q, matched=False, candidates=matches)
