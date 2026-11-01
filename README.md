# OMC — OpenClaw Mission Control

OMC is a self-hosted control plane where teams of LLM agents work on boards. On a mapped board,
each task goes through these stages:

1. it is planned;
2. it is implemented by scoped workers;
3. independent agents check it (QA);
4. it is published as a pull request.

A model router decides which model does which work at what cost. A fleet layer watches for stalls
and repairs the system itself; only decisions that need a human reach the owner, with options.

OMC grew from [`abhi1693/openclaw-mission-control`](https://github.com/abhi1693/openclaw-mission-control),
which is now archived. This repo is its trunk.

## Components

| Component | Where | What it is |
|---|---|---|
| Mission Control | `backend/`, `frontend/`, `compose.yml` | FastAPI backend, Next.js UI, webhook worker, Postgres and Redis, run with Docker Compose |
| OmniRoute | `compose.yml` (`omniroute` profile) | The model gateway. You plug your own LLM servers and accounts in here |
| Fleet layer | `scripts/` | Team pool and runner, watchdog, GitHub responder, backlog batching, intake, quota balancer, disk janitor |
| Instance tooling | `deploy/` | host prep, install, guided setup and purge of isolated instances |
| OmniRoute build | `omniroute/` | pinned upstream release plus the OMC patch |
| Agent harnesses | `harness/` | core OpenClaw gateway plus optional Pi/Claude Code; `deploy/setup.sh --components` offers interactive install/configure/skip choices as the instance user |

For how the pieces fit together, see [architecture](docs/architecture/README.md#fleet-autonomy-layer-2026-10-28).

## Install

The supported targets are Gentoo Linux with OpenRC and Debian/Ubuntu with systemd, both with
rootless Docker. Each deployment is an isolated, named instance:
- its own unprivileged user and home;
- its own Docker daemon, ports and `data/` directory;
- it can be removed completely without affecting other instances.

Start with the [install guide](docs/install.md):

```bash
deploy/host-prep.sh <name>            # root, once per instance
deploy/install.sh && deploy/setup.sh   # as omc-<name>, in its clone
```

Then see [configuration](docs/configuration.md), [operations](docs/operations.md), [agent
harnesses](docs/harness.md) and [security](SECURITY.md). The generic upstream Compose path is still
described in [deployment](docs/deployment/README.md).

## Authentication

Mission Control supports two authentication modes:

- `local`: shared bearer token mode (default for self-hosted use)
- `clerk`: Clerk JWT mode

Environment templates:

- Root: [`.env.example`](./.env.example)
- Backend: [`backend/.env.example`](./backend/.env.example)
- Frontend: [`frontend/.env.example`](./frontend/.env.example)

Real values never go into git. Each instance keeps them in its git-ignored `data/` directory.

## Documentation

Guides for architecture, deployment, operations, troubleshooting and testing are in [`docs/`](./docs/).

## Contributing

See the [contributing guide](./CONTRIBUTING.md).

## License

MIT, as inherited from upstream. See [`LICENSE`](./LICENSE).
