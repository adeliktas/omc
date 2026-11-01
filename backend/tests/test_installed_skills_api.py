"""Owner 2026-10-28: a page listing every installed skill and where it lives, with a Markdown view and editor."""

import hashlib

import pytest
from fastapi import HTTPException

from app.api import installed_skills as api


@pytest.fixture
def roots(monkeypatch, tmp_path):
    skill = tmp_path / "claude/ponytail"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: ponytail\ndescription: "Be lazy."\n---\n# Ponytail\n')
    (skill / "notes.txt").write_text("n")
    (tmp_path / "claude/node_modules/x").mkdir(parents=True)
    (tmp_path / "claude/node_modules/x/SKILL.md").write_text("vendored")
    (tmp_path / "secret.md").write_text("outside")
    monkeypatch.setattr(api, "ROOTS", {"claude": (str(tmp_path / "claude"), "/root/.claude/skills", "Claude"),
                                       "pi": (str(tmp_path / "missing"), "/root/.pi/agent/skills", "Pi")})
    monkeypatch.setattr(api.mc_sidecar, "load_sidecar", lambda: {"models": {"vllm": {"q": {
        "observed_harness": {"skills": ["ponytail:ultra"]}}}}})
    return skill


async def test_lists_skills_with_location_files_and_fleet_use(roots):
    out = await api.list_installed(None)
    assert [r["present"] for r in out["roots"]] == [True, False]
    (skill,) = out["skills"]  # node_modules is pruned
    assert skill["name"] == "ponytail" and skill["description"] == "Be lazy."
    assert skill["host_path"] == "/root/.claude/skills/ponytail"
    assert skill["files"] == ["ponytail/SKILL.md", "ponytail/notes.txt"]
    assert skill["fleet_engines"] == ["vllm/q"]


async def test_reads_and_saves_only_unchanged_files_inside_their_root(roots, monkeypatch):
    calls = []

    async def change(session, member, message, apply, state):
        calls.append(message)
        apply()

    monkeypatch.setattr(api, "audited_admin_change", change)
    admin = type("A", (), {"member": None})()
    got = await api.read_file("claude", "ponytail/SKILL.md", None)
    assert got["sha256"] == hashlib.sha256(got["content"].encode()).hexdigest() and got["editable"]
    write = api.SkillFileWrite(source="claude", path="ponytail/SKILL.md", content="new", expected_sha256=got["sha256"])
    assert (await api.write_file(write, admin, None))["sha256"] == hashlib.sha256(b"new").hexdigest()
    assert (roots / "SKILL.md").read_text() == "new" and len(calls) == 1
    with pytest.raises(HTTPException) as stale:  # saved against the old content
        await api.write_file(write, admin, None)
    assert stale.value.status_code == 409
    for source, path in (("claude", "../secret.md"), ("nope", "x.md"), ("claude", "ponytail/missing.md")):
        with pytest.raises(HTTPException) as refused:
            await api.read_file(source, path, None)
        assert refused.value.status_code == 404
    assert (roots.parent.parent / "secret.md").read_text() == "outside" and len(calls) == 1
