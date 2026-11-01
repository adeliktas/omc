# Operations

All commands below run as the instance user, in `~/omc`, unless marked **root**.

## Services and jobs

| What | How it runs | Log |
|---|---|---|
| Postgres, Redis, backend, frontend, webhook worker, OmniRoute | Docker Compose on the user's rootless daemon | `docker compose logs <service>` |
| rootless Docker | user service `dockerd`: `systemctl --user` on Debian, OpenRC `dockerd` on Gentoo | `~/.local/state/dockerd.log` |
| OpenClaw core gateway | user service `openclaw-gateway` (after installing/configuring OpenClaw) | `data/log/openclaw-gateway.log` |
| team pool | user service `mc-team-pool` | `data/log/mc-claude-runner.log` |
| review agent | user service `mc-review-agent` | `data/log/mc-review-agent.log` |
| watchdog, GitHub responder, next batch, quota balancer, disk janitor | the user's crontab, through `deploy/run.sh` | `data/log/<script>.log` |

User services, on systemd (Debian/Ubuntu):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user list-units 'mc-*' 'docker.service' 'openclaw-gateway.service'
systemctl --user status openclaw-gateway
systemctl --user restart mc-team-pool
```

The same units on Gentoo/OpenRC:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
rc-status -U
rc-service -U openclaw-gateway status
rc-service -U mc-team-pool restart
```

On the host (as **root**), `loginctl disable-linger`/`systemctl --user stop docker` on Debian or
`rc-service user.omc-<name> start`/`stop` on Gentoo starts or stops the whole instance session.

The `openclaw-gateway` service uses this user's `~/.local/bin/openclaw` and existing
`~/.openclaw/openclaw.json`. After configuring the gateway, enable/start it with
`systemctl --user enable --now openclaw-gateway` on Debian, or
`rc-update -U add openclaw-gateway default` and `rc-service -U openclaw-gateway start` on Gentoo.
If it is already running by hand, stop only that instance user's gateway before starting its
managed service; do not kill another user's processes. A tmux setup session is not a replacement
for runtime service management.

Coding services are optional: setup step 7 disarms/stops the pool and review agent when no
coding harness has been configured. Core OpenClaw/PEV operation remains available. Re-enter
`deploy/setup.sh --components`, then step 7, when you want to enable optional coding work.

## Upgrade

```bash
git pull
OMC_REBUILD_OMNIROUTE=1 deploy/install.sh   # without the variable, the OmniRoute image is reused
rc-service -U mc-team-pool restart
```

## Backup and restore

Everything instance-specific is in `data/`, plus the Docker volumes.

```bash
docker compose exec -T db pg_dump -U postgres mission_control > ~/omc-db.sql
tar czf ~/omc-data.tgz -C ~/omc data
```

Restore `data/` into a new clone before running `deploy/install.sh`. Then load the dump with
`docker compose exec -T db psql -U postgres mission_control < ~/omc-db.sql`.

## Remove

```bash
deploy/purge.sh <name>     # root: everything of the instance, then a leftover check
```

## Troubleshooting

- **The fleet does nothing.** Read `data/log/mc-claude-runner.log`. The pool only works boards
  whose goal is confirmed, and `RUNNER_APPLY=1` must be set (it is in `data/etc/fleet.env`).
- **A step of setup fails.** Re-run that step alone with `deploy/setup.sh --step N`.
- **OmniRoute build is killed.** It needs about 8 GB of RAM. Close other builds and retry;
  `install.sh` already builds with webpack and one worker.
