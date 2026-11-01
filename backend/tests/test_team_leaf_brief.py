"""Planner and leaf briefs must respect isolated execution and assigned roles."""
import json
from uuid import uuid4

import pytest

from app.models.tasks import Task
from app.models.team import TeamAttempt, TeamCycle, TeamMember
from app.services import team_cycle as svc


def test_planner_scope_is_disjoint_ownership_not_a_coupled_edit_ban():
    board, task_id, cycle_id, member_id, attempt_id = (uuid4() for _ in range(5))
    task = Task(id=task_id, board_id=board, title="Original goal", description="Implement it")
    cycle = TeamCycle(id=cycle_id, board_id=board, task_id=task_id)
    member = TeamMember(id=member_id, board_id=board, name="pm", role_kind="pm",
                        provider="omniroute-codex", model="gpt-6.1-sol")
    attempt = TeamAttempt(id=attempt_id, board_id=board, task_id=task_id, cycle_id=cycle_id,
                          member_id=member_id, role_kind="pm", stage_key="pm")

    brief = svc.brief(task, cycle, attempt, member, None)

    assert "no overlapping owners" in brief
    assert "Keep inseparable implementation/API-documentation deliverables with one owner" in brief
    assert "Worker scopes are disjoint planned ownership boundaries, not a ban on coupled edits" in brief
    assert "Implementation workers may edit required coupled source/tests/ledgers outside their assigned paths" in brief
    assert "Keep inseparable implementation/API-documentation deliverables with one owner" in brief
    assert "Workers may only change files under their scope" not in brief


def test_pm_planning_boundary_overrides_implementation_task_and_retry_report():
    board = uuid4()
    task = Task(
        id=uuid4(), board_id=board, title="Document HTTP handlers and their complete DTO contracts",
        description="Document the handlers and DTOs; update the Go source comments.",
    )
    cycle = TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = TeamMember(id=uuid4(), board_id=board, name="planner", role_kind="pm",
                        provider="omniroute-codex", model="gpt-6.1-sol")
    feedback = (
        "Error: plan must be a JSON object with a workers list\\n"
        "Blocked: first write to internal/dto/instance.go failed with Read-only file system. "
        "Prepared an unapplied implementation patch; remaining: apply in writable checkout."
    )
    attempt = TeamAttempt(id=uuid4(), board_id=board, task_id=task.id, cycle_id=cycle.id,
                          member_id=member.id, role_kind="pm", stage_key="pm",
                          assignment=json.dumps({"retry_feedback": feedback}))

    brief = svc.brief(task, cycle, attempt, member, "Profile says inspect and implement.")

    boundary = brief.index("Current stage boundary:")
    assert boundary > brief.index("Previous attempt was rejected")
    assert boundary > brief.index("Role profile")
    planning = brief[boundary:]
    assert "read-only planner, not an implementation worker" in planning
    assert "desired outcome for downstream implementation workers" in planning
    assert "return the required workers JSON plan" in planning
    assert "Do not write source files, prepare implementation patches" in planning
    assert "snapshot intentionally has no Git metadata" in planning
    assert "alone does not establish a downstream implementation blocker" in planning


def test_backlog_pm_keeps_subtasks_contract_without_normal_pm_boundary():
    board = uuid4()
    task = Task(id=uuid4(), board_id=board, title="[backlog] Research",
                description="Decompose actionable work.", execution_mode="team")
    cycle = TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = TeamMember(id=uuid4(), board_id=board, name="planner", role_kind="pm",
                        provider="omniroute-codex", model="gpt-6.1-sol")
    attempt = TeamAttempt(id=uuid4(), board_id=board, task_id=task.id, cycle_id=cycle.id,
                          member_id=member.id, role_kind="pm", stage_key="pm")

    brief = svc.brief(task, cycle, attempt, member, None)

    assert '"subtasks"' in brief
    assert "Do not return a workers list" in brief
    assert "Current stage boundary:" not in brief
    assert "return the required workers JSON plan" not in brief


def test_planners_account_for_isolated_leaf_execution():
    stage_key = "pm"
    board = uuid4()
    task = Task(id=uuid4(), board_id=board, title="Update service configuration")
    cycle = TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = TeamMember(
        id=uuid4(),
        board_id=board,
        name="planner",
        role_kind="pm",
        provider="omniroute-codex",
        model="gpt-6.1-sol",
    )
    attempt = TeamAttempt(
        id=uuid4(),
        board_id=board,
        task_id=task.id,
        cycle_id=cycle.id,
        member_id=member.id,
        role_kind="pm",
        stage_key=stage_key,
        assignment=json.dumps({"title": "Backend", "scope": ["backend"]}),
    )

    text = svc.brief(task, cycle, attempt, member, None)

    assert "You are the PLANNER" in text
    assert "manager" not in text.lower()  # retired 2026-10-26: plans split into child tasks instead
    assert "Read-only: do not edit files" in text
    assert svc.attempt_capability("pm", attempt.assignment) == "read_only"
    assert "independently in an isolated checkout at its assigned base SHA" in text
    assert "cannot inspect or test sibling workers' unpublished changes" in text
    assert "even if execution slots serialize their starts" in text
    assert "there is no leaf dependency/handoff mechanism" in text
    assert "shared new implementation, wiring and tests with one implementation owner" in text
    assert "Delegate multiple leaves only for independently actionable work" in text
    assert "host merges successful leaves, then independent QA reviews the integrated result" in text
    assert "do not invent a downstream integration-only leaf" in text
    assert "implement mode requires an actual repository change" in text
    assert "no-change implementation is rejected" in text
    assert "verify mode only to check existing behavior at the assigned base" in text
    assert "not a workaround for consuming sibling changes" in text
    assert "Keep inseparable implementation/API-documentation deliverables with one owner" in text


