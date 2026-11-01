#!/usr/bin/env bash
# Prepares this host for one OMC instance. Run as root, once per instance; safe to re-run.
# usage: deploy/host-prep.sh [--dry-run] [--install-packages] <name>
#
# Creates user omc-<name> (no wheel, no docker group) with its own rootless Docker daemon, started
# by the host init as an OpenRC user service (user.omc-<name>) or a systemd user unit plus lingering.
# Nothing else on the host is touched.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
dry=0 pkgs=0
while [ $# -gt 0 ]; do
  case $1 in --dry-run) dry=1 ;; --install-packages) pkgs=1 ;; *) break ;; esac
  shift
done
name=${1:-}
die() { echo "host-prep: $*" >&2; exit 1; }
run() { if [ "$dry" = 1 ]; then echo "+ $*"; else "$@"; fi; }
# Writes into the instance home run as the instance user, so a link planted there cannot make root
# write elsewhere.
as_user() {
  if [ "$dry" = 0 ]; then runuser -u "$user" -- "$@"; return; fi
  [ -p /dev/stdin ] && cat > /dev/null  # a dry run still drains what is piped in
  echo "+ runuser -u $user -- $*"
}

omc_valid_name "$name" || die "invalid instance name '$name' (want [a-z][a-z0-9]{0,11})"
user=$(omc_user "$name")
state=$(omc_user_state "$name")
[ "$state" = foreign ] && die "user $user exists but is not an OMC instance (no ~/.omc-instance); refusing"
[ "$(id -u)" = 0 ] || die "run as root"

# 1. Packages. Installed only on request. The commands are probed by name; pkg() maps them to
#    this host family's package names.
missing=()
if ! command -v docker >/dev/null; then
  # Debian's docker.io is older than the required 29; adding Docker's apt repository is the
  # operator's call, not this script's.
  [ "$(omc_os)" = debian ] && die "Docker >= 29 is required: install docker-ce from download.docker.com first (this script does not add apt repositories)"
  missing+=(docker)
fi
command -v newuidmap >/dev/null || missing+=(newuidmap)
command -v bwrap >/dev/null || missing+=(bwrap)
command -v slirp4netns >/dev/null || missing+=(slirp4netns)
command -v git >/dev/null || missing+=(git)
command -v curl >/dev/null || missing+=(curl)
command -v crontab >/dev/null || missing+=(crontab)
command -v flock >/dev/null || missing+=(flock)
command -v pgrep >/dev/null || missing+=(pgrep)
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null || missing+=(python3)
pkg() { # <probe name> -> package names on this host family
  case "$(omc_os):$1" in
    gentoo:docker) echo app-containers/docker app-containers/docker-cli ;;
    gentoo:newuidmap) echo sys-apps/shadow ;;
    gentoo:bwrap) echo sys-apps/bubblewrap ;;
    gentoo:slirp4netns) echo app-containers/slirp4netns ;;
    gentoo:git) echo dev-vcs/git ;;
    gentoo:curl) echo net-misc/curl ;;
    gentoo:crontab) echo sys-process/cronie ;;
    gentoo:flock) echo sys-process/util-linux ;;
    gentoo:pgrep) echo sys-process/procps ;;
    gentoo:python3) echo dev-lang/python ;;
    debian:newuidmap) echo uidmap ;;
    debian:bwrap) echo bubblewrap ;;
    debian:slirp4netns) echo slirp4netns ;;
    debian:crontab) echo cron ;;
    debian:flock) echo util-linux ;;
    debian:pgrep) echo procps ;;
    *) echo "$1" ;;
  esac
}
if [ ${#missing[@]} -gt 0 ]; then
  packages=()
  for m in "${missing[@]}"; do read -r -a one <<<"$(pkg "$m")"; packages+=("${one[@]}"); done
  mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)
  if [ "$pkgs" = 1 ]; then
    case $(omc_os) in
      debian) run apt-get update; run apt-get install -y --no-install-recommends "${packages[@]}" ;;
      *) run emerge --noreplace "${packages[@]}" ;;
    esac
  elif [ "$dry" = 0 ]; then
    die "missing packages: ${missing[*]} (re-run with --install-packages, or install ${packages[*]})"
  fi
fi

# Rootless networking (slirp4netns) needs the TUN device; load it now and at every boot.
if [ ! -c /dev/net/tun ]; then
  run modprobe tun
  run mkdir -p /etc/modules-load.d  # read by OpenRC's modules service and systemd-modules-load
  [ -f /etc/modules-load.d/omc-tun.conf ] || run sh -c 'echo tun > /etc/modules-load.d/omc-tun.conf'
fi

