#!/usr/bin/env bash
# Root prepares a named instance; its unprivileged user installs the stack.
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" = 0 ]; then
  exec "$root/deploy/host-prep.sh" "$@"
fi
exec "$root/deploy/install.sh" "$@"
