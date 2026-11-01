#!/usr/bin/env python3
"""mc-team-runner — drive one native team cycle for a team-mode board task.

Started by mc-claude-runner.sh (which already holds the global runner lock on
fd 9) for a task whose execution_mode is "team". The coordinator is Mission
Control's deterministic service; this host process only executes the fenced
attempts it hands out: PM (read-only snapshot) -> developers (own worktree each,
bounded parallel) -> independent QA (detached read-only worktree) -> a serial,
exact-commit, fast-forward-only publication. Every result goes back through the
claim-token API, so a restarted or stale worker cannot publish.

  MC_URL / MC_TOKEN, RUNNER_BOARD, RUNNER_LOG, RUNNER_WORKDIR (as the legacy runner)
  RUNNER_TEAM_PARALLEL   developer attempts at once, 1..16        (default 8)
  RUNNER_TEAM_DEPLOY=0   publish to the checkout without deploying (default 1)
  RUNNER_LOCK_DIR        where the per-task lifecycle lock lives   (default /run)
  RUNNER_GLOBAL_LOCK     legacy global lock, held only to publish  (lock_path("mc-claude-runner"))

This process owns one team task end to end. It holds a nonblocking per-task lock
(mc-team-<uuid>.lock) for its whole lifecycle so no second local runner drives the
same cycle, and it grabs the legacy global runner lock ONLY while it mutates the
deployment checkout (publication/deployment), so a native cycle no longer blocks
the legacy lane except during that short serialized window. The supervisor
(mc-team-pool.py) may run many of these at once.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.dont_write_bytecode = True  # importing shared policy must not dirty the deployment checkout
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(HERE))  # sibling helpers (mc_pi_runtime, mc_task_budget) when loaded by path, as tests do
from omc_paths import HOME, etc_path, load_fleet_config, lock_path, log_path, state_dir  # noqa: E402

GO_MOD_CACHE = HOME / "go/pkg/mod"
from app.core.evidence_attachment import (  # noqa: E402 - runner paths bootstrapped above
    EVIDENCE_MANIFEST as COPY_MANIFEST,
    MAX_EVIDENCE_BYTES as EVIDENCE_MAX_BYTES,
    MAX_EVIDENCE_FILES as EVIDENCE_MAX_FILES,
    evidence_appendix,
)
from app.core.evidence_redaction import (evidence_name_valid, evidence_paths_conflict,
    python_identifiers, redact_diff, redact_evidence)
from mc_pi_runtime import qwen_slot  # noqa: E402 - runner paths are bootstrapped above
from mc_skills import SKILLS_MOUNT, SUPERPOWERS, ponytail, superpowers, superpowers_index  # noqa: E402
from mc_task_budget import ApiError, check as check_task_budget, request

MC = str(HERE / "mc")
BOARD = os.environ.get("RUNNER_BOARD", "")
PRIVATE_BOARDS = set(load_fleet_config().get("private_board_ids", []))  # content may reach local models only
# Per-board target repository (a dedicated clean clone). Mission Control stays the
# default: it alone is deployed; other repos publish as a pushed branch + PR.
MC_REPO = REPO
try:
    REPO = Path(json.loads(Path(os.environ.get("RUNNER_BOARD_REPOS", str(etc_path("mc-team/board-repos.json")))).read_text()).get(BOARD) or REPO)
except (OSError, ValueError, AttributeError):
    pass
LOG = os.environ.get("RUNNER_LOG", str(log_path("mc-claude-runner")))
WORKDIR = os.environ.get("RUNNER_WORKDIR", str(HOME / "claude-work"))
PARALLEL = max(1, min(16, int(os.environ.get("RUNNER_TEAM_PARALLEL", "8"))))
DEPLOY = os.environ.get("RUNNER_TEAM_DEPLOY", "1") == "1" and REPO == MC_REPO
PONYTAIL = os.environ.get("RUNNER_PONYTAIL_SKILL", str(HOME / ".claude/skills/ponytail"))
PONYTAIL_LEVEL, PONYTAIL_RULES = ponytail()
TOTAL_TIMEOUT = int(os.environ.get("RUNNER_TIMEOUT", "3600"))
LOCK_DIR = os.environ.get("RUNNER_LOCK_DIR", str(lock_path("mc-claude-runner").parent))
GLOBAL_LOCK = os.environ.get("RUNNER_GLOBAL_LOCK", str(lock_path("mc-claude-runner")))
MAX_REFRESHES = max(1, int(os.environ.get("RUNNER_TEAM_MAX_REFRESHES", "16")))
GIT_LOCK = threading.Lock()
STOP = threading.Event()
BAD_PROVIDERS = ("openai-codex-1", "openai-codex")  # LG refresh belongs to OmniRoute
NATIVE_ACCOUNTS = HOME / ".pi/accounts"
CANONICAL_PI = HOME / ".pi/agent"
# Known token-bearing source file the host masks from every worker view.
PRIVATE_MONITOR = Path("backend/scripts/ollama_monitor.py")
# Raw project/history/results never belong in a general cloud-worker projection.
# Only separately approved safe exports may be admitted from a different scope.
LOCAL_ONLY_ROOTS = (
    *(Path(p) for p in load_fleet_config().get("forbidden_paths", [])),
    state_dir("private-local-review"),
)


def refuse_local_only_projection(*paths: Path) -> None:
    for path in paths:
        resolved = path.resolve()
        if any(resolved == root or root in resolved.parents or resolved in root.parents
               for root in LOCAL_ONLY_ROOTS):
            raise RuntimeError("local-only source cannot enter worker projection or evidence export")

# Explicit read-only host dependencies of a sandboxed worker: system runtime,
# libs, certs and resolver config, plus the managed Pi/Node install. Nothing
# else from the host is mounted — no root mount, no other checkouts, no host
# Git object databases, no operator/private state.
SYSTEM_RO = (
    "/usr",
    "/etc/ssl/certs",
    "/etc/ssl/cert.pem",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/localtime",
    "/etc/ld.so.cache",
)
MANAGED_RO = (
    str(HOME / ".pi/agent/bin"), str(HOME / ".pi/agent/install"),
    str(HOME / ".local/share/pi-node"), str(HOME / ".local/share/uv/python"),
)
QA_EVIDENCE_LIMIT = 512 * 1024  # bounded diff handed to the QA worker
# 2026-10-07 canary 0000a012: verifiers kept their probes under /tmp, a throwaway
# directory inside the sandbox, and did not paste them into the final reply, so QA
# could never re-run them. A worker's /tmp is now a host directory; after a
# developer/specialist attempt its text files go to the backend, which attaches the
# credential-free ones verbatim (shared bounded evidence_appendix policy).
EVIDENCE_SUFFIXES = frozenset(
    {".py", ".sh", ".sql", ".js", ".mjs", ".ts", ".tsx", ".md", ".txt", ".log", ".json", ".toml", ".yaml", ".yml"}
)
QA_EVIDENCE_BLOB_LIMIT = 8 * 1024 * 1024  # per-file hash bound


def log(msg: str) -> None:
    with open(LOG, "a") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()} team: {msg}\n")


def task_lock(task_id: str) -> int | None:
    """Nonblocking per-task lifecycle lock; the fd is held open for the whole run.

    Returns the open fd (kept alive until the process exits, which releases the
    lock) or None if another local runner already drives this task.
    """
    Path(LOCK_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(LOCK_DIR) / f"mc-team-{task_id}.lock"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextlib.contextmanager
def global_lock(deadline: float):
    """The legacy global runner lock, taken ONLY to publish/deploy.

    Blocks (polling) until acquired or the cycle deadline passes, so native
    publication serializes with the legacy runner and sibling team publishers.
    While held, the deployment checkout cannot move under us.
    """
    fd = os.open(GLOBAL_LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "global publication lock not free before deadline"
                    )
                time.sleep(1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def engine_on_hold(exc: Exception) -> bool:
    """The backend's canonical engine-availability refusal: a wait, not a failure."""
    return isinstance(exc, ApiError) and exc.code in (409, 422) and bool(
        re.search(r"[\w.-]+/[\w.:-]+ is (?:disabled or ineligible|temporarily unavailable)$", exc.text.strip())
    )


def cycle_stop_line(exc: Exception) -> str:
    """Pool-log line for a cycle that stopped. str(ApiError) keeps 200 chars, which cut the
    engine hold off long claim paths, so the watchdog read quota waits as failures (2026-10-22)."""
    if engine_on_hold(exc):
        return f"team cycle waiting on engine hold: {exc.text.strip().rsplit(': ', 1)[-1]}"
    return f"team cycle could not proceed: {exc}"


def api(method: str, path: str, body: dict | None = None):
    return request(MC, method, path, body)


def git(cwd, *args, env=None, check=True, raw=False) -> str | bytes:
    p = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
        capture_output=True,
        text=not raw,
        env={**os.environ, **(env or {})},
    )
    if check and p.returncode:
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout if raw else p.stdout.rstrip()


def dependency_branches(task: dict) -> list[str]:
    """Newest published fleet branch (origin/omc/<task8>-*) of each dependency, if any. A split
    dependency publishes nothing itself, so its published child tasks stand in for it (2026-10-26)."""
    deps, rows = list(task.get("depends_on_task_ids") or []), []
    try:
        for st in ("done", "review") if deps else ():
            for offset in range(0, 10_000, 200):  # the API pages at 200 rows at most
                page = (api("get", f"/api/v1/boards/{BOARD}/tasks?status={st}&limit=200&offset={offset}")
                        or {}).get("items", [])
                rows += page
                if len(page) < 200:
                    break
    except Exception:  # noqa: BLE001 - the dependencies' own branches still apply
        pass
    for dep in deps:
        deps += [t["id"] for t in rows if t.get("parent_task_id") == dep and t["id"] not in deps]
    refs = (git(REPO, "for-each-ref", "--sort=-committerdate", "--count=1", "--format=%(refname:short)",
                f"refs/remotes/origin/omc/{dep[:8]}-*", check=False)
            for dep in deps)
    return [r for r in refs if r]


def contains(ref: str, commit: str) -> bool:
    """True when ref is already part of commit's history."""
    return subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", ref, commit],
                          capture_output=True).returncode == 0


WORKFLOWS = ".github/workflows"
CI_NOTE = (f"\n\nCI workflow edits ({WORKFLOWS}) cannot be pushed by this fleet: publication moves "
           "them into the pull request body for a human to add. Plan them like any file, but no "
           "acceptance criterion may depend on CI running them before a human merges.")


def without_workflows(sha: str) -> tuple[str, str]:
    """(new tip, dropped diff): sha's commits that origin lacks, rewritten so none changes
    WORKFLOWS (each keeps its first parent's). GitHub checks every pushed commit, so a revert on
    top is not enough. Deterministic, so stacked fleet branches keep sharing their history."""
    new: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="omc-strip.") as tmp:
        idx = {"GIT_INDEX_FILE": f"{tmp}/index"}
        for c in git(REPO, "rev-list", "--reverse", "--topo-order", sha, "--not", "--remotes=origin").split():
            parents = [new.get(p, p) for p in git(REPO, "rev-parse", f"{c}^@").split()]
            git(REPO, "read-tree", c, env=idx)
            git(REPO, "rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", WORKFLOWS, env=idx)
            if parents and git(REPO, "rev-parse", "-q", "--verify", f"{parents[0]}:{WORKFLOWS}", check=False):
                git(REPO, "read-tree", f"--prefix={WORKFLOWS}/", f"{parents[0]}:{WORKFLOWS}", env=idx)
            meta = git(REPO, "log", "-1", "--date=raw", "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd%x00%B", c)
            an, ae, ad, cn, ce, cd, msg = meta.split("\0", 6)
            who = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
                   "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce, "GIT_COMMITTER_DATE": cd}
            tree = git(REPO, "write-tree", env=idx)
            new[c] = git(REPO, "commit-tree", tree, *(a for p in parents for a in ("-p", p)), "-m", msg, env=who)
    head = new.get(sha, sha)
    return head, git(REPO, "diff", head, sha, "--", WORKFLOWS)


def identity(name: str) -> dict:
    return {
        "GIT_AUTHOR_NAME": f"mc-team/{name}",
        "GIT_COMMITTER_NAME": f"mc-team/{name}",
        "GIT_AUTHOR_EMAIL": "mc-team@localhost",
        "GIT_COMMITTER_EMAIL": "mc-team@localhost",
    }


