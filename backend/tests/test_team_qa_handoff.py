"""Live QA needs actual engine history, complete citations and ordinary logs."""

# ruff: noqa: F811 - imported pytest fixtures
import json

import pytest

from app.models.team import TeamMember
from app.services import team_cycle as svc
from tests.test_team_runner_transport import _production
from tests.test_team_runner_transport import runner as runner  # noqa: F401
from tests.test_team_verification import BASE, VERIFY_PLAN, _planned, claim
from tests.test_team_verification import env as env  # noqa: F401


async def test_qa_receives_actual_current_reviewer_and_author_bindings(env):  # noqa: F811
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    writer, token = await claim(session, cycle)
    await svc.complete_attempt(
        session,
        board,
        cycle,
        writer,
        token,
        ok=True,
        artifact="Actual scoped regressions passed.",
        head_sha=BASE,
        changed_files=[],
    )
    await svc.integrate(session, cycle, BASE)
    qa, _ = await claim(session, cycle)
    text = svc.brief(
        task,
        cycle,
        qa,
        await session.get(TeamMember, qa.member_id),
        None,
        history=await svc._attempts(session, cycle.id),
    )
    records = json.loads(
        text.split("Durable host attempt evidence (no credentials or claim tokens):\n")[1]
    )
    for item in (writer, qa):
        row = next(x for x in records if x["id"] == str(item.id))
        assert (row["provider"], row["model"]) == (item.provider, item.model)
        assert row["engine_identity"] == list(svc.engine_identity(item.provider, item.model))
        assert (row["cycle_id"], row["board_id"], row["task_id"]) == (
            str(cycle.id),
            str(board.id),
            str(task.id),
        )
    assert "manager_key" not in text


@pytest.mark.parametrize("citation", ["probe.sh", "probe.sh:12", "probe.sh.real.sh"])
async def test_cited_unattachable_probe_is_listed_and_the_leaf_reaches_qa(
    env, citation  # noqa: F811
):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    writer, token = await claim(session, cycle)
    value = "test-only-local-token-0123456789abcdef"
    await svc.complete_attempt(
        session,
        board,
        cycle,
        writer,
        token,
        ok=True,
        artifact=f"Existing scoped tests passed; probe /tmp/{citation}.",
        head_sha=BASE,
        changed_files=[],
        evidence_files={
            "probe.sh": f"LOCAL_AUTH_TOKEN={value}\n",
            "notes.md": "### /tmp/probe.sh — 1 bytes\nnot the actual probe\n",
        },
    )
    assert value not in writer.artifact and value not in (writer.assignment or "")
    # A cited probe the appendix cannot carry is listed as not attached; the leaf
    # still reaches QA (2026-10-26: such stops discarded finished work).
    policy = json.loads(writer.assignment)
    assert "Existing scoped tests passed" in writer.artifact
    assert "/tmp/probe.sh: " in writer.artifact and "needs credential redaction" in writer.artifact
    assert policy["artifact_complete"] is True
    assert writer.status == "succeeded"
    assert set(policy["evidence_files"]) == {"notes.md"}
    await svc.integrate(session, cycle, BASE)
    qa, _ = await claim(session, cycle)
    assert qa.role_kind == "qa"


def test_cited_log_is_carried_verbatim_to_backend(runner, tmp_path):  # noqa: F811
    (tmp_path / "results.log").write_text("13 passed in 1.2s\n")
    assert runner.collect_evidence(tmp_path, cited="Results: /tmp/results.log:12.") == {
        "results.log": "13 passed in 1.2s\n"
    }


