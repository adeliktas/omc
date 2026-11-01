"""Execute the runner offline: fake MC and fake CLIs, real temporary Git repos."""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def setup_run(tmp_path, *, provider="claude-code", change_state=None):
    repo = tmp_path / "production"
    (repo / "scripts").mkdir(parents=True)
    source = (ROOT / "scripts/mc-claude-runner.sh").read_text()
    # Only redirect executable lookup and lock location; never invoke host CLIs
    # or contend with the live cron's lock. All actual runner logic is retained.
    source = source.replace('PATH="$HOME/.local/bin:$PATH"', ":")
    source = source.replace('LOCK="${OMC_DATA:-}/run/mc-claude-runner.lock"', f"LOCK={tmp_path}/lock")
    (repo / "scripts/mc-claude-runner.sh").write_text(source)
    shutil.copy(ROOT / "scripts/mc-agent-result.py", repo / "scripts")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "id": "fixture-task",
                "title": "Fixture",
                "status": "inbox",
                "updated_at": "2026-10-03T00:00:00",
                "execution_lane": "harness",
                "complexity": 3,
                "max_seconds_budget": 120,
                "harness_provider": provider,
                "harness_model": "sonnet",
            }
        )
    )
    mc = repo / "scripts/mc"
    mc.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ["FIXTURE_STATE"]);task=json.loads(p.read_text());method,path=sys.argv[1:3]
if method=="get" and path=="/api/v1/ranking":
 print(json.dumps({"levels":[{"entries":[{"provider":task["harness_provider"],"model":task["harness_model"],"eligible":True}]}]}))
elif method=="get" and path.endswith("/tasks"):
 print(json.dumps({"items":[task]}))
elif method=="patch":
 body=json.loads(sys.argv[3]);task["patches"]=task.get("patches",[])+[body]
 if body.get("expected_status")!=task["status"] or body.get("expected_updated_at")!=task["updated_at"]:
  sys.exit(2)
 task["status"]=body["status"];task["updated_at"]="2026-10-03T00:00:01"
 p.write_text(json.dumps(task));print(json.dumps(task))
else:print("{}")
""")
    mc.chmod(0o755)
    git(repo, "init", "-q", "-b", "production")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    old_work = tmp_path / "old-work"
    git(repo, "worktree", "add", "-qb", "old-branch", str(old_work))
    (old_work / "unrelated.txt").write_text("pre-existing owner work\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    cli = binaries / "claude"
    cli.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
if os.environ.get("FIXTURE_FAIL"):
 print("ModuleNotFoundError: No module named 'fixture'");sys.exit(1)
out=Path(os.environ.get("FIXTURE_FILE") or "result.txt");out.parent.mkdir(parents=True,exist_ok=True)
out.write_text("task result\\n")
Path(os.environ["FIXTURE_CALLED"]).write_text("called")
if os.environ.get("FIXTURE_CHANGE"):
 p=Path(os.environ["FIXTURE_STATE"]);t=json.loads(p.read_text())
 t["status"]=os.environ["FIXTURE_CHANGE"];t["updated_at"]="2026-10-03T00:00:02"
 p.write_text(json.dumps(t))
print(json.dumps({"type":"result","result":"OK","usage":{"input_tokens":1,"output_tokens":1}}))
""")
    cli.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        RUNNER_BOARD="fixture-board",
        RUNNER_APPLY="1",
        RUNNER_ENSEMBLE="0",
        RUNNER_WORKDIR=str(old_work),
        RUNNER_LOG=str(tmp_path / "log"),
        FIXTURE_STATE=str(state),
        FIXTURE_CALLED=str(tmp_path / "called"),
        FIXTURE_CHANGE=change_state or "",
    )
    return repo, old_work, state, env


