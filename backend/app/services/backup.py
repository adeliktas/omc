"""T105 (2026-05-25) — Full-system snapshot + restore service.

Snapshots are point-in-time captures of every component the autonomous
loop could break:

  - Postgres `mission_control` (pg_dump custom format)
  - sqlite `llm_flows.db` (online .backup)
  - `~/.openclaw/openclaw.json` + `runtime_settings.json` + other sidecars
  - All `~/.openclaw/agents/*/sessions/` state
  - All `~/.openclaw/workspace-*/` agent workspaces
  - `~/.openclaw/wiki-vault/` (markdown vault)
  - OmniRoute Docker volume `omniroute_data` (providers/connections/api-keys)
  - Compose files + `.env` (so a `docker compose up` after restore lands
    the same stack)

Layout per snapshot dir under `/snapshots/snaps/<ts>_<label>/`:

  manifest.json          — committed to git; component list + sha256 + sizes
  postgres.dump          — pg_dump --format=custom
  traffic.sqlite         — sqlite online-backup file
  openclaw.tar.zst       — tar of ~/.openclaw (excludes the snapshots
                           dir itself + workspace-*; those are separate
                           archives to keep openclaw.tar.zst small)
  workspaces.tar.zst     — tar of all workspace-*/ + agents/*/sessions
  wiki-vault.tar.zst     — tar of wiki-vault/
  or-volume.tar.zst      — OmniRoute /app/data volume content
  compose.tar.zst        — compose.yml + .env + Dockerfiles

Git tracks the manifests so `git log` shows what existed when.
Large blobs are excluded by .gitignore — restore reads the dir directly.

The autonomous loop calls `take_snapshot(scope='pre-step', label=...)`
before any risky write; `restore_snapshot(snapshot_id)` reverts.

CRITICAL: restore stops affected services first, restores files, restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


_SNAPSHOT_ROOT = Path(os.environ.get("SNAPSHOT_ROOT", "/snapshots"))
_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "/home/host_openclaw"))
_TRAFFIC_DB = Path(os.environ.get("TRAFFIC_DB_FILE", "/traffic-data/llm_flows.db"))
_COMPOSE_PROJECT_DIR = Path(
    os.environ.get("COMPOSE_PROJECT_DIR", "/snap-sources/compose-project"),
)
# T105: OmniRoute volume read-only mount path. Backend can't reach
# /var/lib/docker/volumes/* directly; compose.yml mounts the named
# volume at /snap-sources/omniroute_data:ro for snapshot access.
_OR_VOLUME_RO = Path(os.environ.get("OR_VOLUME_RO", "/snap-sources/omniroute_data"))

# Components — each is a (kind, name, generator, restorer) tuple.
# `generator` writes its artifact into the snapshot dir + returns relative path.
# `restorer` reads the artifact + restores live state.

# Public component IDs
COMPONENT_POSTGRES = "postgres"
COMPONENT_TRAFFIC = "traffic_sqlite"
COMPONENT_OPENCLAW_CFG = "openclaw_config"
COMPONENT_WORKSPACES = "workspaces"
COMPONENT_WIKI = "wiki_vault"
COMPONENT_OR_VOLUME = "omniroute_volume"
COMPONENT_COMPOSE = "compose"

ALL_COMPONENTS = [
    COMPONENT_POSTGRES,
    COMPONENT_TRAFFIC,
    COMPONENT_OPENCLAW_CFG,
    COMPONENT_WORKSPACES,
    COMPONENT_WIKI,
    COMPONENT_OR_VOLUME,
    COMPONENT_COMPOSE,
]

# T105 Phase 4 (2026-05-25): lightweight snapshot for PEV pre-step.
# Skips the multi-GB traffic.sqlite + OR volume — those are append-only
# / not modified by an autonomous step. Workspaces + openclaw config +
# postgres are the components a runaway step could damage.
PEV_PRESTEP_COMPONENTS = [
    COMPONENT_POSTGRES,
    COMPONENT_OPENCLAW_CFG,
    COMPONENT_WORKSPACES,
]


# ── helpers ────────────────────────────────────────────────────────────────


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_label(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:64]


def _tar_zst(source: Path, archive: Path, excludes: list[str] | None = None) -> bool:
    """Tar+zstd a directory. zstd preferred (fast + good ratio); falls back to gzip."""
    if not source.exists():
        return False
    excludes = excludes or []

    # Try zstd first
    use_zstd = shutil.which("zstd") is not None
    suffix = ".tar.zst" if use_zstd else ".tar.gz"
    final_archive = archive.with_suffix("").with_suffix(suffix)

    # Build excludes for tar
    tar_excludes: list[str] = []
    for e in excludes:
        tar_excludes.extend(["--exclude", e])

    cmd_tar = [
        "tar",
        "-cf", "-",
        *tar_excludes,
        "-C", str(source.parent),
        source.name,
    ]
    try:
        with final_archive.open("wb") as out:
            tar = subprocess.Popen(
                cmd_tar,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if use_zstd:
                comp = subprocess.Popen(
                    ["zstd", "-T0", "-3", "-q"],
                    stdin=tar.stdout,
                    stdout=out,
                    stderr=subprocess.PIPE,
                )
            else:
                comp = subprocess.Popen(
                    ["gzip", "-3"],
                    stdin=tar.stdout,
                    stdout=out,
                    stderr=subprocess.PIPE,
                )
            tar.stdout.close()  # type: ignore[union-attr]
            comp.wait()
            tar.wait()
        if final_archive.stat().st_size == 0:
            final_archive.unlink(missing_ok=True)
            return False
        # Rename to expected name (caller passed archive without compress suffix)
        if final_archive != archive:
            final_archive.rename(archive)
        return True
    except Exception as exc:
        logger.warning("backup.tar_zst.failed source=%s err=%s", source, exc)
        return False


def _untar_zst(archive: Path, dest_parent: Path) -> bool:
    """Inverse of `_tar_zst`. Detects zstd vs gzip by file magic."""
    if not archive.exists():
        return False
    head = archive.read_bytes()[:4]
    is_zstd = head[:4] == b"\x28\xb5\x2f\xfd"
    dest_parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive.open("rb") as inp:
            if is_zstd:
                decomp = subprocess.Popen(
                    ["zstd", "-d", "-q"],
                    stdin=inp,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            else:
                decomp = subprocess.Popen(
                    ["gzip", "-d"],
                    stdin=inp,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            tar = subprocess.Popen(
                ["tar", "-xf", "-", "-C", str(dest_parent)],
                stdin=decomp.stdout,
                stderr=subprocess.PIPE,
            )
            decomp.stdout.close()  # type: ignore[union-attr]
            tar.wait()
            decomp.wait()
        return tar.returncode == 0
    except Exception as exc:
        logger.warning("backup.untar.failed archive=%s err=%s", archive, exc)
        return False


# ── per-component snapshot ────────────────────────────────────────────────


async def _snap_postgres(out_dir: Path) -> dict[str, Any] | None:
    """Dump Postgres `mission_control` via pg_dump --format=custom.

    Reads creds from env; runs inside backend container so `db:5432`
    resolves via compose network.
    """
    target = out_dir / "postgres.dump"
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    db_name = os.environ.get("POSTGRES_DB", "mission_control")
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    cmd = [
        "pg_dump",
        "-h", "db",
        "-U", user,
        "-d", db_name,
        "-F", "custom",
        "--no-owner", "--no-acl",
        "-f", str(target),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0 or not target.exists():
            logger.warning("backup.postgres.failed err=%s", (err or b"").decode()[:500])
            return None
        return {
            "kind": COMPONENT_POSTGRES,
            "path": "postgres.dump",
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        }
    except FileNotFoundError:
        logger.warning("backup.postgres.no_pg_dump")
        return None


async def _restore_postgres(snap_dir: Path) -> bool:
    target = snap_dir / "postgres.dump"
    if not target.exists():
        return False
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    db_name = os.environ.get("POSTGRES_DB", "mission_control")
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    cmd = [
        "pg_restore",
        "-h", "db",
        "-U", user,
        "-d", db_name,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        str(target),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("backup.postgres.restore_failed err=%s", (err or b"").decode()[:500])
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("backup.postgres.restore_err err=%s", exc)
        return False


def _snap_traffic_sqlite(out_dir: Path) -> dict[str, Any] | None:
    if not _TRAFFIC_DB.exists():
        return None
    target = out_dir / "traffic.sqlite"
    try:
        import sqlite3
        src = sqlite3.connect(f"file:{_TRAFFIC_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(str(target))
        src.backup(dst)
        dst.close()
        src.close()
        return {
            "kind": COMPONENT_TRAFFIC,
            "path": "traffic.sqlite",
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        }
    except Exception as exc:
        logger.warning("backup.traffic.failed err=%s", exc)
        return None


def _restore_traffic_sqlite(snap_dir: Path) -> bool:
    src = snap_dir / "traffic.sqlite"
    if not src.exists():
        return False
    try:
        # Atomic swap via tmp file (sqlite is single-file).
        tmp = _TRAFFIC_DB.with_suffix(".restoring")
        shutil.copy2(src, tmp)
        os.replace(tmp, _TRAFFIC_DB)
        return True
    except Exception as exc:
        logger.warning("backup.traffic.restore_err err=%s", exc)
        return False


def _snap_openclaw_config(out_dir: Path) -> dict[str, Any] | None:
    """Snapshot configs + per-agent agent/ + sessions/ pointers.

    Excludes workspace-* dirs (separate component) and snapshots dir.
    Includes openclaw.json, runtime_settings.json, payloadRules.json,
    all top-level JSON files, identity/, credentials/, agents/*/agent/,
    agents/*/sessions/sessions.json.
    """
    if not _OPENCLAW_HOME.exists():
        return None
    archive = out_dir / "openclaw.tar.zst"
    excludes = [
        "workspace-*",
        "snapshots",
        ".git",
        "*/sessions/*.jsonl",
        "*/sessions/*.jsonl.bak-*",
        "*/sessions/*.jsonl.reset.*",
        "*/sessions/*.trajectory.jsonl",
        "*/sessions/*.trajectory.jsonl.*",
        "node_modules",
        ".cache",
        ".kilocode",
        ".tmp",
    ]
    if not _tar_zst(_OPENCLAW_HOME, archive, excludes=excludes):
        return None
    return {
        "kind": COMPONENT_OPENCLAW_CFG,
        "path": archive.name,
        "size": archive.stat().st_size,
        "sha256": _sha256(archive),
    }


def _restore_openclaw_config(snap_dir: Path) -> bool:
    archive = snap_dir / "openclaw.tar.zst"
    if not archive.exists():
        archive = snap_dir / "openclaw.tar.gz"
    if not archive.exists():
        return False
    # Untar in-place; OPENCLAW_HOME is mounted from host.
    return _untar_zst(archive, _OPENCLAW_HOME.parent)


def _snap_workspaces(out_dir: Path) -> dict[str, Any] | None:
    """Bundle all workspace-* dirs + agent session jsonl files."""
    if not _OPENCLAW_HOME.exists():
        return None
    # Snapshot two paths together: workspace-* and agents/*/sessions/*.jsonl*
    # Done with a small shell-style approach via tar w/ multiple -C paths.
    archive = out_dir / "workspaces.tar.zst"
    use_zstd = shutil.which("zstd") is not None
    suffix = ".tar.zst" if use_zstd else ".tar.gz"
    final = archive.with_suffix("").with_suffix(suffix)

    # Collect names
    names = []
    for p in _OPENCLAW_HOME.iterdir():
        if p.name.startswith("workspace-"):
            names.append(p.name)
    agents_dir = _OPENCLAW_HOME / "agents"
    if agents_dir.exists():
        names.append("agents")

    if not names:
        return None

    cmd_tar = [
        "tar", "-cf", "-",
        "--exclude", "node_modules",
        "--exclude", ".cache",
        "--exclude", ".tmp",
        "-C", str(_OPENCLAW_HOME),
        *names,
    ]
    try:
        with final.open("wb") as out:
            tar = subprocess.Popen(cmd_tar, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if use_zstd:
                comp = subprocess.Popen(["zstd", "-T0", "-3", "-q"], stdin=tar.stdout, stdout=out)
            else:
                comp = subprocess.Popen(["gzip", "-3"], stdin=tar.stdout, stdout=out)
            tar.stdout.close()  # type: ignore[union-attr]
            comp.wait()
            tar.wait()
        if final.stat().st_size == 0:
            final.unlink(missing_ok=True)
            return None
        if final != archive:
            final.rename(archive)
        return {
            "kind": COMPONENT_WORKSPACES,
            "path": archive.name,
            "size": archive.stat().st_size,
            "sha256": _sha256(archive),
        }
    except Exception as exc:
        logger.warning("backup.workspaces.failed err=%s", exc)
        return None


def _restore_workspaces(snap_dir: Path) -> bool:
    archive = snap_dir / "workspaces.tar.zst"
    if not archive.exists():
        archive = snap_dir / "workspaces.tar.gz"
    if not archive.exists():
        return False
    return _untar_zst(archive, _OPENCLAW_HOME)


def _snap_wiki(out_dir: Path) -> dict[str, Any] | None:
    src = _OPENCLAW_HOME / "wiki-vault"
    if not src.exists():
        return None
    archive = out_dir / "wiki-vault.tar.zst"
    if not _tar_zst(src, archive, excludes=[".obsidian/workspace*"]):
        return None
    return {
        "kind": COMPONENT_WIKI,
        "path": archive.name,
        "size": archive.stat().st_size,
        "sha256": _sha256(archive),
    }


def _restore_wiki(snap_dir: Path) -> bool:
    archive = snap_dir / "wiki-vault.tar.zst"
    if not archive.exists():
        archive = snap_dir / "wiki-vault.tar.gz"
    if not archive.exists():
        return False
    return _untar_zst(archive, _OPENCLAW_HOME)


async def _snap_or_volume(out_dir: Path) -> dict[str, Any] | None:
    """OmniRoute volume — read from `/snap-sources/omniroute_data` ro mount."""
    archive = out_dir / "or-volume.tar.zst"
    if not _OR_VOLUME_RO.exists():
        logger.info("backup.or_volume.skipped reason=ro_mount_missing path=%s", _OR_VOLUME_RO)
        return None
    if not _tar_zst(_OR_VOLUME_RO, archive):
        return None
    return {
        "kind": COMPONENT_OR_VOLUME,
        "path": archive.name,
        "size": archive.stat().st_size,
        "sha256": _sha256(archive),
    }


def _restore_or_volume(snap_dir: Path) -> bool:
    """Restore OmniRoute volume content. Caller must stop the omniroute
    container BEFORE invoking restore — writing to a live volume while
    OR is reading it = corruption."""
    archive = snap_dir / "or-volume.tar.zst"
    if not archive.exists():
        archive = snap_dir / "or-volume.tar.gz"
    if not archive.exists():
        return False
    if not _OR_VOLUME_RO.exists():
        logger.warning("backup.or_volume.restore.skipped reason=ro_mount_missing")
        return False
    # NB the mount is `:ro`. Restore needs to write. Strategy: stage to
    # /snapshots/.restore-or-volume-tmp/, then signal owner to swap.
    # For first iteration: just log + return False; owner uses a
    # host-side `docker run` to apply (documented in backups-tab wiki).
    logger.warning(
        "backup.or_volume.restore.deferred reason=ro_mount; "
        "owner action: docker run --rm -v omniroute_data:/v -v "
        "%s:/src alpine sh -c 'cd /v && rm -rf ./* && tar -I zstd -xf /src/or-volume.tar.zst -C /v --strip-components=1'",
        snap_dir,
    )
    return False


def _snap_compose(out_dir: Path) -> dict[str, Any] | None:
    """compose.yml + .env + Dockerfiles. Best-effort — depends on whether
    the project dir is mounted into the backend."""
    if not _COMPOSE_PROJECT_DIR.exists():
        return None
    archive = out_dir / "compose.tar.zst"
    if not _tar_zst(_COMPOSE_PROJECT_DIR, archive):
        return None
    return {
        "kind": COMPONENT_COMPOSE,
        "path": archive.name,
        "size": archive.stat().st_size,
        "sha256": _sha256(archive),
    }


# ── catalog + git tracking ────────────────────────────────────────────────


def _git_commit_manifests(message: str) -> None:
    """Commit current snapshot manifests + index to the snapshots git repo."""
    cmd = ["git", "-C", str(_SNAPSHOT_ROOT)]
    try:
        subprocess.run([*cmd, "add", "-A"], check=False, capture_output=True)
        r = subprocess.run(
            [*cmd, "commit", "-m", message],
            check=False, capture_output=True,
        )
        if r.returncode != 0 and b"nothing to commit" not in r.stdout:
            logger.debug("backup.git_commit.nontrivial out=%s", r.stdout[:200])
    except Exception as exc:
        logger.debug("backup.git_commit.failed err=%s", exc)


def _read_index() -> list[dict[str, Any]]:
    idx = _SNAPSHOT_ROOT / "index" / "index.json"
    if not idx.exists():
        return []
    try:
        return json.loads(idx.read_text())
    except Exception:
        return []


def _write_index(entries: list[dict[str, Any]]) -> None:
    idx_dir = _SNAPSHOT_ROOT / "index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    (idx_dir / "index.json").write_text(json.dumps(entries, indent=2))


# ── public API ─────────────────────────────────────────────────────────────


async def take_snapshot(
    label: str = "",
    components: list[str] | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    """Capture a snapshot. Returns manifest.

    `scope='full'` (default) snapshots every component. Pre-step snapshots
    can pass a narrower list to save disk + time.
    """
    components = components or ALL_COMPONENTS
    ts = _utcnow_str()
    slug = _safe_label(label) if label else "manual"
    snap_id = f"{ts}_{slug}"
    out_dir = _SNAPSHOT_ROOT / "snaps" / snap_id
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    items: list[dict[str, Any]] = []

    if COMPONENT_POSTGRES in components:
        r = await _snap_postgres(out_dir)
        if r: items.append(r)
    if COMPONENT_TRAFFIC in components:
        r = _snap_traffic_sqlite(out_dir)
        if r: items.append(r)
    if COMPONENT_OPENCLAW_CFG in components:
        r = _snap_openclaw_config(out_dir)
        if r: items.append(r)
    if COMPONENT_WORKSPACES in components:
        r = _snap_workspaces(out_dir)
        if r: items.append(r)
    if COMPONENT_WIKI in components:
        r = _snap_wiki(out_dir)
        if r: items.append(r)
    if COMPONENT_OR_VOLUME in components:
        r = await _snap_or_volume(out_dir)
        if r: items.append(r)
    if COMPONENT_COMPOSE in components:
        r = _snap_compose(out_dir)
        if r: items.append(r)

    finished_at = datetime.now(timezone.utc).isoformat()
    total_size = sum(int(it.get("size") or 0) for it in items)

    manifest = {
        "id": snap_id,
        "label": label,
        "reason": reason,
        "started_at": started_at,
        "finished_at": finished_at,
        "total_size": total_size,
        "components": items,
        "scope": "full" if set(components) == set(ALL_COMPONENTS) else "partial",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Update index
    index = _read_index()
    index = [e for e in index if e.get("id") != snap_id]
    index.insert(0, {
        "id": snap_id,
        "label": label,
        "reason": reason,
        "started_at": started_at,
        "finished_at": finished_at,
        "total_size": total_size,
        "component_count": len(items),
        "scope": manifest["scope"],
    })
    _write_index(index)

    _git_commit_manifests(f"snapshot {snap_id} reason={reason} size={total_size}")
    logger.info("backup.snapshot.created id=%s components=%d size=%d", snap_id, len(items), total_size)
    return manifest


def list_snapshots() -> list[dict[str, Any]]:
    return _read_index()


def read_manifest(snapshot_id: str) -> dict[str, Any] | None:
    p = _SNAPSHOT_ROOT / "snaps" / snapshot_id / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


async def restore_snapshot(
    snapshot_id: str,
    components: list[str] | None = None,
) -> dict[str, Any]:
    """Restore selected components from a snapshot.

    Returns status dict per component. Caller (API layer) is responsible
    for issuing container restarts where needed.
    """
    snap_dir = _SNAPSHOT_ROOT / "snaps" / snapshot_id
    if not snap_dir.exists():
        return {"ok": False, "error": "snapshot not found"}
    manifest = read_manifest(snapshot_id)
    if not manifest:
        return {"ok": False, "error": "manifest missing"}

    avail = {it["kind"] for it in manifest["components"]}
    components = components or list(avail)
    results: dict[str, bool] = {}

    if COMPONENT_OPENCLAW_CFG in components and COMPONENT_OPENCLAW_CFG in avail:
        results[COMPONENT_OPENCLAW_CFG] = _restore_openclaw_config(snap_dir)
    if COMPONENT_WORKSPACES in components and COMPONENT_WORKSPACES in avail:
        results[COMPONENT_WORKSPACES] = _restore_workspaces(snap_dir)
    if COMPONENT_WIKI in components and COMPONENT_WIKI in avail:
        results[COMPONENT_WIKI] = _restore_wiki(snap_dir)
    if COMPONENT_TRAFFIC in components and COMPONENT_TRAFFIC in avail:
        results[COMPONENT_TRAFFIC] = _restore_traffic_sqlite(snap_dir)
    if COMPONENT_OR_VOLUME in components and COMPONENT_OR_VOLUME in avail:
        results[COMPONENT_OR_VOLUME] = _restore_or_volume(snap_dir)
    if COMPONENT_POSTGRES in components and COMPONENT_POSTGRES in avail:
        results[COMPONENT_POSTGRES] = await _restore_postgres(snap_dir)

    _git_commit_manifests(
        f"restore from {snapshot_id} components={','.join(components)}"
    )
    logger.info("backup.restore.done id=%s results=%s", snapshot_id, results)
    return {"ok": all(results.values()), "snapshot_id": snapshot_id, "results": results}


async def delete_snapshot(snapshot_id: str) -> bool:
    snap_dir = _SNAPSHOT_ROOT / "snaps" / snapshot_id
    if not snap_dir.exists():
        return False
    shutil.rmtree(snap_dir, ignore_errors=True)
    index = [e for e in _read_index() if e.get("id") != snapshot_id]
    _write_index(index)
    _git_commit_manifests(f"delete {snapshot_id}")
    return True


def diff_snapshots(a_id: str, b_id: str) -> dict[str, Any]:
    """High-level component-level diff. Per-component contents diff is not
    feasible across all blob types; we report which components changed by
    sha256 between two snapshots."""
    a = read_manifest(a_id)
    b = read_manifest(b_id)
    if not a or not b:
        return {"ok": False, "error": "manifest missing"}
    a_map = {it["kind"]: it for it in a["components"]}
    b_map = {it["kind"]: it for it in b["components"]}
    changes = []
    for k in sorted(set(a_map) | set(b_map)):
        ai = a_map.get(k)
        bi = b_map.get(k)
        if not ai:
            changes.append({"component": k, "change": "added", "size": bi["size"]})
        elif not bi:
            changes.append({"component": k, "change": "removed", "size": ai["size"]})
        elif ai["sha256"] != bi["sha256"]:
            changes.append({
                "component": k, "change": "modified",
                "size_a": ai["size"], "size_b": bi["size"],
            })
    return {"ok": True, "from": a_id, "to": b_id, "changes": changes}
