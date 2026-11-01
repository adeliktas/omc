#!/usr/bin/env bash
# Install or update this OMC instance. Run as the instance user omc-<name>, from its repo clone,
# after `deploy/host-prep.sh <name>` (root). Safe to re-run: data/.env is never regenerated.
# usage: deploy/install.sh [--render-env-only]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=$(dirname "$here")
. "$here/lib.sh"
die() { echo "install: $*" >&2; exit 1; }
name=${OMC_INSTANCE:-$(id -un)}
name=${name#omc-}
omc_valid_name "$name" || die "run as an instance user omc-<name> (see deploy/host-prep.sh)"
[ -n "${OMC_INSTANCE:-}" ] || [[ $(id -un) == omc-* ]] || die "run as an instance user omc-<name> (see deploy/host-prep.sh)"
# A regular .env means a deployment this script did not create: never overwrite its secrets.
[ ! -e "$home/.env" ] || [ -L "$home/.env" ] || die "refusing: $home/.env is a regular file, not an instance checkout"
data=$home/data
umask 077
mkdir -p "$data/etc/mc-team" "$data/state" "$data/log" "$data/run"
chmod 700 "$data"

secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
render() { # <template> <out> KEY=value...
  local s out=$2 kv
  s=$(cat "$1")
  shift 2
  for kv in "$@"; do s=${s//@${kv%%=*}@/${kv#*=}}; done
  printf '%s\n' "$s" > "$out"
}
if [ ! -f "$data/.env" ]; then
  base=${OMC_PORT_BASE:-$(sed -n 's/^OMC_PORT_BASE=//p' "$HOME/.omc-instance" 2>/dev/null || true)}
  read -r p1 p2 p3 p4 p5 p6 <<<"$(omc_pick_ports 6 "${base:-13000}")"
  jwt=$(secret)
  render "$here/templates/env.template" "$data/.env" PROJECT="omc-$name" FRONTEND_PORT="$p1" \
    BACKEND_PORT="$p2" POSTGRES_PORT="$p3" REDIS_PORT="$p4" OMNIROUTE_PORT="$p5" \
    OMNIROUTE_PORT2="$p6" TOKEN="$(secret)" PGPASS="$(secret)" JWT="$jwt"
  render "$here/templates/env.omniroute.template" "$data/.env.omniroute" JWT="$jwt" \
    API_KEY_SECRET="$(secret)" STORAGE_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
    INITIAL_PASSWORD="$(secret)"
fi
[ -f "$data/.env.omniroute" ] || die "data/.env exists but data/.env.omniroute is missing; restore it from your backup"
[ -f "$data/etc/payloadRules.json" ] || cp "$here/templates/payloadRules.json" "$data/etc/payloadRules.json"
ln -sfn data/.env "$home/.env"
ln -sfn data/.env.omniroute "$home/.env.omniroute"
[ "${1:-}" = --render-env-only ] && { echo "install: $data/.env ready"; exit 0; }

[ "$(id -u)" != 0 ] || die "do not run as root"
[ -f "$HOME/.omc-instance" ] || die "not an OMC instance user (run deploy/host-prep.sh <name> as root first)"
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}
docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless || die "rootless Docker is not reachable at $DOCKER_HOST"
set -a; . "$data/.env"; set +a
cd "$home"

# OmniRoute: pinned upstream + OMC patch, webpack and one worker to stay inside small hosts' RAM.
if [ -n "${OMC_REBUILD_OMNIROUTE:-}" ] || ! docker image inspect "$OMNIROUTE_IMAGE" >/dev/null 2>&1; then
  "$home/omniroute/build.sh" "$OMNIROUTE_IMAGE" --target runner-base \
    --build-arg OMNIROUTE_USE_TURBOPACK=0 --build-arg OMNIROUTE_BUILD_WORKERS=1
fi
docker compose up -d --build
# The legacy builder leaves intermediate stages behind (~17 GB for OmniRoute); this daemon is the
# instance's own, so its dangling images and build cache go.
docker image prune -f >/dev/null
docker builder prune -af >/dev/null

# Fleet services and crontab are rendered here and started by deploy/setup.sh (step 7).
svc=$HOME/.config/rc/init.d
mkdir -p "$svc"
for s in mc-team-pool mc-review-agent openclaw-gateway; do
  sed "s|@OMC_HOME@|$home|g; s|@OMC_DATA@|$data|g" "$here/templates/openrc/$s" > "$svc/$s"
  chmod 755 "$svc/$s"
done
for f in crontab fleet.env; do
  sed "s|@OMC_HOME@|$home|g; s|@OMC_DATA@|$data|g; s|@DOCKER_HOST@|$DOCKER_HOST|g; s|@HOME@|$HOME|g" \
    "$here/templates/$f" > "$data/etc/$f"
done

for _ in $(seq 100); do
  curl -fsS -o /dev/null "http://127.0.0.1:$BACKEND_PORT/health" &&
    curl -fsS -o /dev/null "http://127.0.0.1:$FRONTEND_PORT/" && break
  sleep 3
done
curl -fsS -o /dev/null "http://127.0.0.1:$BACKEND_PORT/health" || die "backend not healthy on port $BACKEND_PORT (docker compose logs backend)"
curl -fsS -o /dev/null "http://127.0.0.1:$FRONTEND_PORT/" || die "frontend not answering on port $FRONTEND_PORT"
echo "install: instance '$name' is up: UI http://localhost:$FRONTEND_PORT  next: deploy/setup.sh"