# 2. rootlesskit + dockerd-rootless.sh from Docker's official static rootless extras, matching the
#    host's Docker (Gentoo ships rootlesskit only as ~arch). Shared by all instances, read-only.
ver=$(docker --version | sed -E 's/^Docker version ([0-9.]+).*/\1/')
extras=/opt/omc/rootless-extras/$ver
if [ ! -x "$extras/rootlesskit" ]; then
  url="https://download.docker.com/linux/static/stable/$(uname -m)/docker-rootless-extras-$ver.tgz"
  run mkdir -p "$extras"
  if [ "$dry" = 1 ]; then echo "+ curl $url | tar -xz -C $extras"
  else curl -fsSL "$url" | tar -xz --strip-components=1 -C "$extras"; fi
fi

# 3. The instance user, marked so purge.sh can tell it from any other account.
if [ "$state" = new ]; then
  run useradd -m -s /bin/bash "$user"
  run install -o "$user" -g "$user" -m 0644 /dev/null "/home/$user/.omc-instance"
fi
# cronie only lets members of the cron group use crontab (the fleet's periodic jobs).
getent group cron >/dev/null && ! id -nG "$user" 2>/dev/null | grep -qw cron && run usermod -aG cron "$user"
home=/home/$user

# A 20-port block per instance, recorded so a stopped instance keeps its ports.
reg=${OMC_REGISTRY:-/etc/omc/instances}
base=$(omc_port_base "$name")
grep -qs "^$name " "$reg" || { run mkdir -p "$(dirname "$reg")"; run sh -c "echo '$name $base' >> $reg"; }
grep -qs '^OMC_PORT_BASE=' "$home/.omc-instance" ||
  echo "OMC_PORT_BASE=$base" | as_user sh -c 'cat >> "$1"' _ "$home/.omc-instance"

# 4. One subordinate id range per instance, after the highest range in use.
start=$(awk -F: 'BEGIN { m = 100000 } { if ($2 + $3 > m) m = $2 + $3 } END { print m }' /etc/subuid /etc/subgid 2>/dev/null)
for f in /etc/subuid /etc/subgid; do
  grep -q "^$user:" "$f" 2>/dev/null || run sh -c "echo '$user:$start:65536' >> $f"
done

# 5. The user's rootless Docker as an init service: an OpenRC user service whose session is started
#    by user.omc-<name>, or a systemd user unit made boot-safe with lingering.
svc=$home/.config/rc
uid=$(id -u "$user" 2>/dev/null || echo UID)  # a dry run has no user yet
userctl() { run runuser -u "$user" -- env "XDG_RUNTIME_DIR=/run/user/$uid" systemctl --user "$@"; }
if [ "$(omc_init)" = systemd ]; then
  sysvc=$home/.config/systemd/user
  as_user mkdir -p "$sysvc/default.target.wants"
  sed "s|@EXTRAS@|$extras|g" "$here/templates/systemd/docker.service" |
    as_user sh -c 'cat > "$1" && chmod 0644 "$1"' _ "$sysvc/docker.service"
  # A lingering user manager starts asynchronously, so wait for its bus before talking to it.
  run loginctl enable-linger "$user"
  for _ in $(seq 30); do
    if [ "$dry" = 1 ] || [ -S "/run/user/$uid/bus" ]; then break; fi
    sleep 1
  done
  userctl enable docker
  userctl start docker
else
  as_user mkdir -p "$svc/init.d" "$svc/runlevels/default"
  sed "s|@EXTRAS@|$extras|g" "$here/templates/openrc/dockerd" |
    as_user sh -c 'cat > "$1" && chmod 0755 "$1"' _ "$svc/init.d/dockerd"
  as_user ln -sfn ../../init.d/dockerd "$svc/runlevels/default/dockerd"
fi
# Login shells (su - omc-<name>) talk to the instance's own daemon and user services.
grep -qs DOCKER_HOST "$home/.bash_profile" ||
  printf '%s\n' 'export XDG_RUNTIME_DIR=/run/user/$(id -u)' 'export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock' |
  as_user sh -c 'cat >> "$1"' _ "$home/.bash_profile"
if [ "$(omc_init)" = openrc ]; then
  run ln -sfn user "/etc/init.d/user.$user"
  run rc-update add "user.$user" default
  run rc-service "user.$user" start
fi
# Recovers a dockerd that failed its first start; the session may already be starting it.
if [ "$(omc_init)" = openrc ]; then
  run su - "$user" -c "rc-service -U dockerd status >/dev/null 2>&1 || rc-service -U dockerd start" || true
fi

# 6. Wait for the instance's daemon to answer as rootless.
if [ "$dry" = 0 ]; then
  for _ in $(seq 60); do
    su - "$user" -c 'docker info --format "{{.SecurityOptions}}"' 2>/dev/null | grep -q rootless && break
    sleep 2
  done
  su - "$user" -c 'docker info --format "{{.SecurityOptions}}"' | grep -q rootless || die "rootless Docker for $user did not come up (see $home/.local/state/dockerd.log, and: systemctl --user status docker / rc-service -U dockerd status)"
fi
echo "host-prep: instance '$name' ready as user $user"