def harness_cmd(
    provider: str,
    model: str,
    thinking: str,
    prompt: str,
    session_dir: str,
    attempt_id: str,
):
    """(argv, extra_env) for canonical Claude CLI / native Pi engines, else None."""
    if provider in BAD_PROVIDERS:
        return None
    if provider == "claude-code":
        name = {
            "opus-5": "claude-opus-5",
            "opus-4-8": "claude-opus-4-8",
            # canonical CLI name for the acceptance-review engine
            "opus-5-5": "claude-opus-5-5",
        }.get(model, model)
        argv = [
            "claude",
            "-p",
            prompt,
            "--model",
            name,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash,Read,Edit,Write,Glob,Grep",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--safe-mode",
            "--no-session-persistence",
        ]
        # The coordinator resolved and clamped effort to what this CLI accepts; it was
        # previously never forwarded, so every Claude attempt ran at the CLI default.
        if thinking in ("low", "medium", "high", "xhigh", "max"):
            argv += ["--effort", thinking]
        return argv, {}
    pi_dir, pi_provider, pi_model = str(CANONICAL_PI), None, model
    if provider == "openai-codex-canonical":
        if model not in ("gpt-6.1-sol", "gpt-6-luna"):
            return None
        pi_dir, pi_provider = str(CANONICAL_PI), "openai-codex"
    elif provider.startswith("openai-codex-"):
        account = provider[len("openai-codex-") :]
        pi_dir = f"{NATIVE_ACCOUNTS}/{provider}"
        if re.fullmatch(r"[A-Za-z0-9_-]+", account) and os.access(
            f"{pi_dir}/auth.json", os.R_OK
        ):
            pi_provider = "openai-codex"
    elif provider == "vllm" and model == "Qwen3.8-27B-FP8":
        pi_provider = "openclaw-infer"
    elif provider == "metis" and model == "qwen3.8":
        pi_provider = "metis"  # on-prem vLLM, ~/.pi/agent/models.json
    elif provider == "strata":
        pi_provider = "strata-gfx1030"  # llama.cpp on the owner's strata GPU, same models.json
    elif provider == "pi-neuralwatt":
        pi_provider = "neuralwatt"
    elif provider == "omniroute-codex":
        # Pi through OmniRoute, which rotates its codex accounts (Pi provider "omniroute-api"
        # in ~/.pi/agent/models.json lists "codex/<model>").
        pi_provider, pi_model = "omniroute-api", f"codex/{model}"
    elif provider == "deepseek-sub":
        pi_provider = "deepseek"
    else:
        pi_provider, pi_model = {
            ("ollama-cloud-sub", "glm-5.2"): ("ollama-glm", "glm-5.2"),
            ("ollama-cloud-sub", "glm-5.3"): ("ollama-glm", "glm-5.3"),
            ("ollama-cloud-sub", "kimi-k3"): ("ollama-kimik3", "kimi-k3"),
            ("ollama-cloud-sub", "deepseek-v4-pro:cloud"): (
                "ollama-deepseek",
                "deepseek-v4-pro:cloud",
            ),
        }.get((provider, model), (None, model))
    if not pi_provider:
        return None
    argv = [
        str(CANONICAL_PI / "bin/pi"),
        "-p",
        "--approve",
        "--session-dir",
        session_dir,
        "--session-id",
        f"mc-team-{attempt_id}",
        "--mode",
        "json",
        "--provider",
        pi_provider,
        "--model",
        pi_model,
        "--thinking",
        thinking or "low",
    ]
    if provider == "openai-codex-canonical":
        argv += ["--offline", "--no-extensions", "--no-skills", "--no-context-files",
                 "--no-prompt-templates", "--no-themes", "--no-mcp"]
    elif os.path.isdir(PONYTAIL):
        argv += ["--skill", PONYTAIL]
    # The prompt is an operand, never an option: a leading "- " Markdown
    # prompt must not be parsed as a flag (the installed Pi CLI resolves
    # operands after "--"; see its cli/args.js parser).
    return argv + ["--", prompt], {"PI_CODING_AGENT_DIR": pi_dir}


def _mask(args: list[str], path: Path) -> None:
    """Shadow one existing host path with an empty, private replacement."""
    try:
        path = path.resolve()
    except OSError:
        return
    if not path.exists():
        return
    args += (
        ["--tmpfs", str(path)]
        if path.is_dir()
        else ["--ro-bind", "/dev/null", str(path)]
    )


def _ro_bind(args: list[str], path: Path, covered: list[Path]) -> None:
    """Bind one more host dependency read-only, unless already projected."""
    refuse_local_only_projection(path)
    try:
        real = Path(path).resolve()
    except OSError:
        return
    if not real.exists() or any(real == c or c in real.parents for c in covered):
        return
    args += ["--ro-bind", str(path), str(path)]
    covered.append(real)


def _mount_program(args: list[str], exe: str, covered: list[Path]) -> None:
    """Project a CLI executable, its shebang interpreter and any virtualenv.

    Fixture CLIs are mounted as the single executable file only, never their
    containing host directory; a virtualenv interpreter needs its whole tree
    (pyvenv.cfg, site-packages) plus the real interpreter it symlinks to.
    """
    path = Path(exe)
    if (path.parent.parent / "pyvenv.cfg").is_file():
        _ro_bind(args, path.parent.parent, covered)
        for name in ("python", "python3"):
            sibling = path.parent / name
            if sibling.exists():
                _ro_bind(args, sibling.resolve(), covered)
        return
    _ro_bind(args, path, covered)
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return
    if not first.startswith(b"#!"):
        return
    parts = first[2:].strip().decode("utf-8", "replace").split()
    if not parts:
        return
    if Path(parts[0]).name == "env" and len(parts) > 1:
        found = shutil.which(parts[1])
        if found:
            _mount_program(args, found, covered)
    elif os.path.isabs(parts[0]):
        _mount_program(args, parts[0], covered)


def _hidden_host_paths() -> list[Path]:
    """Known private host state kept masked (defense in depth on top of the
    allowlist: these paths are never mounted in the first place, but an
    existing path is also shadowed so a future mount cannot resurrect it)."""
    paths = [
        REPO / ".env",
        REPO / ".env.production",
        HOME / ".ssh",
        HOME / ".local/state",
        HOME / ".config",
        HOME / ".codex",
        HOME / ".openclaw",
        NATIVE_ACCOUNTS,
        MC_REPO / ".env",
        HOME / "claude-work/.env",
        *(Path(p) for p in load_fleet_config().get("hidden_paths", [])),
    ]
    try:
        paths += list(CANONICAL_PI.glob("auth*.json*"))
        paths += list(CANONICAL_PI.glob("models.json*"))
    except OSError:
        pass
    return paths


def _private_source_path(path: str) -> bool:
    return path in (PRIVATE_MONITOR.as_posix(), "docs/operations/README.md") or any(p.startswith(".env") for p in Path(path).parts)


def _private_source_paths(cwd: Path):
    for relative in (PRIVATE_MONITOR, Path("docs/operations/README.md")):
        path = cwd / relative
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("Private source path traverses a symlink")
    for root, dirs, files in os.walk(cwd):
        for name in dirs + files:
            path = Path(root) / name
            if _private_source_path(path.relative_to(cwd).as_posix()):
                yield path
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules") and not d.startswith(".env")]


