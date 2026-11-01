"""scripts/omc-export.sh: the public repo only ever receives trees that pass every leak gate."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NEUTRAL = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), **NEUTRAL}).stdout.strip()


@pytest.fixture
def private(tmp_path):
    """A private repo shipping scripts/ and README.md, plus a denylist and an empty allowlist."""
    repo = tmp_path / "private"
    for rel in ("scripts/omc-export.sh", "scripts/omc_ship.py", "backend/app/__init__.py",
                "backend/app/core/__init__.py", "backend/app/core/evidence_redaction.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, repo / rel)
    (repo / "scripts/omc-export.paths").write_text("scripts/*\nREADME.md\nfrontend/*\n")
    (repo / "README.md").write_text("# demo\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    (tmp_path / "deny.txt").write_text("acme-internal\n")
    (tmp_path / "allow.txt").write_text("")
    return repo


def export(private, tmp_path, *extra):
    args = [str(private / "scripts/omc-export.sh"), "--target", str(tmp_path / "public"),
            "--denylist", str(tmp_path / "deny.txt"), "--allowlist", str(tmp_path / "allow.txt"), *extra]
    return subprocess.run(args, cwd=private, capture_output=True, text=True,
                          env={"PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin", "HOME": str(Path.home())})


def commit_file(repo, rel, data):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(data if isinstance(data, bytes) else data.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {rel}")


def public_log(tmp_path):
    return git(tmp_path / "public", "log", "--format=%an <%ae>|%cn <%ce>|%s")


def test_clean_tree_exports_as_one_placeholder_commit(private, tmp_path):
    r = export(private, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    sha = git(private, "rev-parse", "--short", "HEAD")
    assert public_log(tmp_path) == f"OMC <omc@localhost.invalid>|OMC <omc@localhost.invalid>|export: {sha}"
    assert (tmp_path / "public/README.md").exists() and not (tmp_path / "public/backend").exists()
    assert git(tmp_path / "public", "remote") == ""


@pytest.mark.parametrize("rel,data", [
    ("scripts/cloud.py", 'KEY = "AKIA' + "QYLPMN5HHHFPZAM2" + '"\n'),  # AWS-shaped key
    ("scripts/acme-internal-notes.py", "x = 1\n"),                       # denylisted file name
    ("frontend/public/logo.png", b"\x89PNG\r\n\x1a\n" + b"acme-internal" + b"\x00" * 8),  # term in a binary
    ("scripts/debug.log", "a log\n"),                                    # forbidden file type
])
def test_each_leak_gate_refuses_and_commits_nothing(private, tmp_path, rel, data):
    assert export(private, tmp_path).returncode == 0
    before = public_log(tmp_path)
    commit_file(private, rel, data)
    r = export(private, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert public_log(tmp_path) == before


def test_reviewed_allowlist_line_admits_exactly_that_finding(private, tmp_path):
    commit_file(private, "scripts/fixture.py", 'H = "Bearer synthetic-not-a-credential"\n')
    assert export(private, tmp_path).returncode == 1
    (tmp_path / "allow.txt").write_text("scripts/fixture.py\tshaped\tBearer synthetic-not-a-credential\n")
    assert export(private, tmp_path).returncode == 0


def test_foreign_history_in_the_target_is_refused(private, tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    git(public, "init", "-q", "-b", "main")
    (public / "x").write_text("x\n")
    git(public, "add", "-A")
    git(public, "commit", "-qm", "someone else")
    assert export(private, tmp_path).returncode == 1


def test_missing_denylist_is_a_setup_error(private, tmp_path):
    (tmp_path / "deny.txt").unlink()
    assert export(private, tmp_path).returncode == 2


def test_target_receives_exactly_the_gated_tree(private, tmp_path):
    assert export(private, tmp_path).returncode == 0
    (tmp_path / "public/stray.txt").write_text("never gated\n")  # left in the target by hand
    (private / "scripts/omc-export.paths").write_text("scripts/*\nREADME.md\nfrontend/*\n.gitignore\n")
    commit_file(private, ".gitignore", "ignored-*\n")  # the shipped .gitignore matches a shipped file
    (private / "scripts/ignored-but-shipped.py").write_text("x = 1\n")
    git(private, "add", "-f", "scripts/ignored-but-shipped.py")
    git(private, "commit", "-qm", "add an ignored-but-shipped file")
    r = export(private, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    files = set(git(tmp_path / "public", "ls-files").split())
    assert "stray.txt" not in files and "scripts/ignored-but-shipped.py" in files and ".gitignore" in files