def run(repo, env):
    subprocess.run(
        ["bash", str(repo / "scripts/mc-claude-runner.sh")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_only_isolated_attempt_changes_are_published(tmp_path):
    repo, old_work, state, env = setup_run(tmp_path)
    run(repo, env)
    assert (old_work / "unrelated.txt").read_text() == "pre-existing owner work\n"
    assert not (repo / "unrelated.txt").exists()
    assert (repo / "result.txt").read_text() == "task result\n"
    task = json.loads(state.read_text())
    assert task["status"] == "review"
    assert task["patches"][0]["expected_status"] == "inbox"
    assert task["patches"][-1]["expected_status"] == "in_progress"
    assert task["patches"][-1]["expected_updated_at"] == "2026-10-03T00:00:01"


def test_dirty_production_is_not_claimed_or_modified(tmp_path):
    repo, _, state, env = setup_run(tmp_path)
    (repo / "operator.txt").write_text("unfinished\n")
    run(repo, env)
    assert json.loads(state.read_text())["status"] == "inbox"
    assert not Path(env["FIXTURE_CALLED"]).exists()
    assert not (repo / "result.txt").exists()


def test_lg_pin_never_starts_a_model(tmp_path):
    repo, _, state, env = setup_run(tmp_path, provider="openai-codex-1")
    run(repo, env)
    assert json.loads(state.read_text())["status"] == "inbox"
    assert not Path(env["FIXTURE_CALLED"]).exists()


def test_another_review_transition_does_not_authorize_publication(tmp_path):
    repo, _, state, env = setup_run(tmp_path, change_state="review")
    before = git(repo, "rev-parse", "HEAD")
    run(repo, env)
    assert git(repo, "rev-parse", "HEAD") == before
    assert not (repo / "result.txt").exists()
    assert json.loads(state.read_text())["status"] == "review"


def test_same_status_with_new_version_also_invalidates_attempt(tmp_path):
    repo, _, state, env = setup_run(tmp_path, change_state="in_progress")
    run(repo, env)
    assert not (repo / "result.txt").exists()
    task = json.loads(state.read_text())
    assert task["status"] == "in_progress"
    assert len(task["patches"]) == 1


def test_missing_workspace_never_falls_back_to_project(tmp_path):
    repo, _, state, env = setup_run(tmp_path)
    task = json.loads(state.read_text())
    task["workspace_path"] = str(tmp_path / "missing")
    state.write_text(json.dumps(task))
    run(repo, env)
    assert json.loads(state.read_text())["status"] == "inbox"
    assert not Path(env["FIXTURE_CALLED"]).exists()


def test_dispatch_maps_neuralwatt_and_denies_lg(tmp_path):
    source = (ROOT / "scripts/mc-claude-runner.sh").read_text()
    function = source[source.index("run_harness() {") : source.index("# Advisory map phase:")]
    fake_pi = tmp_path / "pi"
    called = tmp_path / "calls.json"
    fake_pi.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ["FIXTURE_CALLED"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"type":"turn_end","message":{"content":"OK","usage":{"input":1,"output":1},"stopReason":"stop"}}))
""")
    fake_pi.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        HERE=str(ROOT / "scripts"),
        TASK_ID="fixture-task",
        TASK_THINKING="",
        PONYTAIL_SKILL="",
        RUNNER_SESSION="",
        LOG=str(tmp_path / "log"),
        FIXTURE_CALLED=str(called),
    )
    for provider, model in (
        ("pi-neuralwatt", "glm-5.3-flash"),
        ("openai-codex-1", "gpt-5.6-sol"),
        ("openai-codex", "gpt-5.6-sol"),
    ):
        called.unlink(missing_ok=True)
        prefix = tmp_path / provider
        script = "set -uo pipefail; MC=stub_mc; stub_mc() { :; }; log() { :; };\n" + function
        script += f"\nrun_harness {provider} {model} fixture {tmp_path} {prefix} 2\n"
        subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
        result = json.loads(prefix.with_suffix(".json").read_text())
        if provider == "pi-neuralwatt":
            assert result["effective_rc"] == 0
            args = json.loads(called.read_text())
            assert args[args.index("--provider") + 1] == "neuralwatt"
            assert args[args.index("--model") + 1] == "glm-5.3-flash"
        else:
            assert result["effective_rc"] == 78
            assert not called.exists()


def _fake_deploy(repo, tmp_path):
    deploy = repo / "scripts/mc-deploy.sh"
    deploy.write_text(f'#!/usr/bin/env bash\necho "$1" >> {tmp_path}/deployed\n')
    deploy.chmod(0o755)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fake deploy")
    return tmp_path / "deployed"


def test_published_backend_change_is_deployed(tmp_path):
    repo, _, state, env = setup_run(tmp_path)
    deployed = _fake_deploy(repo, tmp_path)
    run(repo, {**env, "FIXTURE_FILE": "backend/app/fix.py"})
    assert (repo / "backend/app/fix.py").exists()  # published
    assert deployed.read_text().split() == ["backend", "webhook-worker"]
    assert json.loads(state.read_text())["status"] == "review"


def test_change_outside_services_deploys_nothing(tmp_path):
    repo, _, _, env = setup_run(tmp_path)
    deployed = _fake_deploy(repo, tmp_path)
    run(repo, {**env, "FIXTURE_FILE": "docs/note.md"})
    assert (repo / "docs/note.md").exists()
    assert not deployed.exists()


def test_repeating_task_waits_for_its_interval(tmp_path):
    from datetime import datetime, timedelta, timezone

    repo, _, state, env = setup_run(tmp_path)
    task = json.loads(state.read_text())
    recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    task.update(repeat_enabled=True, repeat_interval_minutes=120, updated_at=recent.isoformat())
    state.write_text(json.dumps(task))
    run(repo, env)
    assert not (tmp_path / "called").exists()  # not due yet: no model started
    task["updated_at"] = (recent - timedelta(hours=3)).isoformat()
    state.write_text(json.dumps(task))
    run(repo, env)
    assert (tmp_path / "called").exists()
    assert json.loads(state.read_text())["status"] == "inbox"  # back in the queue for next time


def test_failed_attempt_that_changed_nothing_leaves_no_worktree(tmp_path):
    repo, _, state, env = setup_run(tmp_path)
    run(repo, {**env, "FIXTURE_FAIL": "1"})
    worktrees = git(repo, "worktree", "list").splitlines()
    assert len(worktrees) == 2  # production + the owner's old worktree; no attempt kept
    assert json.loads(state.read_text())["status"] == "blocked"  # setup failure: no loop