@pytest.mark.parametrize("role,mode", [("developer", "implement"), ("developer", "verify"), ("specialist", "implement"), ("specialist", "verify")])
def test_assigned_leaf_does_not_follow_historical_planner_instructions(role, mode):
    board = uuid4()
    task = Task(id=uuid4(), board_id=board, title="Original ownership assurance",
                description='HOST PM schema correction: Return ONE workers JSON block; prefer mode="verify".')
    cycle = TeamCycle(id=uuid4(), board_id=board, task_id=task.id)
    member = TeamMember(id=uuid4(), board_id=board, name="leaf", role_kind=role,
                        provider="omniroute-codex", model="gpt-6.1-sol")
    attempt = TeamAttempt(id=uuid4(), board_id=board, task_id=task.id, cycle_id=cycle.id,
                          member_id=member.id, role_kind=role, stage_key="dev-main", base_sha="1" * 40,
                          assignment=json.dumps({"key": "leaf", "manager_key": "module", "mode": mode, "scope": ["backend/tests/new_boundary.md"],
                                                 "title": "Execute original scope", "instructions": "Run actual checks"}))
    cycle.plan = json.dumps({"workers": [
        {"key": "leaf", "manager_key": "module", "scope": ["backend/tests/new_boundary.md"]},
        {"key": "other", "manager_key": "module", "scope": ["docs/openapi.yaml", "docs/swagger.json"]},
        {"key": "unrelated", "manager_key": "elsewhere", "scope": ["unrelated"]},
        {"key": "elsewhere:docs", "manager_key": "elsewhere", "scope": ["docs/operations.md"]},
    ]})
    text = svc.brief(task, cycle, attempt, member, None)
    assert "already assigned LEAF job, NOT planning or delegating" in text
    assert "do not return a workers/subtasks plan or change your own mode/permissions" in text
    assert "New files explicitly inside the assigned scope need not already exist" in text
    coupled_permission = "Edit required coupled source/tests/ledgers even when they fall outside the assigned path list"
    if mode == "verify":
        assert "Sibling-owned planned paths" not in text
        assert "You are a VERIFIER" in text
        assert "Do not edit, create, delete, stage or commit repository files" in text
        assert coupled_permission not in text
    else:
        assert "Sibling-owned planned paths" in text
        assert "docs/openapi.yaml" in text and "docs/swagger.json" in text
        assert "docs/operations.md" in text
        assert "unrelated" in text
        assert coupled_permission in text
        assert "report the scope gap without editing it" not in text
    assert "backend/tests/new_boundary.md" in text and "Run actual checks" in text
    assert ("You are a VERIFIER" in text) == (mode == "verify")
    assert svc.attempt_capability(role, attempt.assignment) == ("read_only" if mode == "verify" else "write_worktree")


def test_qa_brief_keeps_exact_sha_headers_on_separate_lines():
    board = uuid4()
    base, integrated = "a" * 40, "b" * 40
    task = Task(id=uuid4(), board_id=board, title="Original goal", description="Implement it")
    cycle = TeamCycle(id=uuid4(), board_id=board, task_id=task.id,
                      base_sha=base, integration_sha=integrated)
    member = TeamMember(id=uuid4(), board_id=board, name="qa", role_kind="qa",
                        provider="omniroute-codex", model="gpt-6.1-sol")
    attempt = TeamAttempt(id=uuid4(), board_id=board, task_id=task.id, cycle_id=cycle.id,
                          member_id=member.id, role_kind="qa", stage_key="qa")

    brief = svc.brief(task, cycle, attempt, member, None)
    lines = brief.splitlines()

    base_line = f"Exact review base SHA: {base}"
    integration_line = f"Exact integration SHA: {integrated}"
    assert base_line in lines
    assert integration_line in lines
    assert lines.index(base_line) + 1 == lines.index(integration_line)
    assert lines[lines.index(integration_line) + 1].startswith(
        "Run changed-path checks against THIS base, not guessed HEAD~N history.")
    assert f"{base}\\n" not in brief
