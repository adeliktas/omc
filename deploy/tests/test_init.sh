#!/usr/bin/env bash
# The deploy scripts drive OpenRC or systemd, and emerge or apt, whichever the host has.
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
# shellcheck source=../lib.sh
. "$root/deploy/lib.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

check "OMC_INIT overrides detection" '[ "$(OMC_INIT=systemd omc_init)" = systemd ]'
if [ -d /run/systemd/system ]; then want=systemd; elif [ -d /run/openrc ]; then want=openrc; else want=none; fi
check "detects this host's init ($want)" '[ "$(omc_init)" = "$want" ]'

# omc_svc: the same actions become rc-service/rc-update -U or systemctl --user calls.
mkdir -p "$tmp/bin"
for c in rc-service rc-update systemctl; do
  printf '#!/bin/sh\necho "%s $*" >> "%s/calls"\n' "$c" "$tmp" > "$tmp/bin/$c"
  chmod +x "$tmp/bin/$c"
done
calls() { : > "$tmp/calls"; PATH="$tmp/bin:$PATH" OMC_INIT=$1 omc_svc "${@:2}"; cat "$tmp/calls"; }
check "openrc start" '[ "$(calls openrc start x)" = "rc-service -U x start" ]'
check "openrc status" '[ "$(calls openrc status x)" = "rc-service -U x status" ]'
check "openrc enable" '[ "$(calls openrc enable x)" = "rc-update -U add x default" ]'
check "openrc disable" '[ "$(calls openrc disable x)" = "rc-update -U del x default" ]'
check "openrc daemon-reload is a no-op" '[ -z "$(calls openrc daemon-reload)" ]'
check "systemd start" '[ "$(calls systemd start x)" = "systemctl --user start x" ]'
check "systemd status" '[ "$(calls systemd status x)" = "systemctl --user is-active --quiet x" ]'
check "systemd enable" '[ "$(calls systemd enable x)" = "systemctl --user enable x" ]'
check "systemd disable" '[ "$(calls systemd disable x)" = "systemctl --user disable x" ]'
check "systemd daemon-reload" '[ "$(calls systemd daemon-reload)" = "systemctl --user daemon-reload" ]'
check "no init system is an error" '! OMC_INIT=none omc_svc start x 2>/dev/null'

# Package names follow the package manager; with nothing installed every package is missing.
mkdir -p "$tmp/empty"
apt=$(PATH=$tmp/empty OMC_INIT=systemd omc_missing_packages apt)
emerge=$(PATH=$tmp/empty OMC_INIT=openrc omc_missing_packages emerge)
for p in docker-ce docker-ce-cli docker-compose-plugin uidmap slirp4netns bubblewrap git curl cron python3 dbus-user-session; do
  check "apt needs $p" 'grep -qw -- "$p" <<<"$apt"'
done
check "apt names no Gentoo atoms" '! grep -q / <<<"$apt"'
for p in app-containers/docker app-containers/docker-compose sys-apps/shadow app-containers/slirp4netns sys-process/cronie dev-lang/python; do
  check "emerge needs $p" 'grep -qw -- "$p" <<<"$emerge"'
done
check "emerge on OpenRC skips the D-Bus user session" '! grep -q dbus <<<"$emerge"'

# Service files land where each init system reads them.
OMC_INIT=openrc HOME=$tmp/h1 omc_render_services "$root/deploy/templates" /i/omc /i/omc/data mc-team-pool mc-review-agent openclaw-gateway
for s in mc-team-pool mc-review-agent openclaw-gateway; do
  sed "s|@OMC_HOME@|/i/omc|g; s|@OMC_DATA@|/i/omc/data|g" "$root/deploy/templates/openrc/$s" > "$tmp/want"
  check "openrc $s rendered as before" 'cmp -s "$tmp/want" "$tmp/h1/.config/rc/init.d/$s"'
  check "openrc $s executable" '[ -x "$tmp/h1/.config/rc/init.d/$s" ]'
done
OMC_INIT=systemd HOME=$tmp/h2 omc_render_services "$root/deploy/templates" /i/omc /i/omc/data mc-team-pool mc-review-agent openclaw-gateway
for s in mc-team-pool mc-review-agent openclaw-gateway; do
  f=$tmp/h2/.config/systemd/user/$s.service
  check "systemd $s rendered" '[ -f "$f" ] && grep -q "^ExecStart=" "$f" && grep -q "^WantedBy=default.target" "$f"'
  check "systemd $s has no placeholders" '! grep -q "@[A-Z_]*@" "$f"'
done
# systemd expands $VAR in command lines itself; shell variables must be written $$VAR.
check "fleet units pass RUNNER_APPLY to the shell" 'grep -q "\$\${RUNNER_APPLY" "$tmp/h2/.config/systemd/user/mc-team-pool.service"'

# host-prep and purge pick the init system's commands (dry runs only print).
mkdir -p "$tmp/home-x"; touch "$tmp/home-x/.omc-instance"
printf '#!/bin/sh\n[ "$2" = omc-x ] && echo "omc-x:x:1500:1500::%s/home-x:/bin/bash"\nexit 0\n' "$tmp" > "$tmp/getent"
chmod +x "$tmp/getent"
out=$(OMC_INIT=systemd OMC_GETENT=$tmp/getent OMC_REGISTRY=$tmp/instances "$root/deploy/host-prep.sh" --dry-run zz9 2>&1); rc=$?
check "systemd host-prep lingers the user" '[ $rc -eq 0 ] && grep -q "+ loginctl enable-linger omc-zz9" <<<"$out"'
check "systemd host-prep writes a dockerd user unit" 'grep -q "\.config/systemd/user/dockerd.service" <<<"$out"'
check "systemd host-prep starts dockerd through systemctl --user" 'grep -q "systemctl --user enable --now dockerd" <<<"$out"'
check "systemd host-prep never calls OpenRC" '! grep -qE "rc-update|rc-service" <<<"$out"'
out=$(OMC_INIT=none OMC_GETENT=$tmp/getent OMC_REGISTRY=$tmp/instances "$root/deploy/host-prep.sh" --dry-run zz9 2>&1); rc=$?
check "host-prep refuses a host without OpenRC or systemd" '[ $rc -ne 0 ] && grep -q "OpenRC or systemd" <<<"$out" && ! grep -q "+ useradd" <<<"$out"'
out=$(OMC_INIT=systemd OMC_GETENT=$tmp/getent "$root/deploy/purge.sh" --dry-run x 2>&1); rc=$?
check "systemd purge ends lingering" '[ $rc -eq 0 ] && grep -q "+ loginctl disable-linger omc-x" <<<"$out"'
check "systemd purge never calls OpenRC" '! grep -qE "rc-update|rc-service" <<<"$out"'
out=$(OMC_INIT=openrc OMC_GETENT=$tmp/getent "$root/deploy/purge.sh" --dry-run x 2>&1)
check "openrc purge still stops user.omc-x" 'grep -q "+ rc-service user.omc-x stop" <<<"$out"'

[ "$fail" = 0 ] && echo "PASS init"
exit "$fail"
