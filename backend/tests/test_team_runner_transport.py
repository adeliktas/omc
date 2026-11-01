"""Completion transport and cleanup for the host team runner (2026-10-07).

A completion carrying /tmp evidence can exceed the 128 KiB per-argument limit
once JSON-escaped; the body now travels on stdin. Scratch and evidence
directories go on every completion outcome.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("team_runner_transport", SCRIPTS / "mc-team-runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_large_completion_body_reaches_the_api_through_stdin(runner, monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MC_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MC_TOKEN", "fixture")
    body = {"evidence_files": {"emoji.py": "\U0001F600" * 11000}}  # 44 KB, >128 KiB escaped
    assert len(json.dumps(body)) > 128 * 1024
    try:
        assert runner.api("post", "/complete", body) == {}
    finally:
        server.shutdown()
    assert json.loads(received[0]) == body


@pytest.mark.parametrize("failure", [OSError(7, "Argument list too long"), KeyboardInterrupt()])
def test_scratch_and_evidence_are_removed_whatever_completion_does(runner, monkeypatch, tmp_path, failure):
    made = []

    def mkdtemp(prefix):
        path = tmp_path / f"{prefix}{len(made)}"
        path.mkdir()
        made.append(path)
        return str(path)

    def api(method, path, body=None):
        if path.endswith("/complete"):
            raise failure
        return {}

    monkeypatch.setattr(runner.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(runner, "api", api)
    monkeypatch.setattr(runner, "log", lambda *_: None)
    cycle = runner.Cycle.__new__(runner.Cycle)
    cycle.base, cycle.cycle = "/team", {"id": "c"}

    def execute(claim, tmp, cancelled):
        (runner.evidence_dir(tmp) / "probe.py").write_text("print(1)\n")
        return {"ok": True, "claim_token": "t"}

    cycle._execute = execute
    claim = {"attempt": {"id": "a", "stage_key": "dev"}, "claim_token": "t", "role_kind": "developer"}
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            cycle.attempt(claim)
    else:
        cycle.attempt(claim)
    assert made and not made[0].exists() and not runner.evidence_dir(made[0]).exists()


def test_collector_caps_the_total_it_sends(runner, tmp_path):
    root = tmp_path / "ev"
    root.mkdir()
    for name in ("a.py", "b.py"):
        (root / name).write_text("x" * 40000)
    assert list(runner.collect_evidence(root)) == ["a.py"]  # b would exceed 64 KiB in total


def _production(tmp_path, monkeypatch, runner):
    import subprocess

    repo = tmp_path / "production"
    files = {
        "backend/app/api.py": "print('prod')\n",
        "backend/README.md": "readme\n",
        "fixtures/expected.json": '{"ok": true}\n',
        "submodule blob label/x.txt": "x\n",
        "odd \t name.txt": "odd\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git = ["git", "-c", "commit.gpgsign=false", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "-c", "user.name=f", "-c", "user.email=f@invalid", "commit", "-qm", "base"], check=True)
    monkeypatch.setattr(runner, "REPO", repo)
    return subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()


def test_identical_repository_files_are_named_not_attached(runner, monkeypatch, tmp_path):
    base = _production(tmp_path, monkeypatch, runner)
    known = runner.repo_blobs(base)
    assert set(known) == {
        "backend/app/api.py", "backend/README.md", "fixtures/expected.json",
        "submodule blob label/x.txt", "odd \t name.txt",
    }
    root = tmp_path / "ev"
    copy = root / "verify/repo/backend"
    (copy / "app").mkdir(parents=True)
    (copy / "tests").mkdir()
    (copy / "README.md").write_text("readme\n")  # identical to backend/README.md: named only
    (copy / "app/api.py").write_text("print('edited')\n")  # modified: evidence
    (copy / "tests/test_t067.py").write_text("def test_x():\n    assert True\n")  # new: evidence
    (root / "actual-result.json").write_text('{"ok": true}\n')  # equal to a fixture elsewhere: evidence
    (root / "_unchanged-repository-copies.txt").write_text("worker's own notes\n")  # never replaced
    got = runner.collect_evidence(root, known)
    assert sorted(got) == sorted([
        runner.COPY_MANIFEST, "_unchanged-repository-copies.txt", "actual-result.json",
        "verify/repo/backend/app/api.py", "verify/repo/backend/tests/test_t067.py",
    ])
    assert got["_unchanged-repository-copies.txt"] == "worker's own notes\n"
    assert "/tmp/verify/repo/backend/README.md -> backend/README.md" in got[runner.COPY_MANIFEST]


def test_longest_repository_path_decides_and_odd_names_stay_safe(runner, monkeypatch, tmp_path):
    import subprocess

    repo = tmp_path / "production"
    repo.mkdir()
    (repo / "backend").mkdir()
    (repo / "backend/results.txt").write_text("old result\n")
    (repo / "results.txt").write_text("success\n")
    odd = b"nonutf-\xff.txt"
    (repo / "x").mkdir()
    (repo / "x" / os.fsdecode(odd)).write_text("odd\n")
    git = ["git", "-c", "commit.gpgsign=false", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "-c", "user.name=f", "-c", "user.email=f@invalid", "commit", "-qm", "b"], check=True)
    monkeypatch.setattr(runner, "REPO", repo)
    known = runner.repo_blobs(subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip())
    root = tmp_path / "ev"
    (root / "verify/repo/backend").mkdir(parents=True)
    (root / "verify/repo/backend/results.txt").write_text("success\n")  # differs from its own path
    (root / "verify/repo/x").mkdir()
    (root / "verify/repo/x" / os.fsdecode(odd)).write_text("odd\n")
    (root / "probe.py").write_text("print(1)\n")
    got = runner.collect_evidence(root, known)
    assert "verify/repo/backend/results.txt" in got and "probe.py" in got
    assert "nonutf-\\udcff.txt" in got[runner.COPY_MANIFEST]  # escaped, never a crash


def test_manifest_keeps_its_slot_when_the_file_limit_is_reached(runner, monkeypatch, tmp_path):
    base = _production(tmp_path, monkeypatch, runner)
    root = tmp_path / "ev"
    (root / "repo/backend").mkdir(parents=True)
    (root / "repo/backend/README.md").write_text("readme\n")
    for i in range(runner.EVIDENCE_MAX_FILES + 3):
        (root / f"p{i:02}.py").write_text("print(1)\n")
    got = runner.collect_evidence(root, runner.repo_blobs(base))
    assert len(got) == runner.EVIDENCE_MAX_FILES and runner.COPY_MANIFEST in got


def test_repo_blobs_is_bounded_and_fails_open_to_no_detection(runner, monkeypatch, tmp_path):
    import subprocess

    assert runner.repo_blobs(None) == {} and runner.repo_blobs("0" * 40) == {}

    def hung(*_a, **_kw):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(runner.subprocess, "run", hung)
    assert runner.repo_blobs("a" * 40) == {}


def test_manifest_reaches_qa_even_behind_a_near_limit_early_file(runner, monkeypatch, tmp_path):
    from app.services import team_cycle as svc

    assert runner.COPY_MANIFEST == svc.EVIDENCE_MANIFEST
    base = _production(tmp_path, monkeypatch, runner)
    root = tmp_path / "ev"
    (root / "repo/backend").mkdir(parents=True)
    (root / "repo/backend/README.md").write_text("readme\n")  # identical: named in the manifest
    (root / "A.py").write_text("x" * 57100)  # sorts before "_" and nearly fills the budget
    got = runner.collect_evidence(root, runner.repo_blobs(base))
    assert sorted(got) == ["A.py", runner.COPY_MANIFEST]
    attached = set()
    appendix = svc.evidence_appendix(got, attached=attached)
    assert attached == set(got)  # the manifest must not squeeze out the early file
    assert f"### /tmp/{runner.COPY_MANIFEST}" in appendix
    assert "/tmp/repo/backend/README.md -> backend/README.md" in appendix


def test_long_names_stay_distinct(runner):
    one, two = "d/" + "x" * 240 + "one.txt", "d/" + "x" * 240 + "two.txt"
    assert runner._shown(one) != runner._shown(two) and len(runner._shown(one)) < 200
    assert runner._shown("short.txt") == "short.txt"


def test_cited_probes_go_before_alphabetically_earlier_files(runner, tmp_path):
    root = tmp_path / "ev"
    (root / "pgjar/share").mkdir(parents=True)
    for i in range(runner.EVIDENCE_MAX_FILES + 4):
        (root / f"pgjar/share/ext{i:02}.sql").write_text("select 1;\n")
    (root / "t067_crash.py").write_text("assert True\n")
    (root / "t068_report.py").write_text("assert True\n")
    report = "Probes: /tmp/t067_crash.py and /tmp/t068_report.py, each 1 passed."
    got = runner.collect_evidence(root, None, report)
    assert {"t067_crash.py", "t068_report.py"} <= set(got) and len(got) == runner.EVIDENCE_MAX_FILES
    assert "t067_crash.py" not in runner.collect_evidence(root)  # without citations: name order


def _fake_docker(runner, monkeypatch, migrate_rc=0):
    calls = []

    class Done:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out

    def supervised(cmd, timeout, stop, env=None):
        calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return 0, "mc-test-pg-old 1\nmc-test-pg-live 99999999999\n"
        if cmd[:3] == ["docker", "network", "inspect"]:
            return 0, "false\n"
        if cmd[:2] == ["docker", "port"]:
            return 0, "127.0.0.1:41234\n"
        if any("alembic" in part for part in cmd):
            assert cmd[0] == "bwrap"  # attempt-authored migrations never run on the host
            assert re.fullmatch(r"postgresql\+psycopg://mc_audit_[0-9a-f]{16}@127\.0\.0\.1:41234/mc_audit",
                                env["DATABASE_URL"])
            return migrate_rc, ""
        return 0, ""

    def run(cmd, **kw):  # only the final removal is outside supervision
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(runner, "_supervised", supervised)
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setenv("RUNNER_TEST_PG", "1")
    return calls


def test_disposable_postgres_is_per_attempt_secretless_and_removed(runner, monkeypatch, tmp_path):
    calls = _fake_docker(runner, monkeypatch)
    with runner.disposable_postgres("developer", tmp_path, 600) as url:
        role = re.search(r"//(mc_audit_[0-9a-f]{16})@", url).group(1)
        assert url == f"postgresql+psycopg://{role}@127.0.0.1:41234/mc_audit"
        started = next(c for c in calls if c[:2] == ["docker", "run"])
        mount = started[started.index("-v") + 1]
        assert mount.endswith(":/docker-entrypoint-initdb.d/init.sh:ro")
        init = Path(mount.split(":")[0]).read_text()  # runs before the server listens on TCP
        assert f"host mc_audit {role} all trust" in init and "host all all all reject" in init
        assert f"CREATE ROLE {role} LOGIN" in init and "SUPERUSER" not in init
    name = started[started.index("--name") + 1]
    assert "127.0.0.1::5432" in started and "--pull=never" in started
    assert started[started.index("--network") + 1] == runner.TEST_PG_NETWORK
    assert not any("pg_hba.conf" in " ".join(c) for c in calls if c[:2] == ["docker", "exec"])
    assert ["docker", "rm", "-f", "-v", "mc-test-pg-old"] in calls  # expired leftover reaped
    assert ["docker", "rm", "-f", "mc-test-pg-live"] not in calls  # another runner's live one kept
    assert calls[-1] == ["docker", "rm", "-f", "-v", name]
    with runner.disposable_postgres("qa", tmp_path, 600) as other:
        assert other != url  # a fresh random role per attempt


def test_disposable_postgres_fails_open_to_no_database(runner, monkeypatch, tmp_path):
    calls = _fake_docker(runner, monkeypatch, migrate_rc=1)
    with runner.disposable_postgres("qa", tmp_path, 600) as url:
        assert url is None  # migration failed: the *_pg tests skip as before
    assert calls[-1][:3] == ["docker", "rm", "-f"]
    with runner.disposable_postgres("pm", tmp_path, 600) as url:
        assert url is None  # planners never get one


def test_supervised_setup_steps_stop_at_once(runner):
    import threading

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    assert runner._supervised(["sleep", "30"], 60, stop.is_set) == (-1, "")
    assert time.monotonic() - started < 2
    assert runner._supervised(["echo", "hi"], 10, lambda: False) == (0, "hi\n")


def test_disposable_postgres_is_opt_in(runner, monkeypatch, tmp_path):
    calls = _fake_docker(runner, monkeypatch)
    monkeypatch.delenv("RUNNER_TEST_PG")
    with runner.disposable_postgres("qa", tmp_path, 600) as url:
        assert url is None
    assert calls == []


def test_only_whole_cited_paths_count_and_priority_survives_attachment(runner, tmp_path):
    from app.services import team_cycle as svc

    root = tmp_path / "ev"
    root.mkdir()
    (root / "probe.py").write_text("x" * 65536)  # uncited; would fill the budget alone
    (root / "probe.py.real.py").write_text("assert True\n")
    got = runner.collect_evidence(root, None, "ran /tmp/probe.py.real.py: 1 passed.")
    assert list(got)[0] == "probe.py.real.py"
    (root / "probe.py").write_text("y = 1\n" * 3400)  # 20 KB, sorts first
    (root / "z_cited.py").write_text("z = 2\n" * 6800)  # 40 KB, cited
    got = runner.collect_evidence(root, None, "Probe /tmp/z_cited.py (exit 0).")
    assert list(got)[0] == "z_cited.py"
    appendix = svc.evidence_appendix(got)
    assert "### /tmp/z_cited.py" in appendix  # the backend keeps the runner's priority


def test_disposable_postgres_stops_cleanly_and_never_costs_the_result(runner, monkeypatch, tmp_path):
    import subprocess
    import threading

    calls = _fake_docker(runner, monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    with runner.disposable_postgres("qa", tmp_path, 600, cancelled) as url:
        assert url is None  # cancelled: no start, no migration
    assert not any(c[:2] == ["docker", "run"] for c in calls)
    runner.STOP.set()
    try:
        with runner.disposable_postgres("qa", tmp_path, 600) as url:
            assert url is None
    finally:
        runner.STOP.clear()

    def interrupted_setup(cmd, timeout, stop, env=None):
        calls.append(cmd)
        if cmd[:2] == ["docker", "port"]:
            raise KeyboardInterrupt
        return 0, "false\n" if cmd[:3] == ["docker", "network", "inspect"] else ""

    monkeypatch.setattr(runner, "_supervised", interrupted_setup)
    calls.clear()
    try:
        with runner.disposable_postgres("qa", tmp_path, 600):
            pass
    except KeyboardInterrupt:
        pass
    assert calls[-1][:3] == ["docker", "rm", "-f"]  # cleanup covers setup too

    _fake_docker(runner, monkeypatch)
    real_run = runner.subprocess.run

    def removal_times_out(cmd, **kw):
        if cmd[:2] == ["docker", "rm"]:
            raise subprocess.TimeoutExpired(cmd, 60)
        return real_run(cmd, **kw)

    monkeypatch.setattr(runner.subprocess, "run", removal_times_out)
    observed = {"success": True, "text": "VERDICT: PASS", "usage_observed": True}
    monkeypatch.setattr(runner, "_run_harness", lambda *a, **k: observed)
    claim = {"role_kind": "qa", "attempt": {"id": "a", "provider": "p", "model": "m"}}
    assert runner.run_harness(claim, "brief", tmp_path, tmp_path, 600) is observed


def test_cancellation_mid_setup_or_exhausted_budget_never_starts_the_worker(runner, monkeypatch, tmp_path):
    import threading

    _fake_docker(runner, monkeypatch)
    cancelled = threading.Event()
    fake = runner._supervised

    def cancel_during_migration(cmd, timeout, stop, env=None):
        if any("alembic" in part for part in cmd):
            cancelled.set()
        return fake(cmd, timeout, stop, env)

    monkeypatch.setattr(runner, "_supervised", cancel_during_migration)
    launched = []
    monkeypatch.setattr(runner, "_run_harness", lambda *a, **k: launched.append(a) or {"success": True})
    monkeypatch.setattr(runner, "log", lambda *_: (_ for _ in ()).throw(OSError("log unwritable")))
    claim = {"role_kind": "qa", "attempt": {"id": "a", "provider": "p", "model": "m"}}
    res = runner.run_harness(claim, "brief", tmp_path, tmp_path, 600, cancelled)
    assert launched == [] and res["text"] == "interrupted: claim lost; worker not started"
    import contextlib

    clock = {"now": 0.0}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])

    @contextlib.contextmanager
    def slow_setup(*_a):
        clock["now"] += 25  # database setup used 25 s of a 40 s attempt
        yield "postgresql+psycopg://mc_audit@127.0.0.1:1/mc_audit"

    monkeypatch.setattr(runner, "disposable_postgres", slow_setup)
    res = runner.run_harness(claim, "brief", tmp_path, tmp_path, 40)
    assert launched == [] and res["text"].startswith("interrupted: attempt time limit reached")
    clock["now"] = 0.0
    monkeypatch.setattr(runner, "disposable_postgres", lambda *a: contextlib.nullcontext(None))
    runner.run_harness(claim, "brief", tmp_path, tmp_path, 8)  # no setup: a short budget still runs
    assert launched and launched[-1][4] == 8


@pytest.fixture(params=["api", "timeout", "launch"])
def failed_ranking_submission(runner, monkeypatch, request):
    failure = {
        "api": runner.ApiError(503, "ranking unavailable"),
        "timeout": subprocess.TimeoutExpired("mc", 30),
        "launch": FileNotFoundError("mc unavailable"),
    }[request.param]
    submit = Mock(side_effect=failure)
    monkeypatch.setattr(runner, "api", submit)
    monkeypatch.setattr(runner, "BOARD", "")
    return submit


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("log_error", [None, OSError("log unwritable")], ids=["logged", "log-failed"])
def test_ranking_failure_preserves_parsed_harness_result(
    runner, monkeypatch, tmp_path, failed_ranking_submission, success, log_error,
):
    parsed = {
        "success": success, "retryable": False, "text": "original worker report",
        "usage_observed": True,
        "usage": {"provider": "openai-codex-test", "model": "fixture", "source": "pi_cli",
                  "success": success, "input_tokens": 17, "output_tokens": 5,
                  "cache_read_tokens": 3, "total_tokens": 25, "cost_usd": 0.2},
        "tool_activity": [{"tool": "bash", "is_error": False}], "tool_counts": {"bash": 1},
    }
    monkeypatch.setattr(runner, "disposable_postgres", lambda *a: nullcontext(None))
    monkeypatch.setattr(runner, "harness_cmd", lambda *a: (
        ["fixture-cli"], {"PI_CODING_AGENT_DIR": str(tmp_path)},
    ))
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, *a, **kw: argv)
    proc = Mock(returncode=0)
    proc.poll.return_value = 0
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=proc))
    monkeypatch.setattr(runner.os, "killpg", Mock())
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=Mock(stdout=json.dumps(parsed))))
    warning = Mock(side_effect=log_error)
    monkeypatch.setattr(runner, "log", warning)
    monkeypatch.setattr(runner, "PONYTAIL_LEVEL", "ultra")
    monkeypatch.setattr(runner, "PONYTAIL_RULES", "PONYTAIL MODE ACTIVE — level: ultra")
    monkeypatch.setattr(runner, "superpowers", lambda *a: [])
    claim = {"role_kind": "specialist", "attempt": {"id": "ranking-result",
             "provider": "openai-codex-test", "model": "fixture", "thinking_level": "low"}}

    result = runner.run_harness(claim, "brief", tmp_path, tmp_path, 30)

    expected = {**parsed}
    if success:
        expected["text"] += "\n\n## Host-observed completed tools\n" + json.dumps({
            "session_id": "mc-team-ranking-result", "events": parsed["tool_activity"],
        })
    assert result == expected  # no setup-failure fallback or lost usage/tool evidence
    failed_ranking_submission.assert_called_once_with("post", "/api/v1/ranking/usage", {
        **parsed["usage"], "harness": {"skills": ["ponytail:ultra"], "mcp": []}})  # the report, not the result
    warning.assert_called_once_with("WARN: ranking usage report failed for openai-codex-test/fixture")


@pytest.mark.parametrize("log_error", [None, OSError("log unwritable")], ids=["logged", "log-failed"])
def test_ranking_failure_preserves_genuine_setup_failure(
    runner, monkeypatch, tmp_path, failed_ranking_submission, log_error,
):
    monkeypatch.setattr(runner, "disposable_postgres", Mock(side_effect=RuntimeError("setup failed")))
    worker = Mock()
    monkeypatch.setattr(runner, "_run_harness", worker)
    warning = Mock(side_effect=log_error)
    monkeypatch.setattr(runner, "log", warning)
    claim = {"role_kind": "specialist", "attempt": {"id": "ranking-setup",
             "provider": "openai-codex-test", "model": "fixture"}}

    result = runner.run_harness(claim, "brief", tmp_path, tmp_path, 30)

    assert result == {
        "success": False, "text": "harness runtime setup failed (RuntimeError)",
        "usage_observed": False,
        "usage": {"provider": "openai-codex-test", "model": "fixture",
                  "source": "pi_cli", "success": False},
    }
    worker.assert_not_called()
    failed_ranking_submission.assert_called_once_with("post", "/api/v1/ranking/usage", result["usage"])
    warning.assert_called_once_with(
        "WARN: failed to report runtime failure for openai-codex-test/fixture"
    )


def test_reaper_stops_between_removals(runner, monkeypatch):
    removed = []

    class Done:
        returncode, stdout = 0, "a 1\nb 1\n"

    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: removed.append(cmd) or Done())
    runner._reap_test_postgres(60, lambda: True)
    assert not any(c[:2] == ["docker", "rm"] for c in removed)


def test_supervised_kills_descendants_that_outlive_their_leader(runner, tmp_path):
    marker = tmp_path / "child.pid"
    script = (
        "import os, subprocess, sys; "
        f"p = subprocess.Popen(['sleep', '30']); open({str(marker)!r}, 'w').write(str(p.pid))"
    )
    rc, _ = runner._supervised([sys.executable, "-c", script], 1, lambda: False)
    pid = int(marker.read_text())
    assert rc == -1  # the child held stdout open past the timeout

    def alive(pid):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().split()[2] not in ("Z", "X")
        except FileNotFoundError:
            return False

    time.sleep(0.2)
    assert not alive(pid)


def test_router_runtime_tag_is_per_attempt_and_not_account_authority(tmp_path):
    from mc_pi_runtime import key_runtime

    original = {"providers": {"omniroute-api": {
        "api": "openai-completions", "baseUrl": "http://fixture.invalid", "apiKey": "fixture-key",
        "headers": {"x-omniroute-session-id": "old-static-tag", "X-OmniRoute-Connection": "owner-hint",
                    "X-Unrelated": "unchanged"}, "models": [{"id": "codex/gpt-6.1-sol"}],
    }}}
    source = tmp_path / "original-models.json"
    source.write_text(json.dumps(original))
    scratch = tmp_path / "one"
    scratch.mkdir()
    runtime = key_runtime("omniroute-api", "codex/gpt-6.1-sol", scratch, source,
                          session_tag="mc-team-7fe2a190-c969-4c6e-9047-22200aaffbda")
    cfg = json.loads((runtime / "models.json").read_text())["providers"]["omniroute-api"]
    assert cfg["headers"] == {
        "X-OmniRoute-Session-Id": "mc-team-7fe2a190-c969-4c6e-9047-22200aaffbda",
        "X-OmniRoute-Connection": "owner-hint", "X-Unrelated": "unchanged",
    }
    assert json.loads(source.read_text()) == original  # never change the shared profile/key
    assert json.loads((runtime / "auth.json").read_text()) == {}
    scratch = tmp_path / "two"
    scratch.mkdir()
    untagged = key_runtime("omniroute-api", "codex/gpt-6.1-sol", scratch, source)
    assert json.loads((untagged / "models.json").read_text())["providers"]["omniroute-api"] == original["providers"]["omniroute-api"]


def test_default_author_tempfile_is_in_public_scratch_and_collected(runner, monkeypatch, tmp_path):
    import shutil

    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap not installed")
    cwd, scratch = tmp_path / "checkout", tmp_path / "private-attempt"
    cwd.mkdir()
    scratch.mkdir()
    runner.evidence_dir(scratch).mkdir()  # actual Cycle.attempt creates this before launch
    # Real sandbox and tempfile/write/collector, no provider or inference.
    program = (
        "import json, os, tempfile; "
        "f=tempfile.NamedTemporaryFile(mode='w',prefix='operator-report-',suffix='.md',delete=False); "
        "f.write('whole original report\\n'); f.close(); "
        "print(json.dumps({'result':'Report: '+f.name,'usage':{'input_tokens':1}}))"
    )
    monkeypatch.setattr(runner, "harness_cmd", lambda *a: (["/usr/bin/python3", "-c", program], {}))
    monkeypatch.setattr(runner, "api", lambda *a, **k: {})
    result = runner._run_harness(
        {"role_kind": "specialist", "capability": "read_only", "attempt": {
            "id": "fixture-tempfile", "provider": "claude-code", "model": "fixture", "thinking_level": "low",
        }}, "brief", cwd, scratch, 30,
    )
    assert result["success"], result
    files = runner.collect_evidence(runner.evidence_dir(scratch), cited=result["text"])
    assert len(files) == 1 and list(files.values()) == ["whole original report\n"]
    assert result["text"] == "Report: /tmp/" + next(iter(files))
    assert not list(scratch.glob("operator-report-*.md"))


@pytest.mark.parametrize("role", ["developer", "specialist", "pm", "qa"])
def test_completed_tool_order_reaches_retained_worker_artifact(runner, monkeypatch, tmp_path, role):
    # Real local CLI subprocess/parser; fixture transport and no sandbox/network.
    # The trace records completed events, never tool arguments/results or promises.
    events = [
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolCallId": "one", "toolName": "bash"},
        {"type": "tool_execution_end", "toolCallId": "one", "toolName": "bash",
         "isError": False, "result": "private output"},
        {"type": "tool_execution_end", "toolCallId": "one", "toolName": "bash"},
        {"type": "tool_execution_start", "toolCallId": "unfinished", "toolName": "write"},
        {"type": "turn_end", "message": {"content": [{"type": "text", "text": "whole fixture report"}],
                                          "stopReason": "stop", "usage": {"totalTokens": 12}}},
        {"type": "agent_end"},
    ]
    program = "print(" + repr("\n".join(map(json.dumps, events))) + ")"
    monkeypatch.setattr(runner, "harness_cmd", lambda *a: (
        [sys.executable, "-c", program], {"PI_CODING_AGENT_DIR": str(tmp_path)},
    ))
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, *a, **kw: argv)
    posted = []
    monkeypatch.setattr(runner, "api", lambda *a, **k: posted.append(a) or {})
    claim = {"role_kind": role, "attempt": {"id": "fixture-trace", "provider": "openai-codex-test",
                                         "model": "fixture", "thinking_level": "low"}}
    result = runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert result["success"] and result["tool_counts"] == {"bash": 1}
    assert len(posted) == 1 and posted[0][2]["total_tokens"] == 12
    body = runner.Cycle._result(result, artifact=result["text"])
    if role in ("developer", "specialist"):
        prefix, trace = body["artifact"].split("\n\n## Host-observed completed tools\n", 1)
        assert prefix == "whole fixture report"
        assert json.loads(trace) == {"session_id": "mc-team-fixture-trace",
                                     "events": [{"tool": "bash", "is_error": False}]}
        assert "private output" not in trace and "unfinished" not in trace
    else:
        # A host JSON appendix must never corrupt a PM's plan or QA's verdict.
        assert body["artifact"] == "whole fixture report"


def test_worker_gets_public_fixture_auth_never_the_operator_token(runner, monkeypatch, tmp_path):
    out = tmp_path / "env.json"
    program = (
        "import json, os; json.dump({k: os.environ.get(k) for k in "
        f"('AUTH_MODE', 'LOCAL_AUTH_TOKEN', 'BASE_URL', 'MC_TOKEN')}}, open({str(out)!r}, 'w'))"
    )
    monkeypatch.setenv("LOCAL_AUTH_TOKEN", "operator-real-token-must-not-leak")
    monkeypatch.setenv("MC_TOKEN", "operator-mc-token")
    monkeypatch.setattr(runner, "harness_cmd", lambda *a: ([sys.executable, "-c", program], {}))
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, *a, **kw: argv)
    monkeypatch.setattr(runner, "api", lambda *a, **k: {})
    claim = {"role_kind": "developer", "attempt": {"id": "a", "provider": "claude-code",
                                                   "model": "opus-5-5", "thinking_level": "low"}}
    runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    seen = json.loads(out.read_text())
    token = seen.pop("LOCAL_AUTH_TOKEN")
    assert seen == {"AUTH_MODE": "local", "BASE_URL": "http://localhost:8000", "MC_TOKEN": None}
    assert len(token) >= 50 and token != "operator-real-token-must-not-leak"
    runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert json.loads(out.read_text())["LOCAL_AUTH_TOKEN"] != token  # fresh per attempt


def test_fixture_hooks_need_explicit_test_mode_and_extras_cannot_override(runner, monkeypatch, tmp_path):
    out = tmp_path / "env.json"
    program = (
        "import json, os; json.dump({k: os.environ.get(k) for k in "
        f"('FIXTURE_MC_TOKEN', 'AUTH_MODE', 'MC_TOKEN', 'DATABASE_URL', 'CLERK_SECRET_KEY', "
        f"'OPERATOR_API_KEY')}}, open({str(out)!r}, 'w'))"
    )
    extras = {"LOCAL_AUTH_TOKEN": "x", "MC_TOKEN": "y", "DATABASE_URL": "z", "AUTH_MODE": "clerk",
              "FIXTURE_MC_TOKEN": "w", "CLERK_SECRET_KEY": "v", "OPERATOR_API_KEY": "u"}
    monkeypatch.setattr(runner, "harness_cmd", lambda *a: ([sys.executable, "-c", program], dict(extras)))
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, *a, **kw: argv)
    monkeypatch.setattr(runner, "api", lambda *a, **k: {})
    monkeypatch.setenv("FIXTURE_MC_TOKEN", "renamed-operator-token")
    monkeypatch.delenv("RUNNER_FIXTURES", raising=False)
    claim = {"role_kind": "qa", "attempt": {"id": "a", "provider": "claude-code",
                                            "model": "opus-5-5", "thinking_level": "low"}}
    runner._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert json.loads(out.read_text()) == {  # extras are allowlisted, not trusted
        "FIXTURE_MC_TOKEN": None, "AUTH_MODE": "local", "MC_TOKEN": None, "DATABASE_URL": None,
        "CLERK_SECRET_KEY": None, "OPERATOR_API_KEY": None,
    }


def test_fixture_log_is_not_bound_outside_test_mode(runner, monkeypatch, tmp_path):
    import shutil

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap not installed")
    log = tmp_path / "masked.env"
    log.write_text("secret\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("FIXTURE_LOG", str(log))
    monkeypatch.delenv("RUNNER_FIXTURES", raising=False)
    assert str(log) not in runner.sandbox_command(["true"], wt, tmp_path, writable=False)
    monkeypatch.setenv("RUNNER_FIXTURES", "1")
    assert str(log) in runner.sandbox_command(["true"], wt, tmp_path, writable=False)


def test_allowed_extra_is_an_exact_runner_derived_pi_dir(runner, tmp_path):
    ok = runner._pi_dirs("openai-codex-2", tmp_path)
    assert ok == {"/root/.pi/agent", str(tmp_path / "api-runtime"), "/root/.pi/accounts/openai-codex-2"}
    assert runner._pi_dirs("pi-neuralwatt", tmp_path) == {"/root/.pi/agent", str(tmp_path / "api-runtime")}
    for crafted in ("/root/.pi/agent/SECRET/..", str(tmp_path / "SECRET"), "/root/.pi/accounts/x",
                    "operator-secret-marker", "/root/.pi/agent/"):
        assert crafted not in ok


def test_every_built_in_harness_extra_survives_the_allowlist(runner, tmp_path):
    for provider, model in (("pi-neuralwatt", "glm-5.3"), ("openai-codex-2", "gpt-6.1-sol")):
        built = runner.harness_cmd(provider, model, "high", "brief", str(tmp_path), "attempt")
        if built is None:
            continue
        for key, value in built[1].items():
            assert key in runner.HARNESS_EXTRA_KEYS and value in runner._pi_dirs(provider, tmp_path), (provider, key)
