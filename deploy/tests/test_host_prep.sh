#!/usr/bin/env bash
# host-prep refuses bad or colliding names before changing anything (Review Focus 1).
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
# shellcheck source=../lib.sh
. "$root/deploy/lib.sh"
check "accepts test1" 'omc_valid_name test1'
for bad in Test1 ../x a-b "" abcdefghijklm; do
  check "rejects '$bad'" "! omc_valid_name '$bad'"
done
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/home-x"
printf '#!/bin/sh\n[ "$2" = omc-x ] && echo "omc-x:x:1500:1500::%s/home-x:/bin/bash"\nexit 0\n' "$tmp" > "$tmp/getent"
chmod +x "$tmp/getent"
check "existing user without marker is foreign" '[ "$(OMC_GETENT=$tmp/getent omc_user_state x)" = foreign ]'
touch "$tmp/home-x/.omc-instance"
check "existing user with marker is an instance" '[ "$(OMC_GETENT=$tmp/getent omc_user_state x)" = instance ]'
check "unknown user is new" '[ "$(OMC_GETENT=$tmp/getent omc_user_state y)" = new ]'
rm "$tmp/home-x/.omc-instance"
out=$(OMC_GETENT=$tmp/getent "$root/deploy/host-prep.sh" --dry-run x 2>&1); rc=$?
check "host-prep refuses a foreign omc-x" '[ $rc -ne 0 ] && grep -q "not an OMC instance" <<<"$out"'
out=$(OMC_GETENT=$tmp/getent "$root/deploy/host-prep.sh" --dry-run 'Bad!' 2>&1); rc=$?
check "host-prep refuses an invalid name" '[ $rc -ne 0 ] && grep -q "invalid instance name" <<<"$out"'
out=$(OMC_GETENT=$tmp/getent "$root/deploy/host-prep.sh" --dry-run zz9 2>&1); rc=$?
check "dry run of a new name only prints" '[ $rc -eq 0 ] && grep -q "+ useradd" <<<"$out" && ! getent passwd omc-zz9 >/dev/null'
out=$(OMC_INIT=openrc OMC_GETENT=$tmp/getent OMC_REGISTRY=$tmp/instances "$root/deploy/host-prep.sh" --dry-run zz9 2>&1)
check "home writes run as the instance user" '[ "$(grep -c "runuser -u omc-zz9 --" <<<"$out")" -ge 4 ]'
check "no root write into the home" '! grep -E "^\+ (install -d -o|chmod 0755 /home|chown (-h )?omc-zz9:omc-zz9 /home|ln -sfn \.\./\.\./init\.d)" <<<"$out"'
# The systemd path renders a lingering user unit and leaves the OpenRC files alone.
sysout=$(OMC_INIT=systemd OMC_GETENT=$tmp/getent OMC_REGISTRY=$tmp/instances-systemd "$root/deploy/host-prep.sh" --dry-run zz9 2>&1)
check "systemd renders a lingering user unit" 'grep -q "+ loginctl enable-linger omc-zz9" <<<"$sysout" && grep -q "systemd/user/docker.service" <<<"$sysout"'
check "systemd leaves openrc alone" '! grep -q "/etc/init.d/user.omc-zz9" <<<"$sysout" && ! grep -q "rc-update add" <<<"$sysout"'
[ "$fail" = 0 ] && echo "PASS host-prep"
exit "$fail"
