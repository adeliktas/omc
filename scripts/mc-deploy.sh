#!/usr/bin/env bash
# mc-deploy.sh — rebuild and restart one Mission Control service, with rollback.
#
# Scope is deliberately narrow (operator grant, 2026-09-22): backend, frontend and
# webhook-worker only. omniroute/db/redis hold state or serve traffic that cannot be
# cheaply rebuilt, and the gateway needs ~2min and ~4.4G to boot — a bad restart loops.
#
#   scripts/mc-deploy.sh backend            rebuild + restart + verify, roll back if unhealthy
#   scripts/mc-deploy.sh --dry-run backend  print what it would do, touch nothing
#
# Rollback is at the image level: compose reuses the same image tag, so the previous
# build survives as a dangling image, identified by ID. We retag that ID and bring the
# service back up with --no-build.
#
# NOTE: run from a checkout whose compose.yml and build context are what you want
# deployed. This is not always the deployment checkout.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT=openclaw-mission-control
# compose.yml reads ${LOCAL_AUTH_TOKEN} etc.; without an env file every var silently
# becomes "" and the service comes up misconfigured rather than failing loudly.
ENV_FILE="${MC_ENV_FILE:-$PWD/.env}"
ALLOWED="backend frontend webhook-worker"
MIN_AVAIL_MB=4096
HEALTH_TIMEOUT=120
CHECK_INTERVAL=3
STABLE_CHECKS=5   # ~15s of continuous health before we call it up
KEEP_ROLLBACK_IMAGES=2  # dangling images kept per service after a healthy deploy (>= 1)

DRY=0
[ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
SVC="${1:-}"

case " $ALLOWED " in
  *" $SVC "*) ;;
  *) echo "usage: $0 [--dry-run] <${ALLOWED// /|}>" >&2; exit 2 ;;
esac

[ -f "$ENV_FILE" ] || { echo "ABORT: no env file at $ENV_FILE (set MC_ENV_FILE). compose would substitute blanks." >&2; exit 1; }

# -f compose.yml explicitly, so a stray compose file can never be picked up instead.
dc() { docker compose -p "$PROJECT" -f compose.yml --env-file "$ENV_FILE" "$@"; }

run() { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }
log() { echo "[mc-deploy $(date +%H:%M:%S)] $*"; }

# Health: what "came up" means for each service. webhook-worker serves no HTTP, so
# the best cheap signal is that it is running and has not entered a restart loop.
health() {
  case "$SVC" in
    backend)  curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/healthz ;;
    frontend) curl -fsS -m 5 -o /dev/null http://127.0.0.1:3000/dashboard ;;
    webhook-worker)
      local cid state restarts
      cid=$(dc ps -q "$SVC") || return 1
      [ -n "$cid" ] || return 1
      state=$(docker inspect -f '{{.State.Status}}' "$cid")
      restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
      [ "$state" = running ] && [ "$restarts" -eq 0 ]
      ;;
  esac
}

# Healthy means *stays* healthy. A first drill of this script passed a broken
# webhook-worker build: the single check ran in the ~2s before the deliberately
# broken entrypoint crashed, so the script reported success on a container that
# went on to crashloop. Require STABLE_CHECKS consecutive passes; any failure
# resets the streak.
wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) streak=0
  while [ $SECONDS -lt $deadline ]; do
    if health; then
      streak=$((streak + 1))
      [ $streak -ge $STABLE_CHECKS ] && return 0
    else
      streak=0
    fi
    sleep "$CHECK_INTERVAL"
  done
  return 1
}

# --- preflight -------------------------------------------------------------
avail=$(free -m | awk '/^Mem:/ {print $7}')
log "free RAM: ${avail}MB (need >= ${MIN_AVAIL_MB}MB)"
[ "$avail" -ge "$MIN_AVAIL_MB" ] || { echo "ABORT: not enough free RAM; a redeploy into a starved host caused a restart loop before" >&2; exit 1; }

cid=$(dc ps -q "$SVC" || true)
[ -n "$cid" ] || { echo "ABORT: $SVC is not running; nothing to roll back to" >&2; exit 1; }
PREV_IMAGE=$(docker inspect -f '{{.Image}}' "$cid")
PREV_TAG=$(docker inspect -f '{{.Config.Image}}' "$cid")
log "currently running $PREV_TAG ($PREV_IMAGE)"

health || { echo "ABORT: $SVC is already unhealthy; fix that before deploying over it" >&2; exit 1; }
log "pre-deploy health OK"

# --- deploy ----------------------------------------------------------------
log "deploying $SVC from $PWD"
# Do not rebuild/recreate dependencies: a frontend deploy must not silently replace
# the backend (which may contain an operator-owned, intentionally uncommitted fix).
run dc up -d --build --no-deps "$SVC"

if [ "$DRY" = 1 ]; then
  echo "  would wait up to ${HEALTH_TIMEOUT}s for health, and on failure:"
  echo "    docker tag $PREV_IMAGE $PREV_TAG && dc up -d --no-build --no-deps $SVC"
  exit 0
fi

if wait_healthy; then
  log "OK: $SVC healthy on the new image"
  # Keep KEEP_ROLLBACK_IMAGES dangling images: always the one this deploy replaced
  # (the rollback target, whatever its age — it can predate other dangling images
  # after an earlier rollback), then the newest others. Keeping every replaced image
  # filled the disk: 178 dangling images, ~86 GB reclaimable, root fs 94% (2026-10-06).
  docker images -q --no-trunc -f dangling=true -f "label=com.docker.compose.project=$PROJECT" \
      -f "label=com.docker.compose.service=$SVC" \
    | grep -v -x -F "$PREV_IMAGE" \
    | xargs -r docker inspect -f '{{.Created}} {{.Id}}' | sort -r \
    | tail -n +"$KEEP_ROLLBACK_IMAGES" | awk '{print $2}' \
    | xargs -r docker rmi >/dev/null 2>&1 || true
  exit 0
fi

# --- rollback --------------------------------------------------------------
log "UNHEALTHY after ${HEALTH_TIMEOUT}s — rolling back to $PREV_IMAGE"
docker logs --tail 40 "$(dc ps -q "$SVC")" 2>&1 | sed 's/^/    | /' || true
docker tag "$PREV_IMAGE" "$PREV_TAG"
dc up -d --no-build --force-recreate --no-deps "$SVC"

if wait_healthy; then
  log "rolled back; $SVC healthy again on $PREV_IMAGE"
  exit 1
fi
log "ROLLBACK FAILED — $SVC is down on $PREV_IMAGE. Operator needed."
exit 3
