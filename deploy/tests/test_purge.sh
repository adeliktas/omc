#!/usr/bin/env bash
# purge.sh refuses anything that is not an OMC instance and changes nothing (Review Focus 2).
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/home-x"
printf '#!/bin/sh\n[ "$2" = omc-x ] && echo "omc-x:x:1500:1500::%s/home-x:/bin/bash"\nexit 0\n' "$tmp" > "$tmp/getent"
chmod +x "$tmp/getent"
before=$(sha256sum /etc/passwd /etc/subuid /etc/subgid)
for name in root alice nosuch x 'Bad!'; do
  out=$(OMC_GETENT=$tmp/getent "$root/deploy/purge.sh" --dry-run "$name" 2>&1); rc=$?
  if [ $rc -eq 0 ] || ! grep -q refusing <<<"$out"; then echo "FAIL purge accepted '$name': $out"; fail=1; fi
done
[ "$before" = "$(sha256sum /etc/passwd /etc/subuid /etc/subgid)" ] || { echo "FAIL host files changed"; fail=1; }
touch "$tmp/home-x/.omc-instance"
out=$(OMC_INIT=openrc OMC_GETENT=$tmp/getent "$root/deploy/purge.sh" --dry-run x 2>&1); rc=$?
if [ $rc -ne 0 ] || ! grep -q "+ userdel -r omc-x" <<<"$out"; then echo "FAIL dry run of a real instance: $out"; fail=1; fi
if grep -q '/etc/init.d/user.omc-x' <<<"$out"; then :; else echo "FAIL openrc purge dropped the session service"; fail=1; fi
# On systemd the instance's units and its lingering user manager go as well.
out=$(OMC_INIT=systemd OMC_GETENT=$tmp/getent "$root/deploy/purge.sh" --dry-run x 2>&1); rc=$?
if [ $rc -ne 0 ] || ! grep -q 'systemctl --user stop' <<<"$out" || ! grep -q 'loginctl disable-linger omc-x' <<<"$out"; then
  echo "FAIL systemd purge dry run: $out"; fail=1
fi
[ "$fail" = 0 ] && echo "PASS purge"
exit "$fail"
