"""Failure-first regressions: no providers, production DB or unsafe evidence bypass."""

import json

import pytest

from app.core.evidence_redaction import redact_diff, redact_evidence
from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING, to_qa
from tests.test_team_verification import VERIFY_PLAN, _planned, claim
from tests.test_team_verification import env as _env

env = _env  # expose the reused pytest fixture without shadowing an imported name


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\v", "\f"])
def test_git_context_counts_only_lf_and_requires_both_versions(sep):
    from app.core.evidence_redaction import python_identifiers

    source = "# note" + sep + " password=unquotedPrivate\npassword\n"
    patch = "--- a/case.py\n+++ b/case.py\n@@ -0,0 +1,2 @@\n" + "".join(
        "+" + line + "\n" for line in source.split("\n")[:-1]
    )
    assert redact_diff(patch, contexts={("+", "case.py"): python_identifiers(source)}) != patch
    context = "--- a/case.py\n+++ b/case.py\n@@ -1,1 +1,1 @@\n password=unquotedPrivate\n"
    assert (
        redact_diff(context, contexts={("-", "case.py"): {(1, 0)}, ("+", "case.py"): set()})
        != context
    )


@pytest.mark.parametrize(
    "source",
    [
        "password=supplier(),\n",
        "const password=supplier(),\n",
        "let password=supplier(),\n",
        "let password=supplier(),;\nmarker();\n",
        "function f() { let password=supplier(),; }\nmarker();\n",
        "for(let password=supplier(),;;) {}\nmarker();\n",
        "getToken: vi.fn(),\npassword=supplier(),\n",
    ],
)
def test_incomplete_declarations_are_not_callback_properties(source):
    from tests.test_backlog_independent_regressions import runner

    assert runner().source_identifiers(source, "case.ts") == set()


@pytest.mark.parametrize(
    "source",
    [
        "const usage={input_tokens:count,output_tokens:123};\n",
        "const usage={input_tokens:123,output_tokens:count};\n",
        "interface Credentials { token: string }\n",
        'interface Credentials {\n "token": string;\n}\n',
    ],
)
def test_authenticated_compact_types_and_mixed_counters(source):
    from tests.test_backlog_independent_regressions import runner
    from app.core.evidence_redaction import redact_diff

    contexts = {("+", "case.ts"): runner().source_identifiers(source, "case.ts")}
    diff = "diff --git a/case.ts b/case.ts\n--- /dev/null\n+++ b/case.ts\n@@ -0,0 +1,%d @@\n" % len(
        source.splitlines()
    )
    diff += "".join("+" + line + "\n" for line in source.splitlines())
    assert redact_diff(diff, contexts=contexts) == diff


def test_source_counter_annotation_and_reference_are_not_credentials():
    patch = (
        "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n"
        "+def check(token: str):\n+    prompt_tokens = 123\n"
        "+    same_board_agent_token = make_fixture()\n"
        "diff --git a/page.tsx b/page.tsx\n--- a/page.tsx\n+++ b/page.tsx\n"
        "+const requestToken = ++generation;\n"
        '+const usage = {"input_tokens": 100, "output_tokens": 20};\n'
        '+const taskId = "tsk-task-a-move-done";\n'
        "+getToken: vi.fn(),\n"
        "+completion_tokens=m.completion_tokens, duration_ms=m.duration_ms,\n"
        "+prompt_tokens=pt, completion_tokens=ct, duration_ms=duration_ms,\n"
        "+prompt_tokens=pt, input_tokens=123)\n"
    )
    assert redact_diff(patch) == patch


@pytest.mark.parametrize(
    "literal", ["12345678", "0x12", "1.5", "True", "False", "true", "false", "null"]
)
def test_numeric_and_keyword_credential_literals_are_not_references(literal):
    expression = f"password = String({literal});"
    assert redact_evidence(expression, code=True) != expression
    patch = "diff --git a/test.ts b/test.ts\n--- a/test.ts\n+++ b/test.ts\n+" + expression
    assert redact_diff(patch) != patch


