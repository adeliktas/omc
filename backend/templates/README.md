# Backend workspace templates — current contract

Source baseline: `006f95f8600d5d9f2cde5e4ef4d236403cbca209` (2026-10-05).
See the source-cited current-state report and
agent execution architecture.

## Gateway workspace files

`backend/app/services/openclaw/constants.py:DEFAULT_GATEWAY_FILES+0 (L66)` defines
`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md` and `MEMORY.md`.
Board leads additionally receive `BOOTSTRAP.md` through `LEAD_GATEWAY_FILES`.
Existing `USER.md` and `MEMORY.md` are preserved during updates.

OpenClaw 2026.8+ retired runtime `TOOLS.md` and `HEARTBEAT.md`: tool guidance
belongs in `AGENTS.md`; heartbeat is gateway configuration. Historical Jinja
heartbeat/tool templates still exist, but their presence does not make them
supported default workspace files. Do not restore the retired files by copying
old wiki workspace dumps.

## Rendering and roles

- `provisioning.py:_template_env+0` enables Jinja `StrictUndefined`, disables
  HTML escaping for Markdown and preserves trailing newlines.
- `_build_context` / `_build_main_context` supply board, agent, user, identity
  and API context. Never publish rendered authorization values.
- `_render_agent_files` selects canonical role profiles in
  `backend/templates/roles/<slug>/`; per-agent identity/soul overrides take
  precedence. `canonical_role_slug` normalizes the role identity.
- `BoardAgentLifecycleManager._file_names` and `_template_overrides` select
  lead/shared files. `GatewayMainAgentLifecycleManager` has its own context.
- `_agent_entries_as_list` / `_agent_roster_patch` support the current gateway
  `agents.entries` roster; historical `agents.list[]` examples are not the
  current provisioning contract.

All provisioning references above are in
[`backend/app/services/openclaw/provisioning.py`](../app/services/openclaw/provisioning.py).
Constants are in [`constants.py`](../app/services/openclaw/constants.py).
Native team PM/manager/worker/QA briefs are a **separate** contract in
[`backend/app/services/team_cycle.py:brief+0 (L1319)`](../app/services/team_cycle.py), not
copies of the historical gateway workspace.

## Updating templates safely

Use the gateway template-sync API/script documented in the execution guide;
inspect current gateway/organization/agent identity first. Do not run a sync
merely to validate documentation or bypass current permissions.

For a template change: provide every Jinja variable through its context
builder, preserve editable files, check lead/main/worker selection and run
relevant provisioning tests. `make docs-check` checks documentation formatting
and relative file links; it is not provisioning or security QA.
