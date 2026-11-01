# Getting started

> **Generic local quickstart (upstream Compose path), reviewed 2026-10-28.** The supported OMC install is the instance installer under `deploy/`, which adds the fleet layer, OmniRoute and guided setup. See the current-state snapshot before enabling native teams or standing review. A running UI/API does not establish model readiness or task acceptance.

## What is Mission Control?

Mission Control is the web UI and HTTP API for operating task/agent workflows,
including native CLI teams and optional OpenClaw gateway execution.

It provides a control plane for boards, tasks, agents, approvals, and (optionally) gateway connections.

## Quickstart (Docker Compose)

From repo root:

```bash
cp .env.example .env

# REQUIRED when AUTH_MODE=local
# Set LOCAL_AUTH_TOKEN to a non-placeholder value with at least 50 characters.

docker compose -f compose.yml --env-file .env up -d --build
```

Open:
- Frontend: http://localhost:3000
- Backend health: http://localhost:8000/healthz

## Next steps

- [Authentication](../reference/authentication.md)
- [Deployment](../deployment/README.md)
- [Development](../development/README.md)
