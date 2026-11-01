"""Shared native-worker trust boundary: allowlisted filesystem projection.

Focused real-Bubblewrap checks that a sandboxed team worker cannot read host
sibling private files, cannot touch host Git object databases (its `.git`
pointer is immutable and unresolvable), sees the known private monitor masked,
gets only its selected native account, and can write only the declared fixture
log. Synthetic sentinels stand in for private host content; no real private
file is ever read. Also covers the shared Pi harness `--` operand fix.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NEED_BWRAP = pytest.mark.skipif(not shutil.which("bwrap"), reason="Bubblewrap not installed")


def runner():
    path = ROOT / "scripts/mc-team-runner.py"
    spec = importlib.util.spec_from_file_location("team_runner_projection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args],
        text=True,
    ).strip()


def _fresh_dir(tmp_path: Path) -> Path:
    """A host directory no sandbox mount path references (bwrap creates the
    parents of mount points as private sandbox space; a never-referenced
    directory stays absent inside the sandbox)."""
    path = tmp_path.parent / f"mc-private-{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def test_projection_is_an_allowlist_not_a_host_root_mount(tmp_path):
    r = runner()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    args = r.sandbox_command([sys.executable, "-c", "pass"], cwd, scratch, writable=False)
    joined = " ".join(args)
    assert "--ro-bind / /" not in joined and "--bind / /" not in joined
    for required in ("--ro-bind /usr /usr", "/etc/ssl", "/etc/resolv.conf", "--dev /dev"):
        assert required in joined
    assert not any(a == "/" for a in args)
    assert "/root/.pi/agent" not in r.MANAGED_RO  # sessions/settings/MCP are not runtimes
    assert "/etc/ssl" not in r.SYSTEM_RO  # private TLS keys are not CA certificates


@NEED_BWRAP
def test_host_siblings_are_unreadable_and_attempt_scope_stays_usable(tmp_path):
    r = runner()
    sentinel = _fresh_dir(tmp_path) / "private.txt"
    sentinel.write_text("HOST-PRIVATE")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "readable.txt").write_text("source\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    dev_check = f"""from pathlib import Path
assert Path('readable.txt').read_text() == 'source\\n'
try:
    Path({str(sentinel)!r}).read_text()
    raise AssertionError('host sibling private file was readable')
except OSError:
    pass
Path('work.txt').write_text('ok')
Path({str(scratch / 's.txt')!r}).write_text('ok')
"""
    subprocess.run(
        r.sandbox_command([sys.executable, "-c", dev_check], cwd, scratch, writable=True),
        check=True,
    )
    assert (cwd / "work.txt").read_text() == "ok"
    assert (scratch / "s.txt").read_text() == "ok"
    assert sentinel.read_text() == "HOST-PRIVATE"
    pm_check = """from pathlib import Path
try:
    Path('pm.txt').write_text('bad')
    raise AssertionError('PM/QA wrote into the read-only worktree')
except OSError:
    pass
"""
    subprocess.run(
        r.sandbox_command([sys.executable, "-c", pm_check], cwd, scratch, writable=False),
        check=True,
    )
    assert not (cwd / "pm.txt").exists()


@NEED_BWRAP
def test_git_pointer_is_immutable_and_no_host_git_runs(tmp_path):
    r = runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "t"), ("user.email", "t@x.invalid")):
        _git(repo, "config", key, value)
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "team/dev", str(wt))
    pointer = wt / ".git"
    before = pointer.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    check = """import subprocess
from pathlib import Path
try:
    with open('.git', 'w') as fh:
        fh.write('gitdir: /must-not-be-used\\n')
    raise AssertionError('Git pointer is writable')
except OSError:
    pass
r = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
assert r.returncode != 0, 'host Git object database is reachable'
r = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
assert r.returncode != 0, 'host Git object database is reachable'
"""
    subprocess.run(
        r.sandbox_command([sys.executable, "-c", check], wt, scratch, writable=True),
        check=True,
    )
    assert pointer.read_bytes() == before  # original Git-pointer check preserved


@NEED_BWRAP
def test_known_private_monitor_is_masked_without_reading_real_content(tmp_path):
    r = runner()
    cwd = tmp_path / "worktree"
    (cwd / "backend/scripts").mkdir(parents=True)
    # Synthetic stand-in only: the real host monitor is never read by tests.
    monitor = cwd / "backend/scripts/ollama_monitor.py"
    monitor.write_text("SYNTHETIC-FAKE-TOKEN-MARKER\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    check = """from pathlib import Path
m = Path('backend/scripts/ollama_monitor.py')
try:
    leaked = m.read_text()
except OSError:
    leaked = ''  # device-bound masks deny the read outright
assert leaked == '', 'private monitor content leaked into the sandbox'
try:
    m.write_text('tamper')
    raise AssertionError('masked monitor is writable')
except OSError:
    pass
