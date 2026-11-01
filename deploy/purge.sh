#!/usr/bin/env bash
# Remove one OMC instance completely, then prove nothing of it is left. Run as root.
# usage: deploy/purge.sh [--dry-run] <name>
#
# Only touches user omc-<name> when it carries ~/.omc-instance (written by host-prep.sh).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
dry=0
[ "${1:-}" = --dry-run ] && { dry=1; shift; }
name=${1:-}
die() { echo "purge: $*" >&2; exit 1; }
run() { if [ "$dry" = 1 ]; then echo "+ $*"; else "$@"; fi; }

omc_valid_name "$name" || die "refusing: invalid instance name '$name'"
user=$(omc_user "$name")
[ "$(omc_user_state "$name")" = instance ] || die "refusing: $user is not an OMC instance"
[ "$(id -u)" = 0 ] || die "run as root"
entry=$(${OMC_GETENT:-getent} passwd "$user")
uid=$(cut -d: -f3 <<<"$entry")
home=$(cut -d: -f6 <<<"$entry")
ports=$(grep -hE '^[A-Z0-9_]*PORT[0-9]*=[0-9]+$' "$home"/omc/data/.env 2>/dev/null | cut -d= -f2 | tr '\n' ' ' || true)

run su - "$user" -c 'cd ~/omc 2>/dev/null && docker compose down -v --rmi all --remove-orphans' || true
run su - "$user" -c 'crontab -r' || true
if [ "$(omc_init)" = systemd ]; then
  run loginctl disable-linger "$user" || true
  run systemctl stop "user@$uid.service" || true
else
  run rc-service "user.$user" stop || true
  run rc-update del "user.$user" default || true
  run rm -f "/etc/init.d/user.$user" "/etc/conf.d/user.$user"
fi
run pkill -KILL -U "$uid" || true
run sed -i "/^$user:/d" /etc/subuid /etc/subgid
[ -f "${OMC_REGISTRY:-/etc/omc/instances}" ] && run sed -i "/^$name /d" "${OMC_REGISTRY:-/etc/omc/instances}"
run userdel -r "$user"
[ "$dry" = 1 ] && exit 0

sleep 1
left=()
pgrep -U "$uid" >/dev/null && left+=("processes of uid $uid")
for p in $ports; do [ -n "$(ss -Htln "sport = :$p")" ] && left+=("port $p"); done
getent passwd "$user" >/dev/null && left+=("passwd entry")
grep -qs "^$user:" /etc/subuid /etc/subgid && left+=("subuid/subgid line")
[ -e "/etc/init.d/user.$user" ] && left+=("/etc/init.d/user.$user")
[ -e "/var/lib/systemd/linger/$user" ] && left+=("systemd linger")
[ -e "/var/spool/cron/crontabs/$user" ] && left+=("crontab")
[ -e "$home" ] && left+=("$home")
[ ${#left[@]} -eq 0 ] || die "leftovers: ${left[*]}"
echo "purge: instance '$name' removed; nothing left"
