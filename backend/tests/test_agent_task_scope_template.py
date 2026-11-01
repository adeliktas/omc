"""Rendered discovery works without startup writes that contradict a one-file task scope."""
import json
import re
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


@pytest.mark.parametrize("role", ["agent-main", "agent-lead", "agent-worker"])
def test_agent_discovery_is_read_only_and_filters_role(role):
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    context = dict(
        is_main_agent=role == "agent-main", is_board_lead=role == "agent-lead",
        base_url="http://synthetic.invalid", auth_token="synthetic-only", agent_name="Example",
        agent_id="example", board_id="example-board", workspace_root="/synthetic", workspace_path="/synthetic", user_timezone="UTC",
        board_rule_require_review_before_done=True, board_rule_require_approval_for_done=True,
        board_rule_comment_required_for_review=True, board_rule_block_status_changes_with_pending_approval=True,
        board_rule_only_lead_can_change_status=False, board_rule_max_agents=3,
    )
    text = env.get_template("BOARD_AGENTS.md.j2").render(context)
    block = text.split("## OpenAPI discovery", 1)[1].split("## API source of truth", 1)[0]
    assert "-o " not in block and "mkdir" not in block and ">" not in block
    expression = re.search(r"jq -r --arg role \"([^\"]+)\" '([\s\S]*?)'", block)
    assert expression and expression[1] == role
    source = {"paths": {"/correct": {"parameters": [{"name": "q"}], "summary": "path metadata", "get": {"tags": [role], "operationId": "current", "x-llm-intent": "read", "x-when-to-use": ["one", "two"], "x-routing-policy": ["safe"]}}, "/foreign": {"post": {"tags": ["other-role"]}}, "/missing-tags": {"get": {}}}}
    result = subprocess.run(["jq", "-r", "--arg", "role", role, expression[2]], input=json.dumps(source), text=True, capture_output=True, check=True)
    assert result.stdout == "GET\t/correct\tcurrent\tread\tone | two\tsafe\n"
    assert "explicit write scope bounds ALL cache, startup, memory" in text
    assert "Runtime `TOOLS.md` and `HEARTBEAT.md` are retired" in text
    for name in ("TOOLS.md", "HEARTBEAT.md"):
        assert not re.search(rf"^\d+\) `{re.escape(name)}`", text, flags=re.MULTILINE)
        assert f"read `{name}`" not in text
    assert "relevant operating file (`AGENTS.md`, `TOOLS.md`" not in text
    bootstrap = env.get_template("BOARD_BOOTSTRAP.md.j2").render(context)
    heartbeat = env.get_template("BOARD_HEARTBEAT.md.j2").render(context)
    assert "current task's write scope" in bootstrap and "current task's write scope" in heartbeat
    assert "only when the current write scope permits" in bootstrap
    assert "read-only discovery in `AGENTS.md`" in heartbeat
    assert "-o /tmp/openapi" not in heartbeat
    for name in ("TOOLS.md", "HEARTBEAT.md"):
        assert name not in bootstrap and name not in heartbeat
    if role == "agent-lead":
        assert '"execution_mode": "team"' in text
        assert "documentation/research artifacts" in text
        assert "qualified independent exact-SHA QA" in text