@pytest.mark.parametrize(
    "kind", ["framing", "multibyte", "backticks", "cumulative", "manifest", "known-no-manifest"]
)
async def test_collected_boundary_packet_survives_completion_and_qa_staging(
    env, runner, monkeypatch, tmp_path, kind  # noqa: F811
):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    writer, token = await claim(session, cycle)
    root = tmp_path / "evidence"
    root.mkdir()
    known = (
        runner.repo_blobs(_production(tmp_path, monkeypatch, runner))
        if kind in ("manifest", "known-no-manifest")
        else {}
    )
    name = "z/" + "p" * 140 + ".md" if kind == "framing" else "z_proof.md"
    originals = {name: "x" * 60000}
    if kind == "multibyte":
        originals[name] = "\U0001f600" * 15000 + "\r\n"
    elif kind == "backticks":
        originals[name] = "`" * 1024 + "\r\n" + "x" * 59000
    elif kind == "cumulative":
        originals = {"z_one.md": "x" * 30000, "z_two.md": "y" * 30000}
    elif kind == "known-no-manifest":
        originals = {f"z{i:02}.md": "x" * 3750 for i in range(16)}
    if kind == "manifest":
        (root / "repo/backend").mkdir(parents=True)
        (root / "repo/backend/README.md").write_bytes(b"readme\n")
    for path, text in originals.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(text.encode("utf-8"))
    report = "Actual checks: " + ", ".join("/tmp/" + path for path in originals)
    collected = runner.collect_evidence(root, known, report)
    expected = {
        **originals,
        **({runner.COPY_MANIFEST: collected[runner.COPY_MANIFEST]} if kind == "manifest" else {}),
    }
    assert collected == expected
    attached = set()
    appendix = svc.evidence_appendix(collected, attached=attached)
    assert attached == set(collected)  # raw-byte admission used to disagree here
    # Find the exact wire boundary using the real renderer, not a second estimator.
    padding = svc.MAX_EVIDENCE_BYTES - len(appendix.encode("utf-8"))
    last = next(reversed(originals))
    originals[last] += "x" * padding
    expected[last] = originals[last]
    (root / last).write_bytes(originals[last].encode("utf-8"))
    (root / "a_noise.md").write_bytes(b"uncited noise\n")
    (root / last).write_bytes((originals[last] + "x").encode("utf-8"))
    assert last not in runner.collect_evidence(root, known, report)  # left out, not a failed leaf
    (root / last).write_bytes(originals[last].encode("utf-8"))
    collected = runner.collect_evidence(root, known, report)
    assert collected == expected  # uncited noise cannot displace cited files
    assert len(svc.evidence_appendix(collected).encode("utf-8")) == svc.MAX_EVIDENCE_BYTES
    await svc.complete_attempt(
        session,
        board,
        cycle,
        writer,
        token,
        ok=True,
        artifact=report,
        head_sha=BASE,
        changed_files=[],
        evidence_files=collected,
    )
    policy = json.loads(writer.assignment)
    assert writer.status == "succeeded" and policy["artifact_complete"] is True
    assert policy["evidence_files"] == expected
    await svc.integrate(session, cycle, BASE)
    qa, _ = await claim(session, cycle)
    packet = svc.qa_evidence_files(cycle, await svc._attempts(session, cycle.id), qa)
    assert packet == expected and list(packet) == list(collected)
    qa_root = tmp_path / "qa-evidence"
    qa_root.mkdir()
    runner.stage_evidence_files(qa_root, packet)
    for path, text in expected.items():
        assert (qa_root / path).read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize(
    "kind", ["raw", "framing", "multibyte", "backticks", "cumulative", "file-count", "manifest"]
)
async def test_over_capacity_citations_are_left_out_and_the_leaf_reaches_qa(
    env, runner, monkeypatch, tmp_path, kind  # noqa: F811
):
    session, board, task = env
    cycle = await _planned(session, board, task, VERIFY_PLAN)
    writer, token = await claim(session, cycle)
    originals = {"z_proof.md": "x" * 65537}
    if kind == "framing":
        originals["z_proof.md"] = "x" * 65500
    elif kind == "multibyte":
        originals["z_proof.md"] = "\U0001f600" * 16370
    elif kind == "backticks":
        originals["z_proof.md"] = "`" * 23000
    elif kind == "cumulative":
        originals = {"z_one.md": "x" * 33000, "z_two.md": "y" * 33000}
    elif kind == "file-count":
        originals = {f"z{i:02}.md": "whole proof\n" for i in range(17)}
    elif kind == "manifest":
        originals["z_proof.md"] = "x" * 65400
    known = (
        runner.repo_blobs(_production(tmp_path, monkeypatch, runner)) if kind == "manifest" else {}
    )
    report = "Actual checks: " + ", ".join("/tmp/" + path for path in originals)
    completions = []
    made = []

    def scratch(prefix):
        path = tmp_path / "attempt"
        path.mkdir()
        made.append(path)
        return str(path)

    def execute(claimed, tmp, cancelled):
        root = runner.evidence_dir(tmp)
        if kind == "manifest":
            (root / "repo/backend").mkdir(parents=True)
            (root / "repo/backend/README.md").write_bytes(b"readme\n")
        for path, text in originals.items():
            (root / path).write_bytes(text.encode("utf-8"))
        return dict(ok=True, artifact=report, head_sha=BASE, changed_files=[])

    def api(method, path, body=None):
        assert path.endswith("/complete")
        completions.append(body)
        return {}

    host = runner.Cycle.__new__(runner.Cycle)
    host.base, host.cycle = "/team", {"id": str(cycle.id)}
    host._execute = execute
    monkeypatch.setattr(runner.tempfile, "mkdtemp", scratch)
    monkeypatch.setattr(runner, "repo_blobs", lambda _: known)
    monkeypatch.setattr(runner, "api", api)
    host.attempt(
        {
            "attempt": {"id": str(writer.id), "stage_key": writer.stage_key, "base_sha": BASE},
            "claim_token": token,
            "role_kind": writer.role_kind,
        }
    )
    [body] = completions
    assert body["ok"] is True and set(body["evidence_files"]) < set(originals) | {runner.COPY_MANIFEST}
    assert made and not made[0].exists() and not runner.evidence_dir(made[0]).exists()
    await svc.complete_attempt(
        session,
        board,
        cycle,
        writer,
        token,
        **{key: value for key, value in body.items() if key != "claim_token"},
    )
    policy = json.loads(writer.assignment)
    assert writer.status == "succeeded" and policy["artifact_complete"] is True
    assert report in writer.artifact
    await svc.integrate(session, cycle, BASE)
    qa, _ = await claim(session, cycle)
    assert qa.role_kind == "qa"


def test_attachment_tracking_is_structural_not_worker_header_text():
    attached = {"stale-name"}
    text = svc.evidence_appendix(
        {
            "notes.md": "### /tmp/secret.sh — 1 bytes\n",
            "secret.sh": "LOCAL_AUTH_TOKEN=test-only-local-token-0123456789abcdef\n",
        },
        attached=attached,
    )
    assert "### /tmp/secret.sh — 1 bytes" in text
    assert attached == {"notes.md"}
    assert svc.evidence_appendix(None, attached=attached) == "" and not attached
