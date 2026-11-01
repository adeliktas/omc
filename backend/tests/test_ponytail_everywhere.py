"""Owner 2026-10-26: every agent and model runs ponytail ultra, and /models shows what each loaded.
Isolated harnesses (no setting sources, no host extensions, the private-board worker) loaded none, so the
runner prepends ponytail's own ruleset to every harness prompt and reports it with the run's usage."""

from app.services import team_cycle as svc
from tests.test_backlog_independent_regressions import runner
from tests.test_team_cycle import RANKING, env  # noqa: F401  (fixture)


def test_every_harness_prompt_carries_the_ruleset_and_the_run_reports_it(monkeypatch, tmp_path):
    r = runner()
    monkeypatch.setattr(r, "PONYTAIL_LEVEL", "ultra")
    monkeypatch.setattr(r, "PONYTAIL_RULES", "PONYTAIL MODE ACTIVE — level: ultra\nrules")
    monkeypatch.setattr(r, "superpowers", lambda *a: [])  # role skills: see the test below
    seen, posted = [], []
    monkeypatch.setattr(r, "harness_cmd", lambda provider, model, thinking, prompt, *a: seen.append(prompt))
    monkeypatch.setattr(r, "api", lambda method, path, body=None: posted.append((path, body)) or {})
    claim = {"role_kind": "developer", "attempt": {"id": "a", "provider": "pi-neuralwatt", "model": "glm-5.3",
                                                   "thinking_level": "low"}}
    r._run_harness(claim, "brief", tmp_path, tmp_path, 30)
    assert seen == ["PONYTAIL MODE ACTIVE — level: ultra\nrules\n\n---\n\nbrief"]
    usage = next(body for path, body in posted if path == "/api/v1/ranking/usage")
    assert usage["harness"] == {"skills": ["ponytail:ultra"], "mcp": []}


def test_the_shipped_ruleset_loads_at_the_configured_level():
    r = runner()
    if r.PONYTAIL_RULES:  # hosts without ponytail installed run without it (level "off")
        assert r.PONYTAIL_RULES.startswith(f"PONYTAIL MODE ACTIVE — level: {r.PONYTAIL_LEVEL}")


async def test_reported_skills_show_per_model_and_per_role(env, monkeypatch, tmp_path):  # noqa: F811
    from app.api.model_ranking import get_overview
    from app.services import mc_sidecar, model_ranking

    monkeypatch.setattr(mc_sidecar, "_SIDECAR_PATH", tmp_path / "openclaw.mc.json")
    session, board, task = env
    cycle, _ = await svc.start_cycle(session, board, task, RANKING)
    await session.commit()
    pm = (await svc._attempts(session, cycle.id))[0]
    model_ranking.record_usage(pm.provider, pm.model, {"success": True, "harness": {"skills": ["ponytail:ultra"], "mcp": []}})
    seen = mc_sidecar.get_model_meta(pm.provider, pm.model)["observed_harness"]
    assert seen["skills"] == ["ponytail:ultra"] and seen["mcp"] == [] and seen["at"]
    roles = {r["slug"]: r for r in (await get_overview(db=session, _actor=None))["roles"]}
    assert roles["project-manager"]["harness"] == {"skills": ["ponytail:ultra"], "mcp": []}


def test_implementers_get_a_superpowers_index_and_planners_none(monkeypatch, tmp_path):
    """Owner 2026-10-28: a superpowers subset per role, indexed in the prompt, files mounted read-only."""
    import mc_skills

    r = runner()
    for name in ("test-driven-development", "systematic-debugging", "verification-before-completion"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f'---\nname: {name}\ndescription: "Use for {name}."\n---\nbody\n')
    monkeypatch.setattr(mc_skills, "SUPERPOWERS", tmp_path)  # receiving-code-review not installed: skipped
    monkeypatch.setattr(r, "PONYTAIL_LEVEL", "ultra")
    monkeypatch.setattr(r, "PONYTAIL_RULES", "PONYTAIL")
    seen, posted = [], []
    monkeypatch.setattr(r, "harness_cmd", lambda provider, model, thinking, prompt, *a: seen.append(prompt))
    monkeypatch.setattr(r, "api", lambda method, path, body=None: posted.append((path, body)) or {})
    attempt = {"id": "a", "provider": "pi-neuralwatt", "model": "glm-5.3", "thinking_level": "low"}
    r._run_harness({"role_kind": "developer", "capability": "write_worktree", "attempt": attempt}, "brief", tmp_path, tmp_path, 30)
    r._run_harness({"role_kind": "pm", "attempt": attempt}, "plan", tmp_path, tmp_path, 30)
    implement, plan = seen
    assert implement.startswith("PONYTAIL\n\n---\n\nSKILLS (superpowers).") and implement.endswith("\n\n---\n\nbrief")
    assert f"- test-driven-development: Use for test-driven-development. Read {mc_skills.SKILLS_MOUNT}/test-driven-development/SKILL.md" in implement
    assert "receiving-code-review" not in implement
    assert plan == "PONYTAIL\n\n---\n\nplan"
    reports = [body["harness"]["skills"] for path, body in posted if path == "/api/v1/ranking/usage"]
    assert reports == [["ponytail:ultra", "superpowers:test-driven-development", "superpowers:systematic-debugging",
                        "superpowers:verification-before-completion"], ["ponytail:ultra"]]