def _claim_capability(claim: dict) -> str:
    """Server-derived attempt capability from the claim response.

    Falls back to the fixed role-kind capability only for old fixture claims
    that predate the server-supplied field; it never widens beyond it.
    """
    if claim.get("role_kind") not in ("developer", "specialist"):
        return "read_only"
    # A fixed writer role may be narrowed (verify), never widened by a claim.
    return "write_worktree" if claim.get("capability", "write_worktree") == "write_worktree" else "read_only"


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """True when production's history contains `ancestor` (unknown commits: False)."""
    return (
        subprocess.run(
            ["git", "-C", str(REPO), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
        ).returncode
        == 0
    )


HARNESS_EXTRA_KEYS = frozenset({"PI_CODING_AGENT_DIR"})


def _pi_dirs(provider: str, tmp: Path) -> set[str]:
    """Every value the runner itself may give PI_CODING_AGENT_DIR (exact match only).

    2026-10-07 review: validating where a harness string resolves still forwarded the
    string, so a path component could carry a secret. Only these runner-derived values
    pass: the canonical Pi agent, a validated Codex account, or this attempt's API runtime.
    """
    dirs = {str(CANONICAL_PI), str(tmp / "api-runtime")}
    if provider != "openai-codex-canonical" and re.fullmatch(r"openai-codex-[A-Za-z0-9_-]+", provider):
        dirs.add(f"{NATIVE_ACCOUNTS}/{provider}")
    return dirs


def fixtures_enabled() -> bool:
    """Test-only hooks (FIXTURE_* passthrough, FIXTURE_LOG bind) need an explicit opt-in.

    2026-10-07 review: an inherited FIXTURE_* variable or FIXTURE_LOG path would
    otherwise reach a production worker unfiltered, or bind a masked host file.
    """
    return os.environ.get("RUNNER_FIXTURES") == "1"


def evidence_dir(tmp: Path) -> Path:
    """Host directory mounted as the worker's /tmp (sibling of its scratch)."""
    return Path(f"{tmp}.evidence")


def repo_blobs(sha: str | None) -> dict[str, str]:
    """path -> blob id of every file at `sha` in production; {} when unknown.

    NUL-separated bytes (any path is safe), bounded: a failing or hung Git only
    disables copy detection, it never holds the completion.
    """
    if not sha:
        return {}
    try:
        p = subprocess.run(
            ["git", "-C", str(REPO), "ls-tree", "-r", "-z", sha], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    blobs: dict[str, str] = {}
    for record in p.stdout.split(b"\0") if p.returncode == 0 else []:
        meta, _, name = record.partition(b"\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == b"blob" and name:
            blobs[name.decode("utf-8", "surrogateescape")] = fields[2].decode()
    return blobs


# .list is never collected (no clash); shared policy attaches the manifest first.
COPY_MANIFEST_LINES = 15


def _shown(name: str) -> str:
    """Pure-ASCII form of a path: control and non-UTF-8 characters are escaped.

    Long names keep a prefix plus a digest of the whole name, so two names
    sharing a 150-character prefix never print the same.
    """
    text = ascii(name)[1:-1]
    if len(text) <= 200:
        return text
    return f"{text[:150]}...[sha1 {hashlib.sha1(text.encode()).hexdigest()[:12]}]"


def repository_twin(rel: Path, data: bytes, known: dict[str, str]) -> str | None:
    """The repository path this file's path ends with, if its bytes are identical.

    Only the longest repository path the file's path ends with is compared, so a
    modified copy never matches a shorter, unrelated repository file.
    """
    for i in range(len(rel.parts)):
        twin = "/".join(rel.parts[i:])
        if twin in known:
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            return twin if known[twin] == blob else None
    return None


def collect_evidence(
    root: Path, known: dict[str, str] | None = None, cited: str = ""
) -> dict[str, str]:
    """Text files a worker left under its /tmp, for the backend to vet and attach.

    A file whose path ends with a repository path and whose bytes are identical to
    that file at the base commit is not attached (canary 0000a012, cycle 5: a
    verifier copied the tree to write tests and the copies filled the budget). Each
    such file is named in a manifest with the repository path holding its exact
    bytes, so nothing disappears silently; QA has the repository. Files whose /tmp
    path the worker's final reply cites go first (canary cycle 6: an unpacked
    PostgreSQL tree sorted before the cited t067_*.py probes and took every slot).
    A cited file that cannot be attached whole is left out, not a failed leaf: QA
    judges the repository diff (2026-10-26: 8 finished docs leaves were discarded
    because their reports cited scratch audits over the 64 KiB budget).
    """
    refuse_local_only_projection(root)
    files: dict[str, str] = {}
    twins: list[str] = []
    candidates: list[Path] = []
    # Whole cited paths only: citing /tmp/probe.py.real.py must not cite /tmp/probe.py.
    named = {re.sub(r":\d+(?::\d+)?$", "", m.rstrip(".,:;!?")) for m in re.findall(r"/tmp/[^\s'\"`()<>\[\]{}|*]+", cited)}
    paths = sorted(root.rglob("*"))
    paths.sort(key=lambda p: f"/tmp/{p.relative_to(root).as_posix()}" not in named)  # stable
    for path in paths:
        rel = path.relative_to(root)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix not in EVIDENCE_SUFFIXES
            or rel.parts[0].startswith(("mc-", "claude-"))  # bind mount points; Claude Code's own scratch
            or any(part.startswith(".") or part == "__pycache__" for part in rel.parts)
        ):
            continue
        stat = path.stat()
        if stat.st_nlink != 1 or stat.st_size <= 0:
            continue
        if stat.st_size > EVIDENCE_MAX_BYTES:
            continue
        try:
            twin = repository_twin(rel, path.read_bytes(), known) if known else None
        except OSError:
            continue
        if twin is not None:
            twins.append(f"/tmp/{_shown(rel.as_posix())} -> {_shown(twin)}")
        else:
            candidates.append(path)
    if twins:
        shown = twins[:COPY_MANIFEST_LINES]
        more = len(twins) - len(shown)
        files[COPY_MANIFEST] = (
            "Not attached: identical bytes to the named repository file at the base commit.\n"
            + "\n".join(shown)
            + (f"\n... and {more} more" if more else "")
            + "\n"
        )
    # Discover the actual manifest first: no speculative slot/byte reservation.
    # Admission renders the very same bounded packet that completion will retain.
    for path in candidates:
        name = path.relative_to(root).as_posix()
        if len(files) >= EVIDENCE_MAX_FILES and f"/tmp/{name}" not in named:
            break  # citations were sorted first; no remaining file can get a slot
        try:
            text = path.read_bytes().decode("utf-8")  # verbatim, no newline translation
        except (OSError, UnicodeDecodeError):
            continue
        trial = {**files, name: text}
        attached: set[str] = set()
        evidence_appendix(trial, attached=attached)
        if attached == set(trial):
            files = trial
    return files


def stage_evidence_files(root: Path, files: dict[str, str]) -> None:
    """Project actual retained UTF-8 fields into QA /tmp; no prose reconstruction."""
    if not isinstance(files, dict) or len(files) > EVIDENCE_MAX_FILES or root.is_symlink():
        raise ValueError("invalid whole structured evidence")
    paths: dict[Path, bytes] = {}
    for name, text in files.items():
        if not evidence_name_valid(name) or not isinstance(text, str):
            raise ValueError("invalid structured evidence path/content")
        path = root / name
        if path.exists() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError("structured evidence destination collision")
        try:
            paths[path] = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("structured evidence is not UTF-8") from exc
    if (sum(map(len, paths.values())) > EVIDENCE_MAX_BYTES
            or evidence_paths_conflict(files)):
        raise ValueError("whole structured evidence exceeds bounds or conflicts")
    # Entire payload validated before any write. Source bytes stay in the backend.
    for path, raw in paths.items():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as out:
            out.write(raw)
        path.chmod(0o600)


def sandbox_command(
    argv: list[str],
    cwd: Path,
    tmp: Path,
    *,
    writable: bool,
    native_account: Path | None = None,
    canonical_pi: bool = False,
) -> list[str]:
    """Allowlisted host projection; only the attempt scope is ever writable.

    System runtime/libs/certs, the managed Pi/Node install, installed CLI and
    interpreter dependencies are mounted read-only. The attempt scratch is
    writable, the current worktree is writable only for developer/specialist,
    and selected canonical credential maintenance stays shared as validated.
    The host root is never mounted: operator state, other checkouts and every
    host Git object database are absent, and each worktree's `.git` pointer
    stays visible but immutable with its referenced gitdir unmounted.
    """
    refuse_local_only_projection(cwd, tmp, evidence_dir(tmp))
    if not shutil.which("bwrap"):
        raise RuntimeError(
            "Bubblewrap is required; refusing an unsandboxed team worker"
        )
    args = ["bwrap", "--die-with-parent", "--unshare-pid", "--ro-bind", "/usr", "/usr"]
    for link in ("bin", "lib", "lib64"):  # loader and libs live under /usr
        args += ["--symlink", f"usr/{link}", f"/{link}"]
    args += ["--dev", "/dev", "--proc", "/proc", "--dir", str(HOME)]
    if evidence_dir(tmp).is_dir():  # first, so later binds under /tmp stay visible
        args += ["--bind", str(evidence_dir(tmp)), "/tmp"]
    covered = [Path("/usr"), Path("/dev"), Path("/proc")]
    for system in SYSTEM_RO[1:]:
        if Path(system).exists():
            _ro_bind(args, Path(system), covered)
    for managed in MANAGED_RO:
        if Path(managed).is_dir():
            _ro_bind(args, Path(managed), covered)
    # The program the worker actually runs, including fixture executables (as
    # single read-only files) and any shebang/virtualenv interpreter targets.
    if argv:
        exe = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
        if exe:
            _mount_program(args, exe, covered)
    for path in _hidden_host_paths():
        _mask(args, path)
    if argv and Path(argv[0]).name == "claude":
        canonical = HOME / ".claude"
        if canonical.is_dir():
            # Credential maintenance stays canonical: .credentials.json and its
            # lock remain writable in place. Unrelated historical/config/project/
            # session content in that directory is masked, not merely read-only.
            args += ["--bind", str(canonical), str(canonical)]
            for child in canonical.iterdir():
                if child.name not in (".credentials.json", ".credentials.json.lock"):
                    _mask(args, child)
    if os.path.isdir(PONYTAIL):
        _ro_bind(args, Path(PONYTAIL), covered)
    if SUPERPOWERS.is_dir():  # read by the prompt's skill index; not under ~/.pi, which private-board masks
        args += ["--ro-bind", str(SUPERPOWERS), SKILLS_MOUNT]
    if canonical_pi and (native_account != CANONICAL_PI or not canonical_pi_authorized(0)):
        raise RuntimeError("refusing unselected canonical Pi credential maintenance")
    if native_account is not None:
        if (
            (native_account.parent != NATIVE_ACCOUNTS and not canonical_pi)
            or native_account.is_symlink()
            or native_account.resolve() != native_account
            or (not canonical_pi and not re.fullmatch(
                r"openai-codex-(?!1$)[A-Za-z0-9_-]+", native_account.name
            ))
        ):
            raise RuntimeError("refusing non-isolated or LG credential maintenance")
        # Pi locks and rotates canonical auth in place. Never copy rotating tokens:
        # the account directory keeps proper-lockfile visible to sibling workers.
        args += ["--bind", str(native_account), str(native_account)]
        for child in native_account.iterdir():
            if child.name not in ("auth.json", "auth.json.lock"):
                if canonical_pi and child.name in ("bin", "install"):
                    args += ["--ro-bind", str(child), str(child)]
                else:
                    _mask(args, child)
        if canonical_pi:
            from mc_pi_runtime import compaction_settings

            settings = tmp / "canonical-settings.json"
            settings.write_text(json.dumps(compaction_settings("openai-codex", argv[argv.index("--model") + 1] if "--model" in argv else "", CANONICAL_PI / "settings.json")))
            models = tmp / "canonical-models.json"
            models.write_text('{"providers":{}}')
            for source, name in ((settings, "settings.json"), (models, "models.json")):
                source.chmod(0o600)
                args += ["--ro-bind", str(source), str(CANONICAL_PI / name)]
    args += ["--bind", str(tmp), str(tmp)]
    args += ["--bind" if writable else "--ro-bind", str(cwd), str(cwd)]
    # The worktree's Git pointer stays visible but immutable; the host Git
    # object database it references is not mounted, so no Git operation against
    # host history can run inside the sandbox (tamper is denied before it
    # happens, and the host re-checks the pointer bytes after the model ends).
    pointer = cwd / ".git"
    if pointer.is_dir():
        args += ["--tmpfs", str(pointer)]  # a main checkout's objects are private too
    elif pointer.exists():
        args += ["--ro-bind", str(pointer), str(pointer)]
    # Apply the SAME private-source policy as QA evidence after the final
    # worktree bind. Skip Git/dependency trees, not nested backend/frontend envs.
    for path in _private_source_paths(cwd):
        _mask(args, path)
    # Reuse installed dependencies read-only; mounts create no staged symlinks.
    # An earlier build (QA's evidence preflight) leaves the empty mount point
    # behind; mount over that too, or QA silently loses pytest/node_modules.
    for relative in ("backend/.venv", "frontend/node_modules"):
        source = REPO / relative
        refuse_local_only_projection(source)
        destination = cwd / relative
        empty = destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir())
        if source.is_dir() and destination.parent.is_dir() and (empty or not destination.exists()):
            destination.mkdir(exist_ok=True)
            args += ["--ro-bind", str(source), str(destination)]
    gomod = GO_MOD_CACHE
    if (cwd / "go.mod").is_file() and gomod.is_dir():
        args += ["--ro-bind", str(gomod), str(gomod)]
    # Offline fake-CLI evidence file only; never set by a model or API payload.
    fixture = os.environ.get("FIXTURE_LOG") if fixtures_enabled() else None
    if fixture:
        Path(fixture).touch(exist_ok=True)
        args += ["--bind", fixture, fixture]
    return args + ["--chdir", str(cwd), "--", *argv]


def canonical_pi_authorized(timeout: int) -> bool:
    """Explicit operator-selected Pi owner/account, valid access for the whole leaf.

    No OAuth copy or refresh. Keep historical generic/LG and numbered leases separate.
    """
    try:
        pin = os.environ.get("RUNNER_CANONICAL_PI_ACCOUNT_SHA256", "")
        auth_path = CANONICAL_PI / "auth.json"
        if CANONICAL_PI.is_symlink() or CANONICAL_PI.resolve() != CANONICAL_PI or auth_path.is_symlink():
            return False
        auth = json.loads(auth_path.read_text())["openai-codex"]
        account, expiry = auth["accountId"], auth["expires"]
        return bool(re.fullmatch(r"[0-9a-f]{64}", pin)
                    and isinstance(account, str) and account
                    and auth.get("type") == "oauth"
                    and isinstance(auth.get("access"), str) and bool(auth["access"])
                    and hashlib.sha256(account.encode()).hexdigest() == pin
                    and type(expiry) in (int, float) and math.isfinite(expiry)
                    and expiry / 1000 > time.time() + timeout + 300)
    except (KeyError, ValueError, OSError, TypeError, OverflowError):
        return False


def _private_profile_refusal(attempt: dict) -> dict:
    return {
        "success": False, "retryable": False, "usage_observed": False,
        "text": "private-board transport required",
        "usage": {"provider": attempt["provider"], "model": attempt["model"],
                  "source": "pi_cli", "success": False},
    }


def _run_harness(
    claim: dict,
    prompt: str,
    cwd: Path,
    tmp: Path,
    timeout: int,
    cancelled: threading.Event | None = None,
    test_db: str | None = None,
) -> dict:
    a = claim["attempt"]
    skills = superpowers(claim.get("role_kind"), _claim_capability(claim))
    if index := superpowers_index(skills):  # one place for every harness: Claude, Pi, canonical codex, private-board, reviewer
        prompt = f"{index}\n\n---\n\n{prompt}"
    if PONYTAIL_RULES:
        prompt = f"{PONYTAIL_RULES}\n\n---\n\n{prompt}"
    private_scope = BOARD in PRIVATE_BOARDS or claim.get("board_id") in PRIVATE_BOARDS
    if private_scope and a["provider"] == "claude-code":
        # Intelligence qualification does not qualify an unisolated transport.
        return _private_profile_refusal(a)
    built = harness_cmd(
        a["provider"],
        a["model"],
        a["thinking_level"],
        prompt,
        str(tmp / "sessions"),
        a["id"],  # full per-attempt session ID; no author-session reuse or prefix collision
    )
    raw, rc, interrupted = tmp / "out.raw", 78, ""
    if built is not None and a["provider"] == "openai-codex-canonical" and not canonical_pi_authorized(timeout):
        raw.write_text("credential store read failed: canonical Pi account pin or access window unavailable; no refresh attempted")
        built = None
    if built is not None and a["provider"] == "openai-codex-2" and not (fixtures_enabled() and os.environ.get("FIXTURE_LOG")):
        # ponytail: AD is a copied authorization. Never cross its expiry;
        # unify refresh ownership before permitting automatic rotation.
        try:
            auth = json.loads((Path(built[1]["PI_CODING_AGENT_DIR"]) / "auth.json").read_text())
            expiry = float(auth["openai-codex"]["expires"]) / 1000
        except (KeyError, ValueError, OSError, TypeError): expiry = 0
        if not math.isfinite(expiry) or expiry <= time.time() + timeout + 300:
            raw.write_text("credential store read failed: AD access lease requires canonical-owner renewal; no refresh attempted")
            built = None
    if built is None:
        if private_scope:
            return _private_profile_refusal(a)
        if not raw.exists(): raw.write_text(f"Unsupported harness engine: {a['provider']}/{a['model']}\n")
    else:
        argv, extra = built
        if (
            a["provider"] != "claude-code"
            and not a["provider"].startswith("openai-codex-")
            and not (fixtures_enabled() and os.environ.get("FIXTURE_LOG"))
        ):
            from mc_pi_runtime import key_runtime

            selected = argv[argv.index("--provider") + 1]
            selected_model = argv[argv.index("--model") + 1]
            extra["PI_CODING_AGENT_DIR"] = str(
                key_runtime(selected, selected_model, tmp, session_tag=f"mc-team-{a['id']}")
            )
        if private_scope:
            from mc_pi_runtime import compaction_settings

            selected = argv[argv.index("--provider") + 1]
            selected_model = argv[argv.index("--model") + 1]
            runtime = extra.get("PI_CODING_AGENT_DIR", "")
            if runtime not in _pi_dirs(a["provider"], tmp):
                return _private_profile_refusal(a)
            worker = tmp / "private-pi-worker.mjs"
            worker.write_bytes((HERE / "private-pi-worker.mjs").read_bytes())
            worker.chmod(0o600)
            # A file, not an argv operand: one argument over 128 KiB fails execve with
            # E2BIG (OSError), and QA prompts carry every leaf report (0000a00f, 2026-10-25).
            prompt_file = tmp / "private-prompt.txt"
            prompt_file.write_text(prompt)
            prompt_file.chmod(0o600)
            spec = {
                "cwd": str(cwd), "private_dir": str(tmp), "agent_dir": runtime,
                "prompt_file": str(prompt_file),
                "session_dir": str(tmp / "sessions"), "session_id": f"mc-team-{a['id']}",
                "provider": selected, "model": selected_model, "thinking": a["thinking_level"],
                "write_worktree": _claim_capability(claim) == "write_worktree",
                "settings": compaction_settings(selected, selected_model, Path(runtime) / "settings.json"),
            }
            argv = [str(HOME / ".local/share/pi-node/current/bin/node"), str(worker), json.dumps(spec),
                    "--model", selected_model]  # preserve the canonical outer settings overlay selector
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "USER", "SHELL", "LANG", "TERM")
            or (k.startswith("FIXTURE_") and fixtures_enabled())
        }
        # Harness extras are allowlisted, not trusted (2026-10-07 review): only the
        # Pi runtime directory is a legitimate extra for any built-in harness.
        env.update(
            {k: v for k, v in extra.items() if k in HARNESS_EXTRA_KEYS and v in _pi_dirs(a["provider"], tmp)},
            # Authors' ordinary tempfiles must land in the public /tmp bind,
            # not the private API/runtime directory that collect_evidence excludes.
            TMPDIR="/tmp" if private_scope or claim.get("role_kind") in ("developer", "specialist") else str(tmp),
            NODE_COMPILE_CACHE=str(tmp / "node-compile-cache"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTEST_ADDOPTS="-p no:cacheprovider",
            XDG_CACHE_HOME=str(tmp / "cache"),
            UV_CACHE_DIR=str(tmp / "uv-cache"),
            npm_config_cache=str(tmp / "npm-cache"),
            GIT_OPTIONAL_LOCKS="0",
            GOPROXY="off", GOFLAGS="-mod=mod", GOTOOLCHAIN="local",
            GOMODCACHE=str(GO_MOD_CACHE), GOCACHE=str(tmp / "go-cache"),
        )
        if test_db:
            env["MC_TEST_POSTGRES_URL"] = test_db
        # Local-auth settings so probes importing the app never need an inline KEY=token
        # that makes the whole report fail credential redaction (canary cycle 0000a001,
        # 2026-10-07). The token is random per attempt: it satisfies settings validation
        # but nobody else knows it, so it can authenticate no deployment. Set last, so
        # nothing overrides it; operator credential names never reach a worker.
        env.update(
            AUTH_MODE="local",
            LOCAL_AUTH_TOKEN=secrets.token_hex(),
            BASE_URL="http://localhost:8000",
        )
        for name in ("MC_TOKEN", "DATABASE_URL"):
            env.pop(name, None)
        command = sandbox_command(
            argv,
            cwd,
            tmp,
            writable=_claim_capability(claim) == "write_worktree",
            native_account=(
                Path(extra["PI_CODING_AGENT_DIR"])
                if a["provider"].startswith("openai-codex-")
                else None
            ),
            **({"canonical_pi": True} if a["provider"] == "openai-codex-canonical" else {}),
        )
        if STOP.is_set() or (cancelled is not None and cancelled.is_set()):
            return {  # fence: never start inference for a stopped attempt
                "success": False, "retryable": False,
                "text": f"interrupted: {'runner stopping' if STOP.is_set() else 'claim lost'}; worker not started",
                "usage_observed": False,
                "usage": {"provider": a["provider"], "model": a["model"], "source": "pi_cli", "success": False},
            }
        try:
            with open(raw, "w") as fh:
                if private_scope:
                    os.fchmod(fh.fileno(), 0o600)
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    # 2026-10-07: Pi -p reads a non-TTY stdin as extra prompt and waits for EOF;
                    # an inherited open pipe stalled whole attempts with zero output.
                    stdin=subprocess.DEVNULL,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                    umask=0o077 if private_scope else -1,
                )
                deadline = time.monotonic() + max(1, timeout)
                try:
                    while proc.poll() is None:
                        if (
                            STOP.is_set()
                            or (cancelled is not None and cancelled.is_set())
                            or time.monotonic() >= deadline
                        ):
                            # Named here because the killed CLI's partial output
                            # otherwise parses as "empty model reply" (a model
                            # failure) and is retried as one (2026-10-06 pause).
                            interrupted = (
                                "runner stopping" if STOP.is_set()
                                else "claim lost" if cancelled is not None and cancelled.is_set()
                                else "attempt time limit reached"
                            )
                            os.killpg(proc.pid, signal.SIGTERM)
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                os.killpg(proc.pid, signal.SIGKILL)
                                proc.wait()
                            break
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                    rc = proc.returncode
                finally:
                    # A leader can exit on TERM while a child ignores it. Always
                    # reap the isolated group, including after normal leader exit.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
        except FileNotFoundError:
            raw.write_text("harness CLI missing\n")
    out = subprocess.run(
        [
            sys.executable,
            str(HERE / "mc-agent-result.py"),
            "claude" if a["provider"] == "claude-code" else "pi",
            str(raw),
            a["provider"],
            a["model"],
            str(rc),
            f"mc-team-{a['id']}",
        ],
        capture_output=True,
        text=True,
    )
    try:
        res = json.loads(out.stdout)
    except ValueError:
        res = {
            "text": "result parser failed",
            "success": False,
            "usage": {},
            "usage_observed": False,
        }
    if private_scope and not interrupted and not res.get("usage_observed") and "\"type\":\"agent_start\"" not in raw.read_text():
        # No model request started; deterministic setup refusal is not an empty-model retry.
        return _private_profile_refusal(a)
    if a["provider"] == "openai-codex-canonical" and res.get("success") and not canonical_pi_authorized(0):
        res = {**res, "success": False, "retryable": False,
               "text": "canonical Pi account pin or access window changed; output discarded"}
    if interrupted:  # observed partial usage below is still reported once
        res = {**res, "success": False, "retryable": False,
               "text": f"interrupted: {interrupted}; partial output discarded"}
    if (res.get("success") and claim.get("role_kind") in ("developer", "specialist")
            and res.get("tool_activity")):
        # Retain genuine ordered completed events before scratch is deleted; no
        # arguments/results, new storage system, PM-plan edits or QA certification.
        res["text"] += "\n\n## Host-observed completed tools\n" + json.dumps({
            "session_id": f"mc-team-{a['id']}", "events": res["tool_activity"],
        })
    report = dict(res.get("usage") or {})
    if report:  # what this run loaded, so /models shows it per model (owner 2026-10-26)
        mcp = []
        if built is not None and "--mcp-config" in built[0]:
            try:
                mcp = sorted(json.loads(built[0][built[0].index("--mcp-config") + 1]).get("mcpServers") or {})
            except (ValueError, IndexError, AttributeError):
                mcp = ["unreadable"]
        report["harness"] = {"skills": ([f"ponytail:{PONYTAIL_LEVEL}"] if PONYTAIL_RULES else [])
                                       + [f"superpowers:{name}" for name in skills], "mcp": mcp}
    try:  # keep the ranking board's usage accounting (legacy behaviour)
        api("post", "/api/v1/ranking/usage", report)
    except (ApiError, OSError, subprocess.TimeoutExpired):
        _quiet_log(f"WARN: ranking usage report failed for {a['provider']}/{a['model']}")
    return res