def test_diff_continuation_checks_both_source_versions_and_hunks():
    header = "diff --git a/test.ts b/test.ts\n--- a/test.ts\n+++ b/test.ts\n"
    for body in (
        ' password = getpass()\n-old_reference\n+("private-literal")\n',
        ' password = getpass()\n-("private-literal")\n+new_reference\n',
        ' password = getpass()\n@@ -100,2 +100,2 @@\n+("private-literal")\n',
    ):
        assert redact_diff(header + body) != header + body


@pytest.mark.parametrize(
    "value",
    [
        "api_key=sk-SYNTHETIC12345678901234567890",
        "password=SYNTHETICPASSWORD",
        'access_token = "a-private-token"',
        "Bearer SYNTHETIC12345678901234567890",
    ],
)
def test_credential_values_still_block_even_in_source_or_tests(value):
    patch = "diff --git a/test.py b/test.py\n--- a/test.py\n+++ b/test.py\n+" + value + "\n"
    # Bare Python references aren't values; quoted/credential-shaped values are.
    if value == "password=SYNTHETICPASSWORD":
        assert redact_evidence(value) != value
        yaml = patch.replace("test.py", "settings.yaml")
        assert redact_diff(yaml) != yaml
    else:
        assert redact_diff(patch) != patch


@pytest.mark.parametrize(
    "error",
    [
        "QA cannot inspect complete required changed bytes",
        "Unexpected message role.",
        "edits outside assigned scope left uncommitted",
        "worker changed Git metadata; no host Git operation permitted",
        "no time left in the cycle",
        "storage exhausted (ENOSPC)",
        "memory exhausted (ENOMEM)",
        "harness setup failed (failed to run command)",
        "harness runtime setup failed (ModuleNotFoundError)",
        "interrupted: attempt time limit reached; partial output discarded",
    ],
)
async def test_unchanged_harness_policy_failure_is_not_replayed(env, error):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, attempt, token, ok=False, error=error)
    assert cycle.state == "blocked" and cycle.round == 1
    assert len(await svc._attempts(s, cycle.id)) == 1


async def test_provider_failure_with_exact_base_metadata_keeps_retry_handling(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    await svc.complete_attempt(
        s, board, cycle, attempt, token, ok=False,
        error="provider API error (503): temporarily unavailable",
        head_sha=attempt.base_sha, changed_files=[],
    )
    assert attempt.status == "failed"
    assert cycle.state != "blocked"
    retry, _ = await claim(s, cycle)
    assert retry.attempt_no == 2


async def test_runner_stop_is_retried_as_an_interruption_not_a_model_failure(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, attempt, token, ok=False,
                               error="interrupted: runner stopping; partial output discarded")
    retry = [a for a in await svc._attempts(s, cycle.id) if a.attempt_no == 2]
    assert cycle.state != "blocked" and len(retry) == 1


@pytest.mark.parametrize("artifact", ["x" * 65537, '{"password":"SYNTHETIC-private-literal"}'])
async def test_rejected_planner_artifact_keeps_nonretryable_evidence_failure(env, artifact):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    pm, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, pm, token, ok=True, artifact=artifact)
    assert "SYNTHETIC-private-literal" not in (pm.artifact or "")  # redacted in place
    assert "credential redaction" not in (pm.error or "")


@pytest.mark.parametrize("origin", ["runner"])  # the validator no longer rejects scope (owner 2026-10-22)
async def test_long_scope_failures_block_before_diagnostic_clipping(env, origin):
    s, board, task = env
    cycle = await _planned(
        s, board, task, {"workers": [{"key": "main", "role": "developer", "scope": ["allowed"]}]}
    )
    worker, token = await claim(s, cycle)
    assert worker.role_kind == "developer" and worker.attempt_no == 1
    path = "outside/" + "/".join("x" * 200 for _ in range(6)) + "/stray.txt"
    await svc.complete_attempt(
        s,
        board,
        cycle,
        worker,
        token,
        ok=origin == "validator",
        artifact="worker report",
        error="edits outside assigned scope left uncommitted: " + path,
        head_sha="b" * 40,
        changed_files=[path],
    )
    assert cycle.state == "blocked" and not any(
        a.attempt_no > 1 for a in await svc._attempts(s, cycle.id)
    )
    assert len(worker.error) <= 1000 and "outside assigned scope" in worker.error


