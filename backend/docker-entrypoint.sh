#!/bin/sh
set -e

# Ensure writable volume files/dirs exist with correct ownership before dropping to appuser.
REVIEW_FILE="${REVIEW_FILE:-/traffic-data/OWNER_REVIEW.json}"
TRAFFIC_DIR="/traffic-data"

# Match host UID FIRST so the chown below uses the FINAL appuser uid.
# (Old order chowned traffic-data before usermod, leaving files at the image's
# baked appuser uid. Subsequent runs with a different HOST_OPENCLAW_UID then
# couldn't write the sqlite WAL files → "attempt to write a readonly database".)
HOST_OPENCLAW_UID="${HOST_OPENCLAW_UID:-1000}"
if [ "$(id -u appuser 2>/dev/null)" != "$HOST_OPENCLAW_UID" ]; then
    usermod -o -u "$HOST_OPENCLAW_UID" appuser 2>/dev/null || true
    groupmod -o -g "$HOST_OPENCLAW_UID" appgroup 2>/dev/null || true
    chown -R appuser:appgroup /home/appuser 2>/dev/null || true
fi

if [ -d "$TRAFFIC_DIR" ]; then
    # Now chown to the final appuser uid.
    chown -R appuser:appgroup "$TRAFFIC_DIR" 2>/dev/null || true
    chmod -R 775 "$TRAFFIC_DIR" 2>/dev/null || true

    if [ ! -f "$REVIEW_FILE" ]; then
        echo "[]" > "$REVIEW_FILE"
    fi
    chown appuser:appgroup "$REVIEW_FILE" 2>/dev/null || true
    chmod 664 "$REVIEW_FILE" 2>/dev/null || true
fi

exec gosu appuser "$@"