TEST_PG_IMAGE = os.environ.get("RUNNER_TEST_PG_IMAGE", "postgres:16-alpine")
TEST_PG_LABEL = "mc.test-pg.expires"
# One bridge with inter-container traffic disabled: attempts cannot reach each
# other's databases, and per-attempt networks would exhaust Docker's address pools.
TEST_PG_NETWORK = "mc-test-pg-noicc"
TEST_PG_SETUP_SECONDS = 120  # end-to-end cap; never more than a quarter of the attempt
# Runs in the image's init phase, on a socket-only temporary server, so the first
# TCP listener already allows only this attempt's random role on mc_audit.
TEST_PG_INIT = """set -e
psql -v ON_ERROR_STOP=1 --username postgres --dbname mc_audit <<'SQL'
CREATE ROLE {role} LOGIN;
ALTER DATABASE mc_audit OWNER TO {role};
ALTER SCHEMA public OWNER TO {role};
SQL
printf 'local all postgres trust\\nhost mc_audit {role} all trust\\nhost all all all reject\\n' > "$PGDATA/pg_hba.conf"
"""


def _quiet_log(msg: str) -> None:
    try:
        log(msg)
    except OSError:
        pass  # reporting must never replace an attempt's outcome


def _supervised(cmd: list[str], timeout: float, stop, env: dict | None = None) -> tuple[int, str]:
    """Run one setup command, killable at once on `stop()`; (returncode, stdout).

    Returns -1 when stopped or timed out.
    """
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    end = time.monotonic() + timeout
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=0.2)
                return proc.returncode, out or ""
            except subprocess.TimeoutExpired:
                if stop() or time.monotonic() >= end:
                    return -1, ""
    finally:  # the whole group: a child can outlive its leader and hold stdout
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _reap_test_postgres(limit: float = 60, stop=lambda: False) -> None:
    """Remove disposable databases whose runner died before cleaning up.

    Labels carry setup + worker budget + grace, so a live attempt is never expired.
    """
    end = time.monotonic() + limit
    rc, out = _supervised(
        ["docker", "ps", "-a", "--filter", f"label={TEST_PG_LABEL}",
         "--format", "{{.Names}} {{.Label \"" + TEST_PG_LABEL + "\"}}"],
        max(0.5, min(30, end - time.monotonic())), stop,
    )
    for line in out.splitlines() if rc == 0 else []:
        name, _, expires = line.partition(" ")
        if stop() or time.monotonic() >= end:
            return
        if expires.isdigit() and int(expires) < time.time():
            _supervised(["docker", "rm", "-f", "-v", name], max(0.5, min(30, end - time.monotonic())), stop)


