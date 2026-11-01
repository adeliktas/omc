"""Probes a worker left under /tmp reach QA verbatim, vetted file by file."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from app.services import team_cycle as svc


def test_clean_files_are_attached_verbatim_with_digest():
    probe = "import os\nos.environ['LOCAL_AUTH_TOKEN'] = 'fixture'\nprint('ok ```')\n"
    out = svc.evidence_appendix({"t/probe.py": probe})
    digest = hashlib.sha256(probe.encode()).hexdigest()
    assert f"### /tmp/t/probe.py — {len(probe)} bytes, sha256 {digest}" in out
    assert f"````\n{probe}\n````" in out  # fence longer than any backtick run inside
    assert "Not attached" not in out


def test_unsafe_or_oversized_files_are_listed_never_partially_attached():
    files = {
        "env.sh": "LOCAL_AUTH_TOKEN=test-only-local-token-0123456789abcdef\n",
        "big.py": "x" * (svc.MAX_EVIDENCE_BYTES + 1),
        "../escape.py": "print(1)\n",
        "ok.py": "print(1)\n",
    }
    out = svc.evidence_appendix(files)
    assert "### /tmp/ok.py" in out
    assert "/tmp/env.sh: " in out and "needs credential redaction" in out
    assert "test-only-local-token" not in out
    assert f"/tmp/big.py: {svc.MAX_EVIDENCE_BYTES + 1} bytes, sha256 " in out
    assert "over the 64 KiB evidence budget" in out and "xxxx" not in out
    assert "(invalid name)" in out and "escape" not in out


def test_file_limit_and_empty_input():
    files = {f"p{i:02}.py": "print(1)\n" for i in range(svc.MAX_EVIDENCE_FILES + 2)}
    out = svc.evidence_appendix(files)
    assert out.count("### /tmp/") == svc.MAX_EVIDENCE_FILES
    assert "- 2 more file(s) not attached (over the file limit)" in out
    assert svc.evidence_appendix(None) == svc.evidence_appendix({}) == ""


def test_invalid_utf8_is_listed_not_attached():
    out = svc.evidence_appendix({"bad.py": "x = '\ud800'\n", "ok.py": "print(1)\n"})
    assert "/tmp/bad.py: 10 bytes, sha256 " in out and "not valid UTF-8" in out
    assert "### /tmp/ok.py" in out


def test_omission_diagnostics_are_included_in_the_exact_appendix_bound():
    files = {"first.md": "x" * 65000}
    # Fill the actual representation exactly; byte-count labels stay five digits.
    files["first.md"] += "x" * (svc.MAX_EVIDENCE_BYTES - len(svc.evidence_appendix(files).encode()))
    assert len(svc.evidence_appendix(files).encode()) == svc.MAX_EVIDENCE_BYTES
    files.update({f"p{i:02}.md": "x" * 65536 for i in range(17)})
    attached = {"stale"}
    out = svc.evidence_appendix(files, attached=attached)
    assert 0 < len(out.encode()) <= svc.MAX_EVIDENCE_BYTES
    assert not attached and "### /tmp/" not in out
    assert "/tmp/first.md:" in out and "- 2 more file(s) not attached" in out


def test_worst_case_appendix_fits_and_reaches_qa_with_a_full_report():
    big = {f"{'n' * 150}{i:02}.py": "y = 1\n" * 2000 for i in range(svc.MAX_EVIDENCE_FILES + 5)}
    out = svc.evidence_appendix(big)
    assert 0 < len(out.encode()) <= svc.MAX_EVIDENCE_BYTES
    fitting = {f"p{i}.py": "z = 2\n" * 1700 for i in range(7)}  # ~10 KiB each
    appendix = svc.evidence_appendix(fitting)
    assert appendix.count("### /tmp/") == 6 and "over the 64 KiB evidence budget" in appendix
    report = "r" * svc.MAX_ARTIFACT_BYTES
    cycle = SimpleNamespace(id=uuid4(), board_id=uuid4(), task_id=uuid4(), round=1)
    item = SimpleNamespace(
        cycle_id=cycle.id,
        board_id=cycle.board_id,
        task_id=cycle.task_id,
        round=1,
        role_kind="developer",
        stage_key="dev-main",
        attempt_no=1,
        status="succeeded",
        assignment=json.dumps({"artifact_complete": True}),
        artifact=report + appendix,
        id=uuid4(),
        member_id=uuid4(),
        base_sha="b",
        head_sha="h",
    )
    bundle, problem = svc.worker_evidence(cycle, [item])
    assert problem is None and "### /tmp/p0.py" in bundle
