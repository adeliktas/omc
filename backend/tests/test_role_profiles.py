"""Role-profile registry: same role -> identical well-defined markdown."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.models.agents import Agent
import app.services.openclaw.provisioning as provisioning
from app.services.openclaw.constants import BOARD_SHARED_TEMPLATE_MAP

ROLES_ROOT = provisioning._templates_root() / "roles"
_SHARED_MAP = dict(BOARD_SHARED_TEMPLATE_MAP)

# Free-text roles observed in the live agents table must all resolve to a
# canonical registry slug (or "" for empty input).
CANONICAL_ROLES = {
    "": "",
    "Generalist": "generalist",
    "Reverse Engineer": "reverse-engineer",
    "Senior Reverse Engineer": "reverse-engineer",
    "RE Lead": "re-laboratory-lead",
    "RE Laboratory Lead": "re-laboratory-lead",
    "Reverse Engineer Alpha": "reverse-engineer",
    "Reverse Engineer Beta": "reverse-engineer",
    "RE Lead (vLLM)": "re-laboratory-lead",
    "Reverse Engineer (vLLM)": "reverse-engineer",
    "Binary Analyst": "binary-analyst",
    "Binary Analyst (vLLM)": "binary-analyst",
    "Red Team Operator": "red-team-operator",
    "Red Team Operator (vLLM)": "red-team-operator",
    "DevOps Engineer": "devops-engineer",
    "Senior DevOps Engineer": "devops-engineer",
    "DevOps Engineer (vLLM)": "devops-engineer",
    "QA Engineer": "qa-engineer",
    "Senior QA Engineer": "qa-engineer",
    "QA Engineer (vLLM)": "qa-engineer",
    "Security Architect": "security-architect",
    "Security Architect (vLLM)": "security-architect",
    "Software Architect": "software-architect",
    "Senior Software Architect": "software-architect",
    "Project Manager": "project-manager",
    "Project Manager Senior": "project-manager",
    "Scrum Master": "scrum-master",
    "Scrum Master Senior": "scrum-master",
    "R&D Laboratory Director": "r-d-laboratory-director",
    "Gateway Agent": "gateway-agent",
    "Software Architect (Ollama Clone — qwen3-coder)": "software-architect",
}


def _make_agent(
    *,
    name: str,
    role: str,
    identity_template: str | None = None,
    soul_template: str | None = None,
) -> tuple[Agent, dict[str, str]]:
    agent = Agent(
        id=uuid4(),
        gateway_id=uuid4(),
        name=name,
        status="online",
        identity_profile={"role": role},
        identity_template=identity_template,
        soul_template=soul_template,
    )
    context = {
        "agent_name": name,
        "agent_id": str(agent.id),
        "identity_role": role,
        "identity_communication_style": "direct, concise, practical",
        "identity_emoji": ":gear:",
        "identity_purpose": "",
        "identity_personality": "",
        "identity_custom_instructions": "",
        "is_board_lead": "false",
    }
    return agent, context


def test_free_text_roles_normalize_to_canonical_slugs() -> None:
    for role, expected in CANONICAL_ROLES.items():
        assert provisioning.canonical_role_slug(role) == expected, role


def test_registry_has_both_files_for_every_canonical_role() -> None:
    # "generalist" deliberately has no registry entry: the shared BOARD_*
    # templates ARE the generalist profile (registry lookup misses, falls back).
    slugs = {v for v in CANONICAL_ROLES.values() if v} - {"generalist"}
    for slug in sorted(slugs):
        for file_name in ("IDENTITY.md.j2", "SOUL.md.j2"):
            path = ROLES_ROOT / slug / file_name
            assert path.is_file(), f"missing role profile: {path}"


def test_same_role_renders_identical_profile_markdown() -> None:
    agent_a, context_a = _make_agent(name="RE Lead-Alpha", role="Reverse Engineer")
    agent_b, context_b = _make_agent(name="RE Lead-Beta", role="Senior Reverse Engineer")
    files = {"IDENTITY.md", "SOUL.md"}

    rendered_a = provisioning._render_agent_files(
        context_a, agent_a, files, include_bootstrap=False, template_overrides=dict(_SHARED_MAP)
    )
    rendered_b = provisioning._render_agent_files(
        context_b, agent_b, files, include_bootstrap=False, template_overrides=dict(_SHARED_MAP)
    )

    assert "Reverse Engineer" in rendered_a["IDENTITY.md"]
    assert "RE Lead-Alpha" in rendered_a["IDENTITY.md"]
    # Same role => identical profile, except per-agent name/ID header.
    norm_a = rendered_a["IDENTITY.md"].replace("RE Lead-Alpha", "X").replace(context_a["agent_id"], "ID")
    norm_b = rendered_b["IDENTITY.md"].replace("RE Lead-Beta", "X").replace(context_b["agent_id"], "ID")
    assert norm_a == norm_b
    assert rendered_a["SOUL.md"] == rendered_b["SOUL.md"]


def test_unknown_role_falls_back_to_shared_templates() -> None:
    agent, context = _make_agent(name="Mystery", role="Totally Unknown Role 99")
    rendered = provisioning._render_agent_files(
        context, agent, {"IDENTITY.md", "SOUL.md"}, include_bootstrap=False, template_overrides=dict(_SHARED_MAP)
    )
    assert "Mystery" in rendered["IDENTITY.md"]
    assert "Totally Unknown Role 99" in rendered["IDENTITY.md"]
    assert "not a chatbot" in rendered["SOUL.md"].lower()


def test_per_agent_override_wins_over_role_profile() -> None:
    agent, context = _make_agent(
        name="Bespoke",
        role="Reverse Engineer",
        soul_template="# SOUL.md\n\nBespoke soul wins.",
    )
    rendered = provisioning._render_agent_files(
        context, agent, {"IDENTITY.md", "SOUL.md"}, include_bootstrap=False, template_overrides=dict(_SHARED_MAP)
    )
    assert rendered["SOUL.md"] == "# SOUL.md\n\nBespoke soul wins."
    assert "Reverse Engineer" in rendered["IDENTITY.md"]  # identity still from registry


def test_registry_files_render_as_jinja_with_context() -> None:
    agent, context = _make_agent(name="QA Bot", role="QA Engineer")
    rendered = provisioning._render_agent_files(
        context, agent, {"IDENTITY.md", "SOUL.md"}, include_bootstrap=False, template_overrides=dict(_SHARED_MAP)
    )
    assert rendered["IDENTITY.md"].startswith("# IDENTITY.md")
    assert "QA Bot" in rendered["IDENTITY.md"]
    assert "{%" not in rendered["SOUL.md"]  # fully rendered, no leaked tags