async def test_late_credential_failure_classification_survives_head_clipping(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    worker, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        worker,
        token,
        ok=False,
        error="trace " * 500 + "credential store read failed",
    )
    assert cycle.state == "blocked" and "credential repair required" in cycle.block_reason
    assert len(worker.error) <= 1000 and len(await svc._attempts(s, cycle.id)) == 1


async def test_rejected_planner_receives_feedback_and_exact_bounds(env):
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    pm, token = await claim(s, cycle)
    bad = {"workers": [{"key": "too-long-" * 4, "scope": ["backend/app"]}]}
    await svc.complete_attempt(s, board, cycle, pm, token, ok=True, artifact=json.dumps(bad))
    retry, _ = await claim(s, cycle)
    brief = svc.brief(task, cycle, retry, await s.get(TeamMember, retry.member_id), None)
    assert pm.error in brief
    assert "max 24 characters" in brief and "1..10" in brief
    assert retry.attempt_no == 2


async def test_full_verification_report_survives_storage_and_qa_handoff(env):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    report = (
        "BEGIN verification: checked fixture\n" + "detail " * 1200 + "\nEND verification: results"
    )
    await svc.complete_attempt(
        s, board, cycle, worker, token, ok=True, artifact=report, head_sha=BASE, changed_files=[]
    )
    assert worker.artifact == report
    await svc.integrate(s, cycle, BASE)
    qa, _ = await claim(s, cycle)
    brief = svc.brief(
        task,
        cycle,
        qa,
        await s.get(TeamMember, qa.member_id),
        None,
        history=await svc._attempts(s, cycle.id),
    )
    assert (
        json.loads(svc.worker_evidence(cycle, await svc._attempts(s, cycle.id))[0])[0]["artifact"]
        == report
    )
    assert "BEGIN verification" in brief and "END verification" in brief
    assert "UNTRUSTED evidence" in brief


async def test_missing_verifier_report_blocks_before_qa_inference(env):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        worker,
        token,
        ok=True,
        artifact="checked fixture",
        head_sha=BASE,
        changed_files=[],
    )
    worker.artifact = ""
    await svc.integrate(s, cycle, BASE)
    assert await svc.claim_next(s, cycle, base_sha=BASE) is None
    assert cycle.state == "blocked" and cycle.round == 1
    assert not any(a.role_kind == "qa" and a.claim_count for a in await svc._attempts(s, cycle.id))


@pytest.mark.parametrize(
    "artifact", ["x" * (svc.MAX_ARTIFACT_BYTES + 1), "password=SYNTHETICPASSWORD"]
)
async def test_incomplete_verifier_evidence_is_not_truncated_or_replayed(env, artifact):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    await svc.complete_attempt(
        s, board, cycle, worker, token, ok=True, artifact=artifact, head_sha=BASE, changed_files=[]
    )
    assert "credential redaction" not in (worker.error or "")  # redacted in place, not discarded
    assert "SYNTHETICPASSWORD" not in (worker.artifact or "")
    assert not any(a.attempt_no > 1 for a in await svc._attempts(s, cycle.id))


