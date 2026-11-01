# Operations

All commands below run as the instance user, in `~/omc`, unless marked **root**.

## Services and jobs

| What | How it runs | Log |
|---|---|---|
| Postgres, Redis, backend, frontend, webhook worker, OmniRoute | Docker Compose on the user's rootless daemon | `docker compose logs <service>` |
| rootless Docker | user service `dockerd` | `~/.local/state/dockerd.log` |
| OpenClaw core gateway | user service `openclaw-gateway` (after installing/configuring OpenClaw) | `data/log/openclaw-gateway.log` |
| team pool | user service `mc-team-pool` | `data/log/mc-claude-runner.log` |
| review agent | user service `mc-review-agent` | `data/log/mc-review-agent.log` |
| watchdog, GitHub responder, next batch, quota balancer, disk janitor | the user's crontab, through `deploy/run.sh` | `data/log/<script>.log` |

User services are OpenRC user services or systemd user units, whichever the host runs:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
# OpenRC
rc-status -U
rc-service -U openclaw-gateway status
rc-service -U mc-team-pool restart
# systemd
systemctl --user status openclaw-gateway
systemctl --user restart mc-team-pool
```

On the host (as **root**), the whole instance starts or stops with `rc-service user.omc-<name>
start|stop` on OpenRC, or `systemctl start|stop user@$(id -u omc-<name>).service` on systemd
(`loginctl enable-linger omc-<name>` keeps it running from boot).

The `openclaw-gateway` service uses this user's `~/.local/bin/openclaw` and existing
`~/.openclaw/openclaw.json`. After configuring the gateway, enable and start it with
`rc-update -U add openclaw-gateway default && rc-service -U openclaw-gateway start` (OpenRC) or
`systemctl --user enable --now openclaw-gateway` (systemd).
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
rc-service -U mc-team-pool restart          # systemd: systemctl --user restart mc-team-pool
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
- **OmniRoute build is killed.** Its `next build` needs more than 12 GB of free RAM (measured
  11.9 GB), even with webpack and one worker. Free memory and retry; each failed attempt is a
  host-wide out-of-memory kill.
