#!/usr/bin/env bash
# Root prepares the host; non-root installs the instance. Neither route uses upstream's installer.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
repo="$tmp/checkout with spaces"
mkdir -p "$repo/deploy" "$tmp/bin"
cp "$root/install.sh" "$repo/install.sh"
cat > "$tmp/bin/id" <<'SH'
#!/bin/sh
case "$1" in -u) echo "$TEST_UID" ;; -un) echo "$TEST_USER" ;; *) exit 1 ;; esac
SH
for target in host-prep install; do
  cat > "$repo/deploy/$target.sh" <<'SH'
#!/bin/sh
printf '%s\n' "${0##*/}" "$@"
exit 37
SH
done
chmod +x "$tmp/bin/id" "$repo/deploy/"*.sh
export PATH="$tmp/bin:$PATH"
unset OMC_INSTANCE
cd "$tmp"  # Resolving deploy/ must not depend on the caller's working directory.
for route in root instance; do
  if [ "$route" = root ]; then
    export TEST_UID=0 TEST_USER=root
    target=host-prep.sh
  else
    export TEST_UID=1000 TEST_USER=omc-demo
    target=install.sh
  fi
  status=0
  "$repo/install.sh" --help 'two words' '' '*' > "$tmp/out" 2>&1 || status=$?
  printf '%s\n' "$target" --help 'two words' '' '*' > "$tmp/expected"
  [ "$status" = 37 ] && cmp -s "$tmp/expected" "$tmp/out" || {
    echo "FAIL $route dispatch: wrong target, arguments, or exit status ($status)"; exit 1;
  }
done
# Keep the real instance installer's rejection of ordinary users, before it writes any data.
cp "$root/deploy/install.sh" "$root/deploy/lib.sh" "$repo/deploy/"
export TEST_UID=1000 TEST_USER=nobody
if "$repo/install.sh" --render-env-only > "$tmp/out" 2>&1; then
  echo 'FAIL accepted a non-instance user'; exit 1
fi
grep -q 'run as an instance user' "$tmp/out"
[ ! -e "$repo/.env" ] && [ ! -e "$repo/data" ]
echo 'PASS install dispatch'