@pytest.mark.parametrize(
    "expression",
    [
        'password = b"private-literal"',
        'password = r"private-literal"',
        'password = f"private-literal"',
        'password = br"private-literal"',
        'password = getpass("private-literal")',
        'db_password = os.environ.get("DB_PASSWORD", "private-literal")',
        'password = lookup(KEY) or "private-literal"',
        'password = "two private-literal words"',
        'password = """first\nprivate-literal\nlast"""',
        'password = getpass(\n"private-literal"\n)',
        'password = getpass (\n"private-literal"\n)',
        'password = getpass\t(\n"private-literal"\n)',
        'password = store [\n"private-literal"\n]',
        'password = keyring.get_password (\n"svc", "private-literal"\n)',
        'password = getpass ( # )\n"private-literal"\n)',
        'password = store [ # ]\n"private-literal"\n]',
        'password = call((arg), [\n"private-literal"\n])',
        'input_tokens = 123 and (\n"private-literal"\n)',
        'input_tokens = 123 + (\n"private-literal"\n)',
        'password = fn( // )\n"private-literal"\n)',
        'password = fn( /* ) */\n"private-literal"\n)',
        'password: str = "private-literal"',
        'input_tokens = 123 // comment\n"private-literal"',
        'password = getpass()\n("private-literal")',
        'password = getpass()\n\n("private-literal")',
        'password = getpass()\n// comment\n("private-literal")',
        'password = getpass()\n["private-literal"]',
        'password = getpass()\n+ "private-literal"',
        'password = getpass()\n\ufeff("private-literal")',
        'password = getpass()\n\ufeff\n("private-literal")',
        'password = new\nString("private-literal")',
        'def login(password=getpass()\n or "private-literal"):\n return password',
        'def login(password=getpass()\n if ok else "private-literal"):\n return password',
        'credentials = dict(\n password=getpass()\n or "private-literal"\n)',
        'usage = dict(\n input_tokens=0\n or "private-literal"\n)',
        "input_tokens = dict(password=str(12345678))",
        'input_tokens = dict(password="input_tokens")',
        "input_tokens = dict(secret=True)",
        "let input_tokens = Object({password: String(12345678)});",
        'let input_tokens = Object({password: "input_tokens"});',
        'def login(password=lambda:\n "private-literal"):\n return password()',
        'let password = function()\n{return "private-literal"};',
        'def login(password=getpass() # ;\n or "private-literal"):\n return password',
        'def login(password=getpass() # ,\n if ok else "private-literal"):\n return password',
        'usage = dict(\n input_tokens=0 # ;\n or "private-literal"\n)',
        'usage = dict(\n input_tokens=0 # ,\n or "private-literal"\n)',
    ],
)
def test_prefixed_call_default_and_multiline_literals_do_not_escape(expression):
    assert "private-literal" not in redact_evidence(expression)
    patch = "diff --git a/test.py b/test.py\n--- a/test.py\n+++ b/test.py\n" + "".join(
        "+" + line + "\n" for line in expression.splitlines()
    )
    assert redact_diff(patch) != patch  # refusal, never a usable partial diff
    js_patch = patch.replace("test.py", "test.ts")
    assert redact_diff(js_patch) != js_patch


@pytest.mark.parametrize(
    "expression",
    [
        'password = b"private-literal"',
        'password = getpass("private-literal")',
        'password = "two private-literal words"',
        "\ud800",
    ],
)
async def test_rejected_report_stores_no_secret_fragments_or_invalid_utf8(env, expression):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        worker,
        token,
        ok=True,
        artifact=expression,
        head_sha=BASE,
        changed_files=[],
    )
    assert "private-literal" not in (worker.artifact or "")  # redacted in place, report kept


