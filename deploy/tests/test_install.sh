#!/usr/bin/env bash
# Port picking skips used ports, and re-running install keeps secrets and ports (Review Focus 3, 4).
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# shellcheck source=../lib.sh
. "$root/deploy/lib.sh"
printf '#!/bin/sh\necho "LISTEN 0 4096 0.0.0.0:13000 0.0.0.0:*"\necho "LISTEN 0 4096 [::]:13002 [::]:*"\n' > "$tmp/ss"
chmod +x "$tmp/ss"
got=$(OMC_SS=$tmp/ss omc_pick_ports 3 13000)
[ "$got" = "13001 13003 13004" ] || { echo "FAIL omc_pick_ports: got '$got'"; fail=1; }

home=$tmp/omc; mkdir -p "$home"
cp -r "$root/deploy" "$home/deploy"
OMC_SS=$tmp/ss OMC_INSTANCE=t1 "$home/deploy/install.sh" --render-env-only >/dev/null || { echo "FAIL first render"; fail=1; }
first=$(sha256sum "$home/data/.env" "$home/data/.env.omniroute" 2>&1)
# The fleet's armed/disarmed state belongs to setup step 7; a re-run must not re-arm it.
sed -i 's/^RUNNER_APPLY=.*/RUNNER_APPLY=0/' "$home/data/etc/fleet.env"
OMC_SS=$tmp/ss OMC_INSTANCE=t1 "$home/deploy/install.sh" --render-env-only >/dev/null || { echo "FAIL second render"; fail=1; }
grep -qx 'RUNNER_APPLY=0' "$home/data/etc/fleet.env" || { echo "FAIL re-run re-armed the fleet"; fail=1; }
grep -q 'deploy/run.sh mc-fleet-watchdog.py' "$home/data/etc/crontab" || { echo "FAIL crontab not rendered"; fail=1; }
[ "$first" = "$(sha256sum "$home/data/.env" "$home/data/.env.omniroute" 2>&1)" ] || { echo "FAIL .env changed on re-run"; fail=1; }
[ "$(stat -c %a "$home/data/.env")" = 600 ] || { echo "FAIL .env mode"; fail=1; }
[ "$(stat -c %a "$home/data")" = 700 ] || { echo "FAIL data mode"; fail=1; }
grep -q '^FRONTEND_PORT=13001$' "$home/data/.env" || { echo "FAIL first port not picked"; fail=1; }
grep -q '^COMPOSE_PROJECT_NAME=omc-t1$' "$home/data/.env" || { echo "FAIL project name"; fail=1; }
# Nothing is exposed to the network by default: every published app port goes through the knob.
[ "$(grep -c 'OMC_BIND_ADDR:-127.0.0.1' "$root/compose.yml")" -ge 4 ] || { echo "FAIL compose does not default to loopback"; fail=1; }
grep -q '"0.0.0.0:' "$root/compose.yml" && { echo "FAIL compose publishes on 0.0.0.0 unconditionally"; fail=1; }
grep -qx 'OMC_BIND_ADDR=127.0.0.1' "$home/data/.env" || { echo "FAIL rendered env lacks the loopback default"; fail=1; }
[ "$(grep -c '@[A-Z_]*@' "$home/data/.env")" = 0 ] || { echo "FAIL unrendered placeholder"; fail=1; }
tok=$(sed -n 's/^LOCAL_AUTH_TOKEN=//p' "$home/data/.env")
[ ${#tok} -ge 50 ] || { echo "FAIL token shorter than 50"; fail=1; }
[ "$(readlink "$home/.env")" = data/.env ] || { echo "FAIL .env symlink"; fail=1; }
# A regular .env (a non-instance checkout, e.g. the original deployment) is never touched.
prod=$tmp/prod; mkdir -p "$prod"; cp -r "$root/deploy" "$prod/deploy"; echo "SECRET=keep" > "$prod/.env"
before=$(sha256sum "$prod/.env")
OMC_SS=$tmp/ss OMC_INSTANCE=t1 "$prod/deploy/install.sh" --render-env-only >/dev/null 2>&1 && { echo "FAIL install accepted a regular .env"; fail=1; }
[ "$before" = "$(sha256sum "$prod/.env")" ] && [ ! -e "$prod/data" ] || { echo "FAIL regular .env or data/ changed"; fail=1; }
# Without OMC_INSTANCE, only an omc-<name> user may install.
rm -f "$prod/.env"
OMC_SS=$tmp/ss "$prod/deploy/install.sh" --render-env-only >/dev/null 2>&1 && [ "$(id -un)" != "omc-"* ] && { echo "FAIL a non-instance user could install"; fail=1; }
# The port block starts at the base host-prep assigned.
b=$tmp/b; mkdir -p "$b"; cp -r "$root/deploy" "$b/deploy"
OMC_SS=$tmp/ss OMC_INSTANCE=t2 OMC_PORT_BASE=14000 "$b/deploy/install.sh" --render-env-only >/dev/null || { echo "FAIL render with a base"; fail=1; }
grep -q '^FRONTEND_PORT=14000$' "$b/data/.env" || { echo "FAIL port base not used"; fail=1; }
reg=$tmp/instances; printf 'a 13000\nb 13020\n' > "$reg"
[ "$(OMC_REGISTRY=$reg omc_port_base c)" = 13040 ] || { echo "FAIL new instance base: $(OMC_REGISTRY=$reg omc_port_base c)"; fail=1; }
[ "$(OMC_REGISTRY=$reg omc_port_base b)" = 13020 ] || { echo "FAIL existing instance keeps its base"; fail=1; }
[ "$fail" = 0 ] && echo "PASS install"
exit "$fail"
