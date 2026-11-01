#!/usr/bin/env bash
# T90 B2.b — host-side openclaw-gateway auto-restart.
# Reads heartbeat lines written by MC backend (`watchdog_loop._write_gateway_heartbeat`)
# and restarts the gateway after N consecutive `alive=false`.
#
# Heartbeat schema (one JSON object per line, last 20 retained):
#   {"ts": ISO8601, "alive": bool, "host": str, "port": int, "failures": int}
#
# Install (OpenRC + cronie):
#   cp scripts/gateway-watchdog.sh ~/.openclaw/scripts/
#   crontab: * * * * * ~/.openclaw/scripts/gateway-watchdog.sh >/dev/null 2>&1

set -euo pipefail

HEARTBEAT_FILE="${HEARTBEAT_FILE:-$HOME/.openclaw/heartbeat.gateway}"
THRESHOLD="${GATEWAY_FAILURE_THRESHOLD:-3}"           # consecutive alive=false reports
# The backend rewrites the heartbeat every tick. An old file means the backend is
# down, and restarting the gateway would not help anyone.
STALE_AFTER_SECONDS="${STALE_AFTER_SECONDS:-600}"
COOLDOWN_FILE="$HOME/.openclaw/.gateway-watchdog.cooldown"
COOLDOWN_SECONDS="${GATEWAY_RESTART_COOLDOWN:-300}"   # don't bounce more than 1x / 5min
LOG_FILE="$HOME/.openclaw/.gateway-watchdog.log"

log() {
    printf '%s %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE"
}

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    log "heartbeat_missing path=$HEARTBEAT_FILE"
    exit 0
fi

file_age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))
if (( file_age > STALE_AFTER_SECONDS )); then
    log "heartbeat_stale age=${file_age}s — backend likely down, not restarting gateway"
    exit 0
fi

recent="$(tail -n "$THRESHOLD" "$HEARTBEAT_FILE" || true)"
if [[ -z "$recent" ]]; then
    log "heartbeat_empty"
    exit 0
fi

# The MC `failures` field is the backend's own consecutive count — defer to it.
last_failures="$(printf '%s\n' "$recent" | tail -n 1 | grep -oE '"failures":\s*[0-9]+' | grep -oE '[0-9]+' || echo 0)"
last_alive="$(printf '%s\n' "$recent" | tail -n 1 | grep -oE '"alive":\s*(true|false)' | awk -F: '{gsub(/ /,""); print $2}' || echo true)"

if [[ "$last_alive" == "true" ]]; then
    exit 0
fi

if (( last_failures < THRESHOLD )); then
    log "below_threshold failures=$last_failures threshold=$THRESHOLD"
    exit 0
fi

if [[ -f "$COOLDOWN_FILE" ]]; then
    last_restart_epoch="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)"
    now_epoch="$(date +%s)"
    if (( now_epoch - last_restart_epoch < COOLDOWN_SECONDS )); then
        log "cooldown_active last=$last_restart_epoch now=$now_epoch window=$COOLDOWN_SECONDS"
        exit 0
    fi
fi

log "gateway_alive_probe.restart_fired failures=$last_failures threshold=$THRESHOLD"
date +%s > "$COOLDOWN_FILE"
rc-service openclaw-gateway restart
log "restart_command_sent"