@contextlib.contextmanager
def disposable_postgres(
    role_kind: str, cwd: Path, budget: int, cancelled: threading.Event | None = None
):
    """A throwaway, migrated PostgreSQL (database mc_audit) for one attempt.

    2026-10-07 canary 0000a012: publication serialization could not be verified
    without PostgreSQL. Owner decision: one database per developer/QA attempt (the
    *_pg tests truncate tables, so attempts cannot share one); local image only
    (never pulled); a bridge without inter-container traffic; memory/pid limits;
    127.0.0.1 port. Access: pg_hba allows TCP only for this attempt's random,
    non-superuser role on mc_audit (trust, so the URL carries no password); the
    random role name and port are the capability — other roles, including the
    superuser, are rejected. The attempt checkout's migrations run inside the
    worker sandbox, never on the host. Every setup step is a killable process:
    setup stops at once on cancellation or runner shutdown and takes at most a
    quarter of the attempt budget (120 s). The container is removed on every exit
    path the runner survives, never at the cost of the worker's result; an expiry
    label (setup + budget + grace) lets a later attempt reap one left by a killed
    runner. Any failure yields None and the *_pg tests skip.
    Opt-in: RUNNER_TEST_PG=1 in the runner env.
    """
    if (
        os.environ.get("RUNNER_TEST_PG") != "1"
        or role_kind not in ("developer", "specialist", "qa")
        or not shutil.which("docker")
    ):
        yield None
        return
    name = f"mc-test-pg-{secrets.token_hex(6)}"
    role = f"mc_audit_{secrets.token_hex(8)}"
    setup = min(TEST_PG_SETUP_SECONDS, budget // 4)
    deadline = time.monotonic() + setup
    scratch = Path(tempfile.mkdtemp(prefix="mc-test-pg."))

    def stopped() -> bool:
        return STOP.is_set() or (cancelled is not None and cancelled.is_set())

    def step(cmd: list[str], cap: float, env: dict | None = None) -> tuple[int, str]:
        if stopped():
            raise subprocess.SubprocessError("attempt stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.SubprocessError("database setup time exhausted")
        rc, out = _supervised(cmd, min(cap, remaining), stopped, env)
        if stopped():
            raise subprocess.SubprocessError("attempt stopped")
        return rc, out

    def must(cmd: list[str], cap: float) -> str:
        rc, out = step(cmd, cap)
        if rc:
            raise subprocess.SubprocessError(f"{cmd[:3]} exited {rc}")
        return out

    try:
        url = None
        try:
            _reap_test_postgres(max(0.5, min(30, deadline - time.monotonic())), stopped)
            rc, icc = step(["docker", "network", "inspect", "--format",
                            '{{index .Options "com.docker.network.bridge.enable_icc"}}', TEST_PG_NETWORK], 10)
            if rc:
                must(["docker", "network", "create", "-o",
                      "com.docker.network.bridge.enable_icc=false", TEST_PG_NETWORK], 20)
            elif icc.strip() != "false":
                raise subprocess.SubprocessError(f"{TEST_PG_NETWORK} allows inter-container traffic")
            init = scratch / "init.sh"
            init.write_text(TEST_PG_INIT.format(role=role))
            init.chmod(0o644)
            must(["docker", "run", "-d", "--rm", "--pull=never", "--name", name,
                  "--label", f"{TEST_PG_LABEL}={int(time.time()) + setup + budget + 900}",
                  "--network", TEST_PG_NETWORK, "--memory", "1g", "--pids-limit", "256",
                  "-v", f"{init}:/docker-entrypoint-initdb.d/init.sh:ro",
                  # trust is only the image's bootstrap default; init.sh replaces the
                  # whole pg_hba.conf before the server first listens on TCP
                  "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=mc_audit",
                  "-p", "127.0.0.1::5432", TEST_PG_IMAGE], 30)
            port = must(["docker", "port", name, "5432/tcp"], 10).splitlines()[0].rsplit(":", 1)[1]
            while step(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", role,
                        "-d", "mc_audit"], 10)[0]:  # TCP opens only after init
                time.sleep(0.5)
            candidate = f"postgresql+psycopg://{role}@127.0.0.1:{port}/mc_audit"
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TMPDIR": str(scratch),
                "PYTHONDONTWRITEBYTECODE": "1",
                "DATABASE_URL": candidate,
                "AUTH_MODE": "local",
                "BASE_URL": "http://localhost:8000",
            }
            env.update(LOCAL_AUTH_TOKEN=secrets.token_hex())  # random: authenticates nothing
            migrate = sandbox_command(  # attempt-authored code: worker authority only
                ["sh", "-c", "cd backend && exec .venv/bin/python -m alembic upgrade head"],
                cwd, scratch, writable=False,
            )
            for _ in range(3):
                if step(migrate, 180, env)[0] == 0:
                    url = candidate
                    break
                time.sleep(1)
        except (OSError, subprocess.SubprocessError, RuntimeError, IndexError, ValueError):
            url = None
        yield url
    finally:  # covers setup too; a failed removal must never replace the worker's result
        try:
            removed = subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
            if removed.returncode:
                _quiet_log(f"disposable database {name} not removed (docker rm exit {removed.returncode}); "
                           "its expiry label lets a later attempt reap it")
        except (OSError, subprocess.SubprocessError):
            _quiet_log(f"disposable database {name} not removed; its expiry label lets a later attempt reap it")
        shutil.rmtree(scratch, ignore_errors=True)


def run_harness(claim: dict, prompt: str, cwd: Path, tmp: Path, timeout: int,
                cancelled: threading.Event | None = None, budget_ready=None) -> dict:
    try:
        a = claim["attempt"]
        gate = (
            qwen_slot(timeout, lambda: STOP.is_set() or (cancelled is not None and cancelled.is_set()))
            if (a["provider"], a["model"]) == ("vllm", "Qwen3.8-27B-FP8")
            else contextlib.nullcontext(timeout)
        )
        with gate as allowance:
            started = time.monotonic()
            # private-board commands have no network; do not launch a DB they cannot use.
            database = (contextlib.nullcontext(None)
                        if BOARD in PRIVATE_BOARDS or claim.get("board_id") in PRIVATE_BOARDS
                        else disposable_postgres(claim["role_kind"], cwd, int(allowance), cancelled))
            with database as test_db:
                spent = int(time.monotonic() - started)  # setup/slot waiting use the existing budget
                remaining = int(allowance) - spent
                stop = (
                    "runner stopping" if STOP.is_set()
                    else "claim lost" if cancelled is not None and cancelled.is_set()
                    else "attempt time limit reached" if remaining <= 0 or (spent and remaining < 30)
                    else "task budget/lifecycle gate refused execution"
                    if budget_ready is not None and not budget_ready()
                    else ""
                )
                if stop:  # fence: never start inference for a stopped or exhausted attempt
                    return {"success": False, "retryable": False,
                            "text": f"interrupted: {stop}; worker not started", "usage_observed": False,
                            "usage": {"provider": a["provider"], "model": a["model"],
                                      "source": "pi_cli", "success": False}}
                return _run_harness(claim, prompt, cwd, tmp, remaining, cancelled, test_db)
    except Exception as exc:
        a = claim["attempt"]
        # Setup failures have no observed token usage. Never expose configuration
        # contents in diagnostics or bypass the sandbox to recover.
        result = {
            "success": False,
            "text": f"harness runtime setup failed ({type(exc).__name__})",
            "usage_observed": False,
            "usage": {"provider": a["provider"], "model": a["model"],
                      "source": "pi_cli", "success": False},
        }
        try:
            api("post", "/api/v1/ranking/usage", result["usage"])
        except (ApiError, OSError, subprocess.TimeoutExpired):
            _quiet_log(f"WARN: failed to report runtime failure for {a['provider']}/{a['model']}")
        return result