@pytest.mark.parametrize(
    "expression",
    [
        'password = b"private-literal"',
        'password = r"private-literal"',
        'password = getpass("private-literal")',
        'db_password = os.environ.get("DB_PASSWORD", "private-literal")',
        'password = getpass (\n"private-literal"\n)',
        'password = getpass\t(\n"private-literal"\n)',
        'password = store [\n"private-literal"\n]',
        'password = keyring.get_password (\n"svc", "private-literal"\n)',
        'password = getpass ( # )\n"private-literal"\n)',
        'password = store [ # ]\n"private-literal"\n]',
        'password = call((arg), [\n"private-literal"\n])',
        'input_tokens = 123 and (\n"private-literal"\n)',
        'input_tokens = 123 + (\n"private-literal"\n)',
        'password = fn( // )\n"private-literal"\n)',
        'password = fn( /* ) */\n"private-literal"\n)',
        'password: str = "private-literal"',
        'input_tokens = 123 // comment\n"private-literal"',
        'password = getpass()\n("private-literal")',
        'password = getpass()\n\n("private-literal")',
        'password = getpass()\n// comment\n("private-literal")',
        'password = getpass()\n["private-literal"]',
        'password = getpass()\n+ "private-literal"',
        "password = String(12345678);",
        "password = String(true);",
        'password = getpass()\n\ufeff("private-literal")',
        'password = getpass()\n\ufeff\n("private-literal")',
        'password = new\nString("private-literal")',
        'def login(password=getpass()\n or "private-literal"):\n return password',
        'def login(password=getpass()\n if ok else "private-literal"):\n return password',
        'credentials = dict(\n password=getpass()\n or "private-literal"\n)',
        'usage = dict(\n input_tokens=0\n or "private-literal"\n)',
        "input_tokens = dict(password=str(12345678))",
        'input_tokens = dict(password="input_tokens")',
        "input_tokens = dict(secret=True)",
        "let input_tokens = Object({password: String(12345678)});",
        'let input_tokens = Object({password: "input_tokens"});',
        'def login(password=lambda:\n "private-literal"):\n return password()',
        'let password = function()\n{return "private-literal"};',
        'def login(password=getpass() # ;\n or "private-literal"):\n return password',
        'def login(password=getpass() # ,\n if ok else "private-literal"):\n return password',
        'usage = dict(\n input_tokens=0 # ;\n or "private-literal"\n)',
        'usage = dict(\n input_tokens=0 # ,\n or "private-literal"\n)',
    ],
)
def test_actual_qa_gate_refuses_literals_and_retains_no_fragments(
    tmp_path, monkeypatch, expression
):
    import subprocess

    from tests.test_backlog_independent_regressions import runner

    r = runner()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@invalid")
    (repo / "test.py").write_text("pass\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "test.py").write_text(expression + "\n")
    git("add", ".")
    git("commit", "-qm", "candidate")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(r, "REPO", repo)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    r.qa_evidence(scratch, base, head, cwd=repo)  # flagged, no longer refused (owner 2026-10-21)
    assert json.loads((scratch / "qa-evidence.json").read_text())["evidence_redacted"] is True


@pytest.mark.parametrize(
    "path, source",
    [
        ("case.py", "# password=unquotedPrivate\n"),
        ("case.py", '"""\npassword=unquotedPrivate\nadditional notes\n"""\n'),
        ("case.ts", "// password=unquotedPrivate\n"),
        ("case.ts", "/*\npassword=unquotedPrivate\nadditional notes\n*/\n"),
        ("case.ts", "const doc=`\npassword=unquotedPrivate\nadditional notes\n`;\n"),
    ],
)
@pytest.mark.parametrize("deleted", [False, True])
def test_actual_qa_gate_never_treats_comment_or_string_text_as_references(
    tmp_path, monkeypatch, path, source, deleted
):
    import subprocess

    from tests.test_backlog_independent_regressions import runner

    r = runner()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@invalid")
    (repo / path).write_text(source if deleted else "")
    git("add", path)
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / path).write_text("" if deleted else source)
    git("add", path)
    git("commit", "-qm", "change")
    head = git("rev-parse", "HEAD")
    scratch = tmp_path / "evidence"
    scratch.mkdir()
    monkeypatch.setattr(r, "REPO", repo)
    monkeypatch.setattr(r, "_hidden_host_paths", lambda: [])
    r.qa_evidence(scratch, base, head, cwd=repo)  # flagged, no longer refused (owner 2026-10-21)
    assert json.loads((scratch / "qa-evidence.json").read_text())["evidence_redacted"] is True


async def test_qa_missing_evidence_does_not_spend_code_round(env):
    s, board, task = env
    cycle, _ = await to_qa(s, board, task)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(
        s,
        board,
        cycle,
        qa,
        token,
        ok=True,
        artifact="Worker evidence not available\nCORRECTION_KIND: EVIDENCE",
        head_sha=cycle.integration_sha,
        verdict="fail",
    )
    assert qa.status == "succeeded" and qa.verdict == "fail"
    assert cycle.state == "blocked" and cycle.round == 1 and not cycle.published_sha


async def test_provider_error_quoting_a_host_label_is_still_retried(env):
    # Third independent review: an unanchored match blocked a 503 whose echoed prompt
    # mentioned "storage exhausted"; the parser had correctly kept it retryable.
    s, board, task = env
    cycle, _ = await svc.start_cycle(s, board, task, RANKING)
    attempt, token = await claim(s, cycle)
    await svc.complete_attempt(
        s, board, cycle, attempt, token, ok=False,
        error='Error: HTTP 503 overloaded; request prompt: investigate "storage exhausted"',
    )
    assert cycle.state != "blocked"
    assert any(a.attempt_no == 2 for a in await svc._attempts(s, cycle.id))
