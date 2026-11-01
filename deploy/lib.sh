# Shared helpers for the OMC deploy scripts (sourced, not executed).

# Host family, which decides the package names: debian (Debian/Ubuntu) or gentoo.
omc_os() { [ -f /etc/debian_version ] && echo debian || echo gentoo; }

# Which init supervises this instance's user services: systemd or openrc. OMC_INIT overrides the
# detection (the deploy tests use it to run both paths on one host).
omc_init() {
  if [ -n "${OMC_INIT:-}" ]; then echo "$OMC_INIT"; return; fi
  [ -d /run/systemd/system ] && echo systemd || echo openrc
}

# The one place that names an init command: start/stop/status/enable/disable one user service,
# run as the instance user. systemd user units are reached through the caller's session runtime dir.
omc_svc() {
  local action=$1 name=$2 runtime="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [ "$(omc_init)" = systemd ]; then
    case $action in
      start|stop|status|enable|disable) env "$runtime" systemctl --user "$action" "$name" ;;
      *) echo "omc_svc: unknown action '$action'" >&2; return 2 ;;
    esac
    return
  fi
  case $action in
    enable)  rc-update -U add "$name" default ;;
    disable) rc-update -U del "$name" default ;;
    *)       rc-service -U "$name" "$action" ;;
  esac
}

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
