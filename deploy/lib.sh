# Shared helpers for the OMC deploy scripts (sourced, not executed).

# An instance name: lowercase letter, then up to 11 lowercase letters/digits (user: omc-<name>).
omc_valid_name() { [[ ${1:-} =~ ^[a-z][a-z0-9]{0,11}$ ]]; }

omc_user() { echo "omc-$1"; }

# new | instance (user exists and carries ~/.omc-instance) | foreign (user exists, no marker)
omc_user_state() {
  local entry home
  entry=$(${OMC_GETENT:-getent} passwd "omc-$1") || true
  [ -z "$entry" ] && { echo new; return; }
  home=$(cut -d: -f6 <<<"$entry")
  if [ -f "$home/.omc-instance" ]; then echo instance; else echo foreign; fi
}

# First port of an instance's 20-port block, from host-prep's registry (OMC_REGISTRY); a name the
# registry does not know gets the block after the highest one.
omc_port_base() {
  local reg=${OMC_REGISTRY:-/etc/omc/instances} found
  found=$(awk -v n="$1" '$1 == n { print $2 }' "$reg" 2>/dev/null)
  if [ -n "$found" ]; then echo "$found"; return; fi
  awk 'BEGIN { m = 12980 } $2 > m { m = $2 } END { print m + 20 }' "$reg" 2>/dev/null || echo 13000
}

# Echo <count> free TCP ports, scanning upward from <start>. A port is free when nothing on the
# host listens on it (OMC_SS overrides the ss command for tests).
omc_pick_ports() {
  local count=$1 port=$2 used out=()
  used=$(${OMC_SS:-ss} -Htln 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/')
  while [ ${#out[@]} -lt "$count" ]; do
    grep -qx "$port" <<<"$used" || out+=("$port")
    port=$((port + 1))
  done
  echo "${out[*]}"
}

# The host's service manager: systemd, openrc or none (OMC_INIT overrides, for tests).
omc_init() {
  if [ -n "${OMC_INIT:-}" ]; then echo "$OMC_INIT"
  elif [ -d /run/systemd/system ]; then echo systemd
  elif [ -d /run/openrc ]; then echo openrc
  else echo none; fi
}

# One action on one of the calling user's services: start|stop|status|enable|disable <name>, or
# daemon-reload after its service files changed.
omc_svc() {
  local act=$1 s=${2:-}
  case $(omc_init) in
    systemd)
      case $act in
        status) systemctl --user is-active --quiet "$s" ;;
        daemon-reload) systemctl --user daemon-reload ;;
        *) systemctl --user "$act" "$s" ;;
      esac ;;
    openrc)
      case $act in
        enable) rc-update -U add "$s" default ;;
        disable) rc-update -U del "$s" default ;;
        daemon-reload) : ;;
        *) rc-service -U "$s" "$act" ;;
      esac ;;
    *) echo "OMC needs OpenRC or systemd as the service manager" >&2; return 1 ;;
  esac
}

# Render the user services <name>... from <templates>/{openrc,systemd}/ into the calling user's
# service directory, with @OMC_HOME@ and @OMC_DATA@ filled in.
omc_render_services() {
  local tpl=$1 home=$2 data=$3 dir ext="" s
  shift 3
  if [ "$(omc_init)" = systemd ]; then dir=$HOME/.config/systemd/user ext=.service tpl=$tpl/systemd
  else dir=$HOME/.config/rc/init.d tpl=$tpl/openrc; fi
  mkdir -p "$dir"
  for s in "$@"; do
    sed "s|@OMC_HOME@|$home|g; s|@OMC_DATA@|$data|g" "$tpl/$s$ext" > "$dir/$s$ext"
    [ -n "$ext" ] || chmod 755 "$dir/$s"
  done
}

# The host's package manager: emerge, apt or none.
omc_pkg_manager() {
  if command -v emerge >/dev/null; then echo emerge
  elif command -v apt-get >/dev/null; then echo apt
  else echo none; fi
}

# Packages (named for <pm>: emerge or apt) that provide what an instance needs and this host lacks.
# Docker comes from Docker's own apt repository on Debian/Ubuntu (their docker.io is too old).
omc_missing_packages() {
  local pm=$1 out=()
  _omc_need() { # <Gentoo atoms> <Debian packages> <test command...>
    "${@:3}" >/dev/null 2>&1 && return
    # shellcheck disable=SC2206  # each argument is a space-separated package list
    if [ "$pm" = apt ]; then out+=($2); else out+=($1); fi
  }
  _omc_need "app-containers/docker app-containers/docker-cli" "docker-ce docker-ce-cli" command -v docker
  _omc_need app-containers/docker-compose docker-compose-plugin docker compose version
  _omc_need sys-apps/shadow uidmap command -v newuidmap
  _omc_need sys-apps/bubblewrap bubblewrap command -v bwrap
  _omc_need app-containers/slirp4netns slirp4netns command -v slirp4netns
  _omc_need dev-vcs/git git command -v git
  _omc_need net-misc/curl curl command -v curl
  _omc_need sys-process/cronie cron command -v crontab
  _omc_need dev-lang/python python3 python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'
  # Rootless Docker under systemd talks to the user's manager over a D-Bus user session.
  [ "$(omc_init)" != systemd ] || _omc_need sys-apps/dbus dbus-user-session test -e /usr/lib/systemd/user/dbus.socket
  echo "${out[*]}"
}