Path('normal.txt').write_text('ok')
"""
    subprocess.run(
        r.sandbox_command([sys.executable, "-c", check], cwd, scratch, writable=True),
        check=True,
    )
    assert monitor.read_text() == "SYNTHETIC-FAKE-TOKEN-MARKER\n"  # host bytes intact
    assert (cwd / "normal.txt").read_text() == "ok"


@NEED_BWRAP
def test_native_account_projection_shares_only_the_selected_account(tmp_path, monkeypatch):
    r = runner()
    accounts = tmp_path / "accounts"
    selected = accounts / "openai-codex-2"
    selected.mkdir(parents=True)
    (selected / "auth.json").write_text("{}")
    (selected / "models-store.json").write_text('{"models": []}')
    sibling = accounts / "openai-codex-9"
    sibling.mkdir()
    (sibling / "auth.json").write_text('{"private": "sibling-secret"}')
    monkeypatch.setattr(r, "NATIVE_ACCOUNTS", accounts)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    check = f"""import json
from pathlib import Path
root = Path({str(selected)!r})
(root / 'auth.json').write_text(json.dumps({{"rotated": True}}))  # canonical rotation
try:
    assert (root / 'models-store.json').read_text() == ''  # unrelated config masked
except OSError:
    pass  # denial is equivalent; host contents remain unchanged
try:
    Path({str(sibling / 'auth.json')!r}).read_text()
    raise AssertionError('sibling native account is visible')
except OSError:
    pass
"""
    subprocess.run(
        r.sandbox_command(
            [sys.executable, "-c", check],
            cwd,
            scratch,
            writable=False,
            native_account=selected,
        ),
        check=True,
    )
    assert json.loads((selected / "auth.json").read_text()) == {"rotated": True}
    assert (sibling / "auth.json").read_text() == '{"private": "sibling-secret"}'
    for wrong in (accounts / "openai-codex-1", tmp_path / "outside", accounts.parent):
        with pytest.raises(RuntimeError, match="refusing non-isolated or LG"):
            r.sandbox_command(["true"], cwd, scratch, writable=False, native_account=wrong)
    outside_link = accounts / "openai-codex-7"
    outside_link.symlink_to(selected)
    with pytest.raises(RuntimeError, match="refusing non-isolated or LG"):
        r.sandbox_command(["true"], cwd, scratch, writable=False, native_account=outside_link)


@NEED_BWRAP
def test_fixture_log_is_the_only_writable_fixture_path(tmp_path, monkeypatch):
    r = runner()
    log = tmp_path / "cli.log"
    log.touch()
    monkeypatch.setenv("FIXTURE_LOG", str(log))
    sibling_dir = _fresh_dir(tmp_path)
    bins = tmp_path / "bin"
    bins.mkdir()
    cli = bins / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        f"Path(os.environ['FIXTURE_LOG']).open('a').write('called\\n')\n"
        "try:\n"
        f"    Path({str(sibling_dir / 'escape.txt')!r}).write_text('bad')\n"
        "    raise SystemExit('wrote outside the declared fixture log')\n"
        "except OSError:\n"
        "    pass\n"
    )
    cli.chmod(0o755)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run(r.sandbox_command([str(cli)], cwd, scratch, writable=False), check=True)
    assert log.read_text() == "called\n"
    assert not (sibling_dir / "escape.txt").exists()


@NEED_BWRAP
def test_main_checkout_git_objects_are_hidden_too(tmp_path):
    r = runner()
    cwd, scratch = tmp_path / "cwd", tmp_path / "scratch"
    cwd.mkdir(); scratch.mkdir()
    (cwd / ".git").mkdir()
    (cwd / ".git" / "private-object").write_text("synthetic private history")
    script = "from pathlib import Path; assert not Path('.git/private-object').exists()"
    subprocess.run(r.sandbox_command([sys.executable, "-c", script], cwd, scratch, writable=False), check=True)
    assert (cwd / ".git" / "private-object").read_text() == "synthetic private history"


def test_pi_prompt_is_an_operand_after_dash_dash():
    r = runner()
    prompt = "- not an option\n- second markdown line"
    argv, extra = r.harness_cmd(
        "pi-neuralwatt", "glm-5.3-flash", "low", prompt, "/tmp/sessions", "abc12345"
    )
    assert argv[0] == "/root/.pi/agent/bin/pi"
    assert argv[-2:] == ["--", prompt]  # a leading-dash prompt is never a flag
    assert argv[argv.index("--provider") + 1] == "neuralwatt"
    assert extra["PI_CODING_AGENT_DIR"] == "/root/.pi/agent"
    assert (
        r.harness_cmd("openai-codex-1", "gpt-5.6-sol", "low", prompt, "/tmp/sessions", "abc12345")
        is None
    )  # LG stays refused
    claude, _ = r.harness_cmd("claude-code", "sonnet", "low", prompt, "/tmp/sessions", "abc12345")
    assert claude[claude.index("-p") + 1] == prompt