def source_identifiers(source: str, path: str) -> set[tuple[int, int]]:
    if path.endswith(".py"):
        return python_identifiers(source)
    if not path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        return set()
    # Existing TypeScript parser: no home-grown JS comment/template lexer.
    program = r'''
const ts = require(require.resolve("typescript", {paths: [process.argv[1]]}));
const text = require("fs").readFileSync(0, "utf8");
const file = ts.createSourceFile(process.argv[2], text, ts.ScriptTarget.Latest, true);
const names = [];
let malformed = false;
function visit(node) {
  if (ts.isVariableDeclarationList(node) && node.declarations.hasTrailingComma) malformed = true;
  if (ts.isIdentifier(node) || (ts.isStringLiteral(node) &&
      (ts.isPropertyAssignment(node.parent) || ts.isPropertySignature(node.parent)) &&
      node.parent.name === node)) {
    const start = node.getStart(file) + (ts.isStringLiteral(node) ? 1 : 0);
    const prefix = text.slice(0, start);
    // Git hunks count LF only, unlike TypeScript's Unicode line separators.
    // ponytail: prefix scans; cache LF/column maps if minified lines matter.
    names.push([(prefix.match(/\n/g) || []).length + 1,
                [...prefix.slice(prefix.lastIndexOf("\n") + 1)].length]);
  }
  ts.forEachChild(node, visit);
}
// A trailing callback-property comma is lexically clear even without its container.
// Other parse uncertainty (including unfinished strings/templates/comments) refuses refs.
const tail = text.trimEnd();
const last = file.statements[file.statements.length - 1];
const expr = last && ts.isLabeledStatement(last) && ts.isExpressionStatement(last.statement)
             ? last.statement.expression : null;
const callback = expr && ts.isBinaryExpression(expr) &&
    expr.operatorToken.kind === ts.SyntaxKind.CommaToken && ts.isCallExpression(expr.left) &&
    ts.isIdentifier(expr.right) && expr.right.getWidth(file) === 0;
if (file.parseDiagnostics.length === 0 ||
    (callback && file.parseDiagnostics.every(
      d => d.code === 1109 && d.length === 0 && d.start === tail.length && tail.endsWith(",")))) visit(file);
process.stdout.write(JSON.stringify(malformed ? [] : names));
'''
    try:
        result = subprocess.run(
            ["node", "-e", program, str(HERE.parent / "frontend"), path], input=source,
            capture_output=True, text=True, timeout=10,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        return {tuple(p) for p in json.loads(result.stdout)} if result.returncode == 0 else set()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()  # absent/invalid parser context cannot authorize exemptions


def qa_evidence(tmp: Path, base: str, sha: str, *, cwd: Path | None = None) -> str | None:
    """Host-prepared, credential-free, bounded Git evidence for the QA worker.

    The QA sandbox has no host Git database access, so this supervisor — the
    only component allowed to run Git — writes changed-path/base/integration
    SHA metadata, per-file hashes and a bounded sanitized diff into the attempt
    scratch. Returns the note appended to the QA brief, or None when required
    changed bytes cannot be inspected (callers must fail the attempt then,
    never fabricate a reviewable diff or a pass).
    """
    cwd = cwd or REPO
    # Private aliases can shadow required public context, including unchanged
    # lines or no-change verification. Do not ask a reviewer to guess it.
    for private in _private_source_paths(cwd):
        try:
            target = private.resolve().relative_to(cwd.resolve())
        except ValueError:
            continue  # outside-worktree targets are absent or private runtime
        except (OSError, RuntimeError):
            return None
        if not _private_source_path(target.as_posix()):
            return None
    # Destination-only names lose rename/copy provenance. Check BOTH names
    # before writing any evidence; a new alias cannot unmask private bytes.
    # ponytail: Git copy search scales with repo size; use explicit host lineage
    # if this bounded MC repo outgrows the native rename/copy detector.
    names = iter(git(REPO, "diff", "--name-status", "-z", "--find-renames", "--find-copies-harder", base, sha).split("\0"))
    try:
        for status in names:
            if not status:
                continue
            paths = [next(names)]
            if status.startswith(("R", "C")):
                paths.append(next(names))
            if any(_private_source_path(p) for p in paths):
                return None
    except StopIteration:
        return None  # malformed/truncated provenance is not reviewable
    changed = [
        p for p in git(REPO, "diff", "--name-only", "-z", base, sha, "--no-renames").split("\0") if p
    ]
    if any(_private_source_path(p) for p in changed):
        return None
    if len(changed) > 512:
        return None
    files = []
    for path in changed:
        size = git(REPO, "cat-file", "-s", f"{sha}:{path}", check=False)
        if not size.isdigit():
            size = git(REPO, "cat-file", "-s", f"{base}:{path}", check=False)
            if not size.isdigit():
                return None  # changed bytes unreadable: fail, never guess
            if int(size) > QA_EVIDENCE_BLOB_LIMIT:
                return None
            files.append({"path": path, "deleted": True, "bytes": int(size)})
            continue
        entry: dict = {"path": path, "bytes": int(size)}
        if int(size) > QA_EVIDENCE_BLOB_LIMIT:
            return None
        link = cwd / path
        if link.is_symlink():  # its blob is the link text; the probe compares that, not the target
            try:
                if _private_source_path(link.resolve().relative_to(cwd.resolve()).as_posix()):
                    return None
            except (ValueError, OSError, RuntimeError):
                return None  # a link out of the worktree is not reviewable here
        if int(size) <= QA_EVIDENCE_BLOB_LIMIT:
            blob = subprocess.run(
                ["git", "-C", str(REPO), "cat-file", "blob", f"{sha}:{path}"],
                capture_output=True,
                check=True,
            ).stdout
            entry["sha256"] = hashlib.sha256(blob).hexdigest()
        files.append(entry)
    diff_bytes = git(REPO, "diff", base, sha, "--no-ext-diff", "--no-textconv", "--no-color", raw=True)
    if isinstance(diff_bytes, str):  # old fake-Git fixture outputs
        diff_bytes = diff_bytes.encode()
    if len(diff_bytes) > QA_EVIDENCE_LIMIT:
        return None  # partial original evidence cannot support exact-SHA QA
    try:
        diff = diff_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None  # no replacement characters masquerading as original bytes
    if "\0" in diff or re.search(r"^(?:Binary files |GIT binary patch$)", diff, re.M):
        return None  # summaries/opaque patches are not inspectable original bytes
    commits = git(REPO, "log", "--format=%H %s", f"{base}..{sha}").splitlines()
    if len(commits) > 64:
        return None
    # Credential values still refuse QA; counters/references are not secrets.
    # Original hashes and unredacted patch bytes remain exact.
    contexts = {}
    source_redacted = False
    for path in changed:
        for version, revision in (("-", base), ("+", sha)):
            exists = git(REPO, "ls-tree", "--name-only", "-z", revision, "--", path)
            if not exists:
                continue  # actual added/deleted path, not an unreadable existing blob
            size = git(REPO, "cat-file", "-s", f"{revision}:{path}", check=False)
            if not size.isdigit() or int(size) > QA_EVIDENCE_BLOB_LIMIT:
                return None
            raw = git(REPO, "cat-file", "blob", f"{revision}:{path}", check=False, raw=True)
            if len(raw) != int(size) or (b"\0" if isinstance(raw, bytes) else "\0") in raw:
                return None
            try:
                source = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                contexts[(version, path)] = source_identifiers(source, path)
                # Rename/copy-only diffs still expose the complete changed file.
                patch = (
                    f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,0 @@\n"
                    + "".join("+" + line + "\n" for line in source.split("\n"))
                )
                source_redacted |= redact_diff(
                    patch, contexts={("+", path): contexts[(version, path)]}
                ) != patch
            except UnicodeDecodeError:
                return None
    safe_diff = redact_diff(diff, contexts=contexts)
    safe_commits = [redact_evidence(c) for c in commits]
    redacted = source_redacted or safe_diff != diff or safe_commits != commits
    meta = {
        "base_sha": base,
        "integration_sha": sha,
        "commits": safe_commits,
        "changed_files": files,
        "diff_truncated": False,
        "evidence_redacted": redacted,
    }
    # Owner 2026-10-21: credential-shaped lines never block QA; the QA worker
    # already reads these files in its read-only worktree. Flag them only.
    (tmp / "qa-evidence.json").write_text(json.dumps(meta, indent=1))
    (tmp / "qa-changes.diff").write_bytes(diff_bytes)
    # Hash the actual projected bytes, not merely host-readable Git blobs.
    # No providers or credentials: this probe only emits an exit status. A symlink's
    # Git blob is its target text (0000a004, 2026-10-26: a root Dockerfile -> build/Dockerfile
    # link refused QA in every cycle as "cannot inspect complete required changed bytes").
    expected = {f["path"]: f["sha256"] for f in files if not f.get("deleted")}
    probe = (
        "import hashlib,json,os,sys\nfrom pathlib import Path\n"
        "try:\n"
        " for p,h in json.loads(sys.argv[1]).items():\n"
        "  b=os.fsencode(os.readlink(p)) if os.path.islink(p) else Path(p).read_bytes()\n"
        "  if hashlib.sha256(b).hexdigest()!=h: sys.exit(1)\n"
        "except OSError: sys.exit(1)\n"
    )
    command = sandbox_command([sys.executable, "-c", probe, json.dumps(expected)], cwd, tmp, writable=False)
    if subprocess.run(command, capture_output=True).returncode:
        return None
    return (
        "\n\nThe supervisor prepared the Git evidence for this review (this "
        "sandbox has no Git database access): metadata at "
        f"{tmp}/qa-evidence.json and the bounded diff at {tmp}/qa-changes.diff; "
        "every changed file is readable in this read-only worktree."
    )


def checkout_note(cwd: Path, base: str, writable: bool) -> str:
    # Repository rules such as "Inspect `git status` before editing" made workers stop
    # with no changes when Git failed in the sandbox (3 of 4 blocks on 2026-10-24).
    return (
        f"\n\nCheckout status, supplied by the supervisor: {cwd} is a fresh worktree "
        f"created for this attempt at exact base {base}; it has no uncommitted or "
        "unrelated changes. This sandbox has no Git database by design, so `git status`, "
        "`git diff` and `git log` fail with 'not a git repository'. That is expected, not "
        "a blocker: this note is the pre-edit status check repository rules ask for. "
        "Do not run Git. "
        + ("Edit the files directly; after you exit, the host commits every changed file."
           if writable else "This checkout is read-only; change nothing.")
    )


def scope_of(path: str, scope: list[str]) -> bool:
    return any(
        path == s.rstrip("/") or path.startswith(s.rstrip("/") + "/") for s in scope
    )


def dirty(cwd: Path) -> list[str]:
    entries = iter(git(cwd, "status", "--porcelain", "-z", "-uall").split("\0"))
    paths = []
    for entry in entries:
        if not entry:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            next(entries, None)  # -z reports destination first, then old path
    return paths


class Cycle:
    def __init__(self, task_id: str, task: dict) -> None:
        self.task_id, self.task = task_id, task
        self.base = f"/api/v1/boards/{BOARD}/tasks/{task_id}/team"
        self.cycle: dict = {}
        self.cleanup: list[tuple[Path, str | None]] = []
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.refreshes = 0  # bounded production-move integrations per cycle

    def view(self) -> tuple[dict, list[dict]]:
        v = api("get", self.base)["cycles"][0]
        self.cycle = v["cycle"]
        return v["cycle"], v["attempts"]

    def budget_ready(self) -> bool:
        return check_task_budget(api, BOARD, self.task_id, self.task) is not None

    def remaining(self, claim: dict) -> int:
        # The cycle deadline is extendable while a worker is live. Bound the attempt from its
        # own accepted-claim time; backend heartbeats enforce the absolute cycle lifetime.
        return max(0, min(TOTAL_TIMEOUT, int(claim["_runner_deadline"] - time.monotonic())))

    def _harness(self, claim: dict, prompt: str, cwd: Path, tmp: Path,
                 cancelled: threading.Event, budget_ready=None) -> dict:
        timeout = self.remaining(claim)
        if timeout < 30:
            return {"success": False, "retryable": False, "text": "no time left in the cycle"}
        return run_harness(claim, prompt, cwd, tmp, timeout, cancelled, budget_ready)

    def comment(self, text: str) -> None:
        try:
            api(
                "post",
                f"/api/v1/boards/{BOARD}/tasks/{self.task_id}/comments",
                {"message": text},
            )
        except ApiError:
            log("WARN: could not post comment")

    # ── one fenced attempt ──────────────────────────────────────────────────
    def attempt(self, claim: dict) -> None:
        a, lease = claim["attempt"], claim["claim_token"]
        claim = {**claim, "_runner_deadline": claim.get(
            "_runner_deadline", time.monotonic() + TOTAL_TIMEOUT
        )}
        path = f"{self.base}/cycles/{self.cycle['id']}/attempts/{a['id']}"
        tmp = Path(tempfile.mkdtemp(prefix="mc-team-attempt."))
        stop = threading.Event()
        cancelled = threading.Event()

        def beat() -> None:
            while not stop.wait(10):
                try:
                    api("post", path + "/heartbeat", dict(claim_token=lease))
                except ApiError:
                    cancelled.set()  # stop the entire process group, not only its result
                    return

        threading.Thread(target=beat, daemon=True).start()
        body: dict = dict(ok=False, error="runner error", claim_token=lease)
        try:
            evidence_dir(tmp).mkdir()
            body = self._execute(claim, tmp, cancelled)
            if claim["role_kind"] in ("developer", "specialist") and body.get("ok"):
                body["evidence_files"] = collect_evidence(
                    evidence_dir(tmp), repo_blobs(a.get("base_sha")), body.get("artifact") or ""
                )
        except Exception as exc:  # a worker crash must still release its claim
            body = dict(
                ok=False,
                error=f"runner error: {str(exc)[:300]}",
                claim_token=lease,
            )
        finally:
            stop.set()
            try:
                api("post", path + "/complete", dict(body, claim_token=lease))
            except Exception as exc:  # any completion failure: the claim lease expires
                log(f"result of {a['stage_key']} discarded: {exc}")
            finally:  # scratch and evidence go even if completion is interrupted
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.rmtree(evidence_dir(tmp), ignore_errors=True)

    def _execute(self, claim: dict, tmp: Path, cancelled: threading.Event) -> dict:
        if not self.budget_ready():
            return {"ok": False, "error": "task budget/lifecycle gate refused execution"}
        # Before Git archive/worktree creation, prompt construction or inference.
        refuse_local_only_projection(REPO, Path(WORKDIR), tmp, evidence_dir(tmp))
        claim = {**claim, "board_id": BOARD}  # trusted controller scope, never prompt instructions
        a, kind = claim["attempt"], claim["role_kind"]
        timeout = self.remaining(claim)
        if timeout < 30:
            return {"ok": False, "error": "no time left in the cycle"}
        short = f"{self.task_id[:8]}-{a['id'][:8]}"
        env_id = identity(a.get("member_name") or kind)
        if kind == "pm":
            snap = tmp / "snapshot"
            snap.mkdir()
            archive = subprocess.check_output(
                ["git", "-C", str(REPO), "archive", a["base_sha"]]
            )
            subprocess.run(["tar", "-x", "-C", str(snap)], input=archive, check=True)
            brief = claim["brief"] + (CI_NOTE if REPO != MC_REPO else "")
            res = self._harness(claim, brief, snap, tmp, cancelled, self.budget_ready)
            return self._result(res, artifact=res.get("text", ""))
        if kind == "qa":
            stage_evidence_files(evidence_dir(tmp), claim.get("evidence_files") or {})
            sha = self.cycle["integration_sha"]
            wt = Path(tempfile.mkdtemp(prefix=f"{WORKDIR}-team-qa."))
            with GIT_LOCK:
                git(REPO, "worktree", "add", "--detach", "-f", str(wt), sha)
            self.cleanup.append((wt, None))
            # QA reviews the exact integration commit, but never gets host Git
            # access: the supervisor hands over credential-free, bounded evidence
            # (changed paths, base/integration SHAs, file hashes, sanitized
            # diff). If required changed bytes cannot be inspected, fail —
            # never fabricate a reviewable diff or a PASS.
            note = qa_evidence(tmp, self.cycle["base_sha"], sha, cwd=wt)
            if note is None:
                return {
                    "ok": False,
                    "error": (
                        "QA cannot inspect complete required changed bytes "
                        "(private, redacted or oversized evidence); owner review required, "
                        "no verdict was fabricated"
                    ),
                }
            res = self._harness(claim, claim["brief"] + note, wt, tmp, cancelled, self.budget_ready)
            text = res.get("text", "")
            head, extra = git(wt, "rev-parse", "HEAD"), dirty(wt)
            verdicts = re.findall(r"^\s*VERDICT:\s*(PASS|FAIL)\s*$", text, re.M | re.I)
            out = self._result(res, artifact=text, head_sha=head)
            if out["ok"] and extra:
                return {
                    **out,
                    "ok": False,
                    "error": f"QA modified the read-only checkout: {extra[:5]}",
                }
            if out["ok"] and not verdicts:
                return {**out, "ok": False, "error": "QA gave no VERDICT line"}
            if verdicts:
                out["verdict"] = verdicts[-1].lower()
            return out
        if _claim_capability(claim) != "write_worktree":
            # verify attempt: genuinely read-only worktree at the exact original
            # base (host Git). Nothing is staged or committed — evidence only.
            wt = Path(tempfile.mkdtemp(prefix=f"{WORKDIR}-team-verify."))
            with GIT_LOCK:
                git(REPO, "worktree", "add", "--detach", "-f", str(wt), a["base_sha"])
            self.cleanup.append((wt, None))
            note = checkout_note(wt, a["base_sha"], writable=False)
            res = self._harness(claim, claim["brief"] + note, wt, tmp, cancelled, self.budget_ready)
            out = self._result(res, artifact=res.get("text", ""))
            if not out["ok"]:
                return out
            head, extra = git(wt, "rev-parse", "HEAD"), dirty(wt)
            if extra:
                return {
                    **out,
                    "ok": False,
                    "error": f"verify worker modified the read-only checkout: {extra[:5]}",
                }
            if head != git(wt, "rev-parse", a["base_sha"]):
                return {
                    **out,
                    "ok": False,
                    "error": "verify worker moved the worktree HEAD; no host Git allowed",
                }
            if not (res.get("text") or "").strip():
                return {
                    **out,
                    "ok": False,
                    "error": "verify worker produced no substantive evidence",
                }
            return {**out, "head_sha": head, "changed_files": []}
        # developer / specialist: own worktree + branch, scoped commit only
        wt, branch = (
            Path(tempfile.mkdtemp(prefix=f"{WORKDIR}-team-dev.")),
            f"team/{short}",
        )
        with GIT_LOCK:
            git(REPO, "worktree", "add", "-b", branch, "-f", str(wt), a["base_sha"])
        self.cleanup.append((wt, branch))
        git_link = (wt / ".git").read_bytes()
        note = checkout_note(wt, a["base_sha"], writable=True)
        res = self._harness(claim, claim["brief"] + note, wt, tmp, cancelled, self.budget_ready)
        out = self._result(res, artifact=res.get("text", ""))
        if not out["ok"]:
            return out
        pointer = wt / ".git"
        if pointer.is_symlink() or not pointer.is_file() or pointer.read_bytes() != git_link:
            return {**out, "ok": False, "error": "worker changed Git metadata; no host Git operation permitted"}
        scope = claim["scope"]
        changed = dirty(wt)
        stray = [f for f in changed if not scope_of(f, scope)]
        if stray:  # scope is guidance; overlapping leaves still surface as integration conflicts
            out["artifact"] = (out.get("artifact") or "") + f"\n\nAlso edited outside the assigned scope: {stray[:20]}"
        if changed:  # stage only the changed paths already checked against scope
            git(wt, "add", "--", *changed)
            git(
                wt,
                "commit",
                "-q",
                "-m",
                f"team: {a['stage_key']} ({a['provider']}/{a['model']})",
                env=env_id,
            )
        head = git(wt, "rev-parse", "HEAD")
        files = [
            p
            for p in git(wt, "diff", "--name-only", "-z", a["base_sha"], head).split(
                "\0"
            )
            if p
        ]
        return {**out, "head_sha": head, "changed_files": files}

    @staticmethod
    def _result(res: dict, **extra) -> dict:
        u = res.get("usage") or {}
        usage = (
            {
                k: u.get(k)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "total_tokens",
                    "cost_usd",
                )
            }
            if res.get("usage_observed")
            else None
        )
        return {
            "ok": bool(res.get("success")),
            "tool_counts": res.get("tool_counts"),
            "error": "" if res.get("success") else (res.get("text") or "failed"),
            "usage": usage,
            **extra,
        }

    def shutdown(self) -> None:
        try:
            current, _ = self.view()
            if current["state"] == "publishing":
                api("post", f"{self.base}/cycles/{current['id']}/publication/fail", {"version": current["version"], "reason": "supervisor stopped during publication; inspect production/deployment"})
            elif current["state"] in ("planning", "developing", "qa", "qa_passed"):
                api("post", f"{self.base}/cycles/{current['id']}/cancel", {})
        except ApiError:
            log("shutdown fence unavailable; expired leases/reservations require recovery")

    # ── orchestration loop ──────────────────────────────────────────────────
    def drive(self) -> None:
        initial, _ = self.view()
        base = initial.get("base_sha") or git(REPO, "rev-parse", "HEAD")
        # A slot is refilled as soon as any attempt finishes. Waiting for the whole batch
        # left 8 stages of one cycle idle behind a single hour-long attempt (2026-10-24).
        # The executor drains accepted work on every exit.
        inflight: set = set()
        idle = 0
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            while idle < 60:
                if STOP.is_set():
                    return
                cycle, attempts = self.view()
                state = cycle["state"]
                if state not in ("planning", "developing", "qa", "qa_passed"):
                    return
                if not self.budget_ready():
                    return
                if state == "qa_passed" and not inflight:
                    self.publish(cycle)
                    continue
                limit = PARALLEL if state == "developing" else 1
                claimed = False
                while len(inflight) < limit and time.monotonic() < self.deadline and self.budget_ready():
                    got = api(
                        "post",
                        f"{self.base}/cycles/{cycle['id']}/claims",
                        {"base_sha": base, "independent": REPO != MC_REPO},
                    )
                    if not got:
                        break
                    # Attempt time starts at acceptance; setup consumes the same bounded budget.
                    if isinstance(got, dict):  # production API claim; fixtures may use sentinels
                        got["_runner_deadline"] = time.monotonic() + TOTAL_TIMEOUT
                    # Supervise each accepted lease immediately: a later claim can wait or fail.
                    inflight.add(pool.submit(self.attempt, got))
                    claimed = True
                if inflight:
                    # Re-try claims every 30s too: another cycle may free an engine slot.
                    done, inflight = wait(inflight, timeout=30, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                    continue
                if claimed:
                    continue
                if state == "developing" and self.integrate(cycle, attempts):
                    continue
                if time.monotonic() > self.deadline:
                    log(f"cycle {cycle['id']} ({state}): runner time budget used; leaving it to the next runner")
                    return
                idle += 1
                time.sleep(1)
        log(f"cycle {cycle['id']} ({state}): drive loop reached its 60-iteration limit (e.g. waiting on a scope another cycle holds); leaving it to the next runner")

    def integrate(self, cycle: dict, attempts: list[dict]) -> bool:
        """Merge this round's developer branches, but only once the round is truly done.

        Returns False (keep polling) whenever work is still queued or running —
        a claim can come back empty merely because provider slots are reserved by
        a sibling cycle, or because management (pm) stages have not finished
        producing the leaf jobs yet. Integrating then would create an empty or
        partial integration. We integrate only when every leaf developer/
        specialist stage key succeeded and nothing in the round is still pending.
        """
        rnd = cycle["round"]
        # Retries clone an attempt with the same stage_key, so only the newest
        # attempt of each stage counts; a stale failed clone must not block us.
        latest: dict[str, dict] = {}
        for a in sorted(
            (a for a in attempts if a["round"] == rnd), key=lambda a: a["attempt_no"]
        ):
            latest[a["stage_key"]] = a
        if any(a["status"] in ("queued", "running") for a in latest.values()):
            return False  # slots busy or still in flight; never integrate early
        # The PM stage precedes the leaf jobs; integration
        # waits for the whole round, then needs every leaf stage succeeded.
        leaf = [
            a for a in latest.values() if a["role_kind"] in ("developer", "specialist")
        ]
        if not leaf:
            return False  # management stages have not queued leaf work yet
        if any(a["status"] != "succeeded" for a in leaf):
            return False  # a leaf did not succeed; backend requeues or blocks it
        devs = {a["stage_key"]: a for a in leaf}
        wt = Path(tempfile.mkdtemp(prefix=f"{WORKDIR}-team-int."))
        with GIT_LOCK:
            git(REPO, "worktree", "add", "--detach", "-f", str(wt), cycle["base_sha"])
        self.cleanup.append((wt, None))
        try:
            for key in sorted(devs):
                git(
                    wt,
                    "merge",
                    "--no-ff",
                    "-q",
                    "-m",
                    f"team: integrate {key}",
                    devs[key]["head_sha"],
                    env=identity("coordinator"),
                )
        except RuntimeError as exc:
            conflicts = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
            paths = ", ".join(p[:160] for p in conflicts[:20]) or "paths unavailable"
            evidence = f"stage=leaf-integration base={cycle['base_sha'][:12]} conflicts={paths}"
            git(wt, "merge", "--abort", check=False)
            api("post", f"{self.base}/cycles/{cycle['id']}/block", {"version": cycle["version"], "reason": f"leaf-integration conflict; owner resolution required; {evidence}"})
            self.comment(
                f"**team-runner**: leaf integration conflict, cycle blocked; nothing published. {evidence}. {exc}"
            )
            return True
        api(
            "post",
            f"{self.base}/cycles/{cycle['id']}/integrate",
            {"integration_sha": git(wt, "rev-parse", "HEAD")},
        )
        return True

    @staticmethod
    def _is_ancestor(ancestor: str, descendant: str) -> bool:
        return is_ancestor(ancestor, descendant)

    @staticmethod
    def _ff(sha: str) -> int:
        return subprocess.run(
            ["git", "-C", str(REPO), "merge", "--ff-only", "-q", sha],
            capture_output=True,
        ).returncode

    def publish(self, cycle: dict) -> None:
        # The whole reserve + head checks + git integration + complete + deploy
        # runs under the legacy global lock. While we hold it the deployment
        # checkout cannot move, so the checks below stay true through completion.
        with global_lock(max(self.deadline, time.monotonic() + 600)):  # an old runner may still publish
            self._publish_locked(cycle)

    def _publish_locked(self, cycle: dict) -> None:
        if not self.budget_ready():
            return
        sha, ver = cycle["reviewed_sha"], cycle["version"]
        pub = f"{self.base}/cycles/{cycle['id']}/publication"
        pre = git(REPO, "rev-parse", "HEAD")
        if git(REPO, "status", "--porcelain"):
            try:  # record the refusal on the cycle; never publish over operator dirt
                api("post", pub + "/reserve", {"sha": sha, "version": ver})
                api(
                    "post",
                    pub + "/fail",
                    {
                        "version": ver + 1,
                        "reason": "deployment checkout has uncommitted changes",
                    },
                )
            except ApiError as exc:
                log(f"publication aborted (dirty checkout): {exc}")
            return
        # Only MC deploys onto HEAD; PR repos push the exact reviewed SHA to a
        # separate branch. Their shared staging HEAD can contain unrelated PRs.
        # A moved MC deployment still needs a merged tree and a SECOND independent QA.
        if REPO == MC_REPO and not (sha == pre or self._is_ancestor(pre, sha)):
            self.refresh_integration(cycle, pre, sha)
            return
        try:
            api("post", pub + "/reserve", {"sha": sha, "version": ver})
        except ApiError as exc:
            log(f"publication not reserved: {exc}")
            time.sleep(1)
            self.comment(
                # Losing the reservation says nothing about whether the cycle was
                # published by another runner; speak only for this one.
                f"**team-runner**: publication not reserved by this runner ({exc}); "
                "this runner did not publish."
            )
            return
        ver += 1
        why = ""
        if git(REPO, "status", "--porcelain"):
            why = "deployment checkout has uncommitted changes"
        elif REPO == MC_REPO and self._ff(sha) != 0:  # other repos publish as PRs only
            why = "fast-forward failed"
        if why:
            api("post", pub + "/fail", {"version": ver, "reason": why})
            return
        api("post", pub + "/complete", {"sha": sha, "version": ver})
        if sha == pre:
            # Evidence-only verification (all-no-change cycle): the reviewed
            # commit IS the current production HEAD. Record that truthfully and
            # deploy nothing — there is no deployment diff to apply.
            self.comment(
                f"**team-runner**: published the QA-verified commit {sha[:10]} with NO "
                "code change — it is the current production HEAD. Git HEAD is unchanged "
                "and no service was deployed."
            )
        elif DEPLOY:
            self.deploy(pre)
        elif REPO != MC_REPO:
            self.open_pr(sha)
        self._clean_worktrees()

    def _qa_followups(self) -> str:
        """Edge cases a correction-round QA PASS left to the human reviewer (team_cycle brief)."""
        try:
            attempts = self.view()[1]
        except ApiError:
            return ""
        passed = sorted((a for a in attempts if a.get("stage_key") == "qa" and a.get("verdict") == "pass"),
                        key=lambda a: (a.get("round") or 0, a.get("attempt_no") or 0))
        found = re.search(r"(?ms)^\W*Follow-ups:?\W*$(.+?)(?=^\W*VERDICT:|\Z)",
                          passed[-1].get("artifact") or "") if passed else None
        return found.group(1).strip()[:4000] if found else ""

    def open_pr(self, sha: str) -> None:
        """Non-MC repo: publication is a pushed branch plus a PR; nothing merges upstream."""
        stacked = [r for r in dependency_branches(self.task) if contains(r, sha)]
        branch = f"omc/{self.task_id[:8]}-{sha[:8]}"
        default = git(REPO, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).removeprefix("origin/") or "main"
        issues = re.findall(r"(?im)^GitHub issues?:\s*(#\d+(?:[\s,]+#\d+)*)", self.task.get("description") or "")
        numbers = sorted({int(n) for line in issues for n in re.findall(r"#(\d+)", line)})
        follow = self._qa_followups()
        body = (f"Automated by the OMC fleet from board task {self.task_id} "
                f"(QA-passed commit {sha[:10]}).\n\n" + "".join(f"Closes #{n}\n" for n in numbers)
                + ("\nBuilds on the unmerged fleet branch(es) " + ", ".join(f"`{r.removeprefix('origin/')}`" for r in stacked)
                   + "; merge those first, then this diff shrinks to this task's own change.\n" if stacked else "")
                + (f"\n## QA follow-ups (non-blocking)\n{follow}\n" if follow else ""))
        try:
            try:
                git(REPO, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
            except RuntimeError as exc:
                if "`workflow` scope" not in str(exc):
                    raise
                # Owner 2026-10-28: the gh token lacks the `workflow` scope, so CI edits leave
                # the PR and ride along as a diff for a human to add. A scoped token skips this.
                head, dropped = without_workflows(sha)
                git(REPO, "push", "-q", "origin", f"{head}:refs/heads/{branch}")
                body += ("\n## CI workflow changes to add by hand\nThe fleet's GitHub token cannot push "
                         f"`{WORKFLOWS}` changes, so this branch leaves out these QA-reviewed edits "
                         f"of {sha[:10]}:\n\n````diff\n{dropped[:50000]}\n````\n")
            url = subprocess.run(
                ["gh", "pr", "create", "--head", branch, "--base", default,
                 "--title", (self.task.get("title") or branch)[:200], "--body", body],
                cwd=REPO, capture_output=True, text=True, timeout=120, check=True).stdout.strip()
            git(REPO, "update-ref", "-d", f"refs/omc-unpushed/{branch}", check=False)
            self.comment(f"**team-runner**: published as pull request {url} (branch {branch}).")
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            # Keep the commit reachable (gc) for a later push; the pool does not accept the task without its PR.
            git(REPO, "update-ref", f"refs/omc-unpushed/{branch}", sha, check=False)
            self.comment(f"**team-runner**: QA-passed commit {sha[:10]} is committed locally, but opening the PR failed: {str(exc)[:300]}")

    def refresh_integration(self, cycle: dict, pre: str, sha: str) -> None:
        """Production moved: merge the reviewed commit onto current HEAD, then
        requeue independent QA on that new exact commit. No rebase, no publish of
        an unreviewed resolution, and production is never mutated here."""
        if self.refreshes >= MAX_REFRESHES:
            api("post", f"{self.base}/cycles/{cycle['id']}/block", {"version": cycle["version"], "reason": "integration refresh attempts exhausted; owner resolution required"})
            self.comment(
                "**team-runner**: production kept advancing past the refresh attempt limit "
                f"({MAX_REFRESHES}); cycle blocked, nothing published or deployed."
            )
            return
        self.refreshes += 1
        wt = Path(tempfile.mkdtemp(prefix=f"{WORKDIR}-team-refresh."))
        with GIT_LOCK:
            git(REPO, "worktree", "add", "--detach", "-f", str(wt), pre)
        self.cleanup.append((wt, None))
        try:
            git(
                wt,
                "merge",
                "--no-ff",
                "-q",
                "-m",
                f"team: refresh integration of {sha[:10]} onto {pre[:10]}",
                sha,
                env=identity("coordinator"),
            )
        except RuntimeError as exc:
            conflicts = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
            paths = ", ".join(p[:160] for p in conflicts[:20]) or "paths unavailable"
            evidence = f"stage=production-refresh base={pre[:12]} reviewed={sha[:12]} conflicts={paths}"
            git(wt, "merge", "--abort", check=False)
            api("post", f"{self.base}/cycles/{cycle['id']}/block", {"version": cycle["version"], "reason": f"production-refresh conflict; owner resolution required; {evidence}"})
            self.comment(
                "**team-runner**: production advanced and the QA-reviewed commit no "
                f"longer merges cleanly; {evidence}. ({exc}); cycle blocked. Nothing published "
                "or deployed; no unreviewed conflict resolution was applied."
            )
            return
        new_sha = git(wt, "rev-parse", "HEAD")
        try:
            api(
                "post",
                f"{self.base}/cycles/{cycle['id']}/refresh-integration",
                {
                    "integration_sha": new_sha,
                    "base_sha": pre,
                    "version": cycle["version"],
                },
            )
        except ApiError as exc:
            self.comment(
                "**team-runner**: production moved; could not queue a second "
                f"independent QA on the re-merged commit ({exc}); this runner did not publish."
            )
            return
        self.comment(
            f"**team-runner**: production advanced to {pre[:10]}; merged the "
            f"QA-reviewed commit {sha[:10]} onto it as {new_sha[:10]} in an isolated "
            "checkout. A SECOND independent QA must pass on that exact commit before "
            "publication; production was not touched."
        )

    def _clean_worktrees(self) -> None:
        for wt, branch in self.cleanup:
            subprocess.run(
                ["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )
            if branch:
                subprocess.run(
                    ["git", "-C", str(REPO), "branch", "-q", "-D", branch],
                    capture_output=True,
                )
        self.cleanup.clear()

    def deploy(self, pre: str) -> None:
        changed = git(REPO, "diff", "--name-only", pre, "HEAD").splitlines()
        targets = []
        if any(c.startswith("backend/") or c == "compose.yml" for c in changed):
            targets += ["backend", "webhook-worker"]
        if any(c.startswith("frontend/") or c == "compose.yml" for c in changed):
            targets.append("frontend")
        report = []
        for svc in targets:
            ok = (
                subprocess.run(
                    [str(REPO / "scripts/mc-deploy.sh"), svc], capture_output=True
                ).returncode
                == 0
            )
            report.append(
                f"deployed {svc}"
                if ok
                else f"DEPLOY FAILED {svc} (previous image restored)"
            )
        if "scripts/mc-claude-code-p.py" in changed:
            ok = (
                subprocess.run(
                    ["rc-service", "mc-claude-code-p", "restart"], capture_output=True
                ).returncode
                == 0
            )
            report.append(
                "restarted mc-claude-code-p"
                if ok
                else "RESTART FAILED mc-claude-code-p"
            )
        if report:
            self.comment("**team-runner** deploy: " + "; ".join(report))


def main() -> int:
    task_id = sys.argv[1]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    if os.environ.get("RUNNER_APPLY", "0") != "1":
        log("team runner disarmed; no task claimed")
        return 0
    if not BOARD:
        log("FATAL: RUNNER_BOARD unset")
        return 78
    try:  # a missing or unreadable repo must fail safely, never publish
        git(REPO, "rev-parse", "HEAD")
    except RuntimeError:
        log(f"FATAL: {REPO} is not a usable git checkout; refusing to run")
        return 78
    # One local runner per task for its whole lifecycle. The fd is kept alive
    # (held fd = held flock) until this process exits; the supervisor also
    # probes it, but this is the binding exclusion.
    held = task_lock(task_id)
    if held is None:
        log(f"task {task_id} is already being driven locally; nothing to do")
        return 0
    task = None
    offset = 0
    while task is None:
        tasks = api(
            "get",
            f"/api/v1/boards/{BOARD}/tasks?status=inbox,in_progress&limit=200&offset={offset}",
        )
        rows = tasks if isinstance(tasks, list) else tasks.get("items", [])
        task = next((t for t in rows if t["id"] == task_id), None)
        if len(rows) < 200:
            break
        offset += 200
    if task is None or task.get("execution_mode") != "team":
        log("task is gone or not in team mode; nothing to do")
        return 0
    if task.get("split_count"):
        # Split parents (e.g. a [backlog] manager task that delegated to board
        # children) are not runnable: the children execute the work. The
        # backend refuses the cycle too; this avoids a futile claim first.
        log("task is a split parent; its board children execute the work")
        return 0
    if REPO != MC_REPO:
        # Every cycle starts from upstream, so each PR holds only its own task's work and one
        # publication no longer moves the base under every other cycle (owner 2026-10-24).
        # A task whose dependencies were published as still-unmerged PRs builds on those
        # branches; otherwise it never sees the code it depends on (0000a016, 0000a01d).
        try:
            with global_lock(time.monotonic() + 120):
                git(REPO, "fetch", "-q", "origin", check=False)
                upstream = git(REPO, "rev-parse", "--abbrev-ref", "@{upstream}", check=False)
                if upstream:
                    git(REPO, "reset", "-q", "--hard", upstream, check=False)
                    for ref in dependency_branches(task):
                        if contains(ref, "HEAD"):
                            continue
                        try:
                            git(REPO, "merge", "-q", "--no-edit", ref, env=identity("coordinator"))
                            log(f"cycle base includes unmerged dependency branch {ref}")
                        except RuntimeError:
                            git(REPO, "merge", "--abort", check=False)
                            log(f"dependency branch {ref} conflicts with upstream; cycle starts without it")
        except RuntimeError:
            log("upstream sync skipped: publication lock busy")
    if git(REPO, "status", "--porcelain"):
        log("deployment checkout is dirty; leaving team task unclaimed")
        return 0
    task = check_task_budget(api, BOARD, task_id, task)
    if task is None:
        return 0
    patch = f"/api/v1/boards/{BOARD}/tasks/{task_id}"
    if task["status"] == "inbox":
        try:
            task = api(
                "patch",
                patch,
                {
                    "status": "in_progress",
                    "assigned_agent_id": None,
                    "expected_status": "inbox",
                    "expected_updated_at": task["updated_at"],
                },
            )
        except ApiError:
            log("claim rejected; no model started")
            return 0
    elif task["status"] != "in_progress":
        return 0
    task = check_task_budget(api, BOARD, task_id, task)
    if task is None:
        return 0
    run = Cycle(task_id, task)
    try:
        started = api("post", run.base + "/cycles")
        # Do NOT unconditionally recover on resume: with the pool, sibling
        # runners may hold live leases on this cycle. Backend recovery expires
        # only actually-expired worker leases (via reap during claims).
        log(
            f"team cycle {started['cycle']['id']} for task {task_id} resumed={started['resumed']}"
        )
        current, _ = run.view()
        if current["state"] == "publishing":
            api("post", f"{run.base}/cycles/{current['id']}/recover", {})
        run.drive()
        if STOP.is_set():
            return 0
        cycle, _ = run.view()
    except (ApiError, RuntimeError) as exc:
        log(cycle_stop_line(exc))
        if "HTTP 409" in str(exc) and "evidence" in str(exc):
            # ponytail: deterministic evidence refusals never clear by respawning every 10 s.
            try:
                cycle, _ = run.view()
                api("post", f"{run.base}/cycles/{cycle['id']}/block",
                    {"version": cycle["version"], "reason": f"QA handoff refused: {str(exc)[-200:]}"})
            except (ApiError, RuntimeError, KeyError, TypeError):
                pass
        # 2026-10-07: an engine on hold is a wait, not news. Commenting on every 10 s pool
        # tick posted ~1000 identical comments; other failures still comment. Previously:
        # run.comment(f"**team-runner**: team cycle could not start or continue: {exc}. ...")
        if not engine_on_hold(exc):
            run.comment(
                f"**team-runner**: team cycle could not start or continue: {exc}. Task left for the next tick."
            )
        return 0
    finally:
        if STOP.is_set(): run.shutdown()
    if cycle["state"] == "blocked":
        task = check_task_budget(api, BOARD, task_id, task)
        if task is None:
            return 0
        reason = cycle.get("block_reason") or ""
        # 2026-10-07 (independent QA, reproduced): a block after the host
        # fast-forward was requeued as "production moved ... nothing published".
        # Production containing the reviewed commit is evidence; reason text is not.
        reviewed = cycle.get("reviewed_sha")
        maybe_published = bool(reviewed) and is_ancestor(reviewed, "HEAD")
        stale = not maybe_published and ("stale base" in reason or "production moved" in reason)
        api(
            "patch",
            patch,
            {
                "status": "inbox" if stale else "blocked",
                "assigned_agent_id": None,
                "expected_status": "in_progress",
                "expected_updated_at": task["updated_at"],
            },
        )
        run.comment(
            # The coordinator's BLOCKED comment states what happened to publication;
            # this process cannot know whether a vanished publisher fast-forwarded.
            "**DECISION NEEDED** (team cycle blocked): " + reason
            if not stale
            else "**team-runner**: production moved during the cycle; requeued."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
