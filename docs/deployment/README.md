# Deployment

> **Generic upstream deployment guide (single host, Docker Compose).** The supported OMC install, for Gentoo with OpenRC or Debian/Ubuntu with systemd, and rootless Docker as isolated named instances, is the instance installer under `deploy/`. Checked against Compose, entrypoint and shipped init examples, not deployed here. See the current-state snapshot for the reviewed Gentoo/OpenRC host, canonical production path and unresolved public TLS mismatch. These examples do not authorize service/configuration/data changes.

> **The live deployment is documented in nuc-host.md.** The instructions below
> are upstream's generic single-host guide; the reviewed production host runs OpenRC, and a
> systemd host is installed by the same installer (`deploy/`) with systemd user units,
> a host-process gateway, and the `omniroute` compose profile.

## Supported OMC installation (Gentoo/OpenRC or Debian/systemd)

Follow the [instance install guide](../install.md). From the repo root, `./install.sh` dispatches
by privilege:

- As root: `./install.sh --install-packages demo` runs `deploy/host-prep.sh` for instance `demo`.
- As `omc-demo`, in its own clone: `./install.sh` runs `deploy/install.sh`.
- Then `deploy/setup.sh` guides onboarding and enables the fleet.

This flow uses rootless Docker and the host init for user services: OpenRC on Gentoo, systemd user
units with lingering on Debian. Configuration and secrets stay under
`data/`; do not create a regular root `.env` in an instance checkout. The generic manual examples
below are not the instance installer and must not be mixed into its checkout.

> **Goal**
> A simple, reproducible deploy that preserves the Postgres volume and supports safe upgrades.

## Deployment mode: single host (Docker Compose)

### Prerequisites

- Docker + Docker Compose v2 (`docker compose`)
- A host where the **browser** can reach the backend URL you configure (see `NEXT_PUBLIC_API_URL` below)

### 1) Configure environment

From repo root:

```bash
cp .env.example .env
```

Edit `.env`:

- `AUTH_MODE=local` (default)
- **Set** `LOCAL_AUTH_TOKEN` to a non-placeholder value (≥ 50 chars)
- Ensure `BASE_URL` matches the public backend origin if it is not `http://localhost:8000`
- Ensure `NEXT_PUBLIC_API_URL` is reachable from the browser (not a Docker-internal hostname)

Key variables (from `.env.example` / `compose.yml`):

- Frontend: `FRONTEND_PORT` (default `3000`)
- Backend: `BACKEND_PORT` (default `8000`)
- Postgres: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`
- Backend:
  - `DB_AUTO_MIGRATE` (default `true` in compose)
  - `CORS_ORIGINS` (default `http://localhost:3000`)
- Security headers (see [configuration reference](../reference/configuration.md)):
  - `SECURITY_HEADER_X_CONTENT_TYPE_OPTIONS` (default `nosniff`)
  - `SECURITY_HEADER_X_FRAME_OPTIONS` (default `DENY`)
  - `SECURITY_HEADER_REFERRER_POLICY` (default `strict-origin-when-cross-origin`)

### 2) Start the stack

```bash
docker compose -f compose.yml --env-file .env up -d --build
```

Open:

- Frontend: `http://localhost:${FRONTEND_PORT:-3000}`
- Backend health: `http://localhost:${BACKEND_PORT:-8000}/healthz`

The current `compose.yml` already sets `restart: unless-stopped` for the core
services. Docker itself must be configured to start at boot on the target host.

### 3) Verify

```bash
curl -f "http://localhost:${BACKEND_PORT:-8000}/healthz"
```

If the frontend loads but API calls fail, double-check:

- `NEXT_PUBLIC_API_URL` is set and reachable from the **browser**
- backend CORS includes the frontend origin (`CORS_ORIGINS`)

## Database persistence

The Compose stack uses a named volume:

- `postgres_data` → `/var/lib/postgresql/data`

This means:

- `docker compose ... down` preserves data
- `docker compose ... down -v` is **destructive** (deletes the DB volume)

## Migrations / upgrades

### Default behavior in Compose

In `compose.yml`, the backend container defaults:

- `DB_AUTO_MIGRATE=true`

So on startup the backend will attempt to run Alembic migrations automatically.

> **Warning**
> For zero/near-zero downtime, migrations must be **backward compatible** with the currently running app if you do rolling deploys.

### Safer operator pattern (manual migrations)

If you want more control, set `DB_AUTO_MIGRATE=false` and run migrations explicitly during deploy:

```bash
cd backend
uv run alembic upgrade head
```

## Container security

The frontend image uses a non-root application user. Backend/worker entrypoints
use `gosu appuser`, but remap that user's UID from `HOST_OPENCLAW_UID`; current
Compose sets it to **0** for gateway-state compatibility. The username alone is
not a non-root security boundary. Match mount permissions to the actual process
UID; do not apply a historical recursive ownership recipe without operator review.

## Reverse proxy / TLS

Typical setup (outline):

- Put the frontend behind HTTPS (reverse proxy)
- Ensure the frontend can reach the backend over the configured `NEXT_PUBLIC_API_URL`

This section is intentionally minimal until we standardize a recommended proxy (Caddy/Nginx/Traefik).

## Run at boot (local install)

The supported OMC installer uses rootless Docker and the host's init for user services: OpenRC user
services on Gentoo, systemd user units on Debian (see the
[install guide](../install.md)). The former upstream `install.sh --mode local` is no longer
available. The systemd and launchd examples below are retained only for unsupported manual,
non-Docker deployments; they are not configured or tested by the OMC installer.

### Linux (systemd)

Use the example systemd units and instructions in [systemd/README.md](./systemd/README.md). In short:

1. Copy the unit files from `docs/deployment/systemd/` and replace `REPO_ROOT`, `BACKEND_PORT`, and `FRONTEND_PORT` with your paths and ports.
2. Install the units under `~/.config/systemd/user/` (user) or `/etc/systemd/system/` (system).
3. Enable and start the backend, frontend, and RQ worker services.

The RQ queue worker is required for gateway lifecycle (wake/check-in) and webhook delivery; run it as a separate unit.

### macOS (launchd)

LaunchAgents run at **user login**, not at machine boot. Use LaunchAgents so the backend, frontend, and worker run under your user and restart on failure. For true boot-time startup you would need LaunchDaemons or other configuration (not covered here).

1. Create a plist for each process under `~/Library/LaunchAgents/`, e.g. `com.openclaw.mission-control.backend.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.openclaw.mission-control.backend</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>uv</string>
    <string>run</string>
    <string>uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>8000</string>
  </array>
  <key>WorkingDirectory</key>
  <string>REPO_ROOT/backend</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:REPO_ROOT/backend/.venv/bin</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

Replace `REPO_ROOT` with the actual repo path. Ensure `uv` is on `PATH` (e.g. add `~/.local/bin` to the `PATH` in the plist). Load with:

```bash
launchctl load ~/Library/LaunchAgents/com.openclaw.mission-control.backend.plist
```

2. Add similar plists for the frontend (`npm run start -- --hostname 0.0.0.0 --port 3000` in `REPO_ROOT/frontend`) and for the RQ worker (`uv run python ../scripts/rq worker` with `WorkingDirectory=REPO_ROOT/backend` and `ProgramArguments` pointing at `uv`, `run`, `python`, `../scripts/rq`, `worker`).
