#!/usr/bin/env bash
# setup.sh: a step's check fails until the step is done, then it is remembered and skipped.
set -u
unset DISPLAY  # a forwarded X11 display would let these runs open a browser window
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
tmp=$(mktemp -d)
home=$tmp/omc
mkdir -p "$home/data/state"
cp -r "$root/deploy" "$home/deploy"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
printf 'BACKEND_PORT=1\nFRONTEND_PORT=2\nOMNIROUTE_PORT=3\nLOCAL_AUTH_TOKEN=t\n' > "$home/data/.env"
echo '[]' > "$tmp/boards.json"
python3 - "$port" "$tmp" <<'PYSTUB' &
import http.server, sys, pathlib
port, root = int(sys.argv[1]), pathlib.Path(sys.argv[2])
FILES = {"/api/v1/boards": "boards.json", "/api/v1/gateways": "gateways.json",
         "/api/v1/agents": "agents.json"}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = FILES.get(self.path.split("?")[0], "boards.json")  # boards.json doubles for /v1/models
        f = root / name
        body = f.read_bytes() if f.exists() else b"[]"
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PYSTUB
stub=$!
trap 'kill $stub; rm -rf "$tmp"' EXIT
sleep 0.5
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -ne 0 ] && grep -q "no board yet" <<<"$out"; } || { echo "FAIL empty board list passed: rc=$rc $out"; fail=1; }
grep -qx 6 "$home/data/state/setup-done" && { echo "FAIL step 6 recorded without a board"; fail=1; }
echo '{"items": [{"id": "b1", "name": "Demo"}]}' > "$tmp/boards.json"
echo '{"items": [{"id": "a1", "board_id": "b1", "is_board_lead": false}]}' > "$tmp/agents.json"
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -ne 0 ] && grep -q "no board has a lead agent" <<<"$out"; } || { echo "FAIL a board without a lead passed: rc=$rc $out"; fail=1; }
echo '{"items": [{"id": "a1", "board_id": "b1", "is_board_lead": true}]}' > "$tmp/agents.json"
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -qx 6 "$home/data/state/setup-done"; } || { echo "FAIL board with a lead did not pass: rc=$rc $out"; fail=1; }
echo '[]' > "$tmp/boards.json"
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q "already done" <<<"$out"; } || { echo "FAIL finished step was not skipped: rc=$rc $out"; fail=1; }
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 5 </dev/null 2>&1); rc=$?
{ [ $rc -ne 0 ] && grep -q "no gateway registered yet" <<<"$out"; } || { echo "FAIL step 5 passed without a gateway: rc=$rc $out"; fail=1; }
# A registered gateway must teach the backend its address: without it the watchdog reports the
# gateway down and nothing is dispatched (2026-11-02).
printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/docker"; chmod +x "$tmp/bin/docker"
echo '{"items": [{"id": "g1", "url": "ws://192.0.2.10:13010", "token": "gw-token"}]}' > "$tmp/gateways.json"
out=$(PATH="$tmp/bin:$PATH" OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 5 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q "reaches the gateway at 192.0.2.10:13010" <<<"$out"; } || { echo "FAIL step 5 did not record the gateway: rc=$rc $out"; fail=1; }
grep -qx 'OPENCLAW_GATEWAY_HOST=192.0.2.10' "$home/data/.env" && grep -qx 'OPENCLAW_GATEWAY_PORT=13010' "$home/data/.env" &&
  grep -qx 'OPENCLAW_GATEWAY_TOKEN=gw-token' "$home/data/.env" || { echo "FAIL gateway env not written"; fail=1; }
# Step 3 reads an OpenAI-style model list ({"data": [...]}) from this instance's OmniRoute.
echo '{"object": "list", "data": [{"id": "auto/best-chat"}, {"id": "omniroute-auto"}]}' > "$tmp/boards.json"
sed -i "s/^OMNIROUTE_PORT=.*/OMNIROUTE_PORT=$port/" "$home/data/.env"
out=$("$home/deploy/setup.sh" --step 3 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -qx 3 "$home/data/state/setup-done"; } || { echo "FAIL step 3 missed omniroute-auto in a data list: rc=$rc $out"; fail=1; }
# No optional coding harness: stop/disarm only user coding services, but base setup succeeds.
mkdir -p "$tmp/bin" "$home/data/etc"
printf 'RUNNER_APPLY=1\n' > "$home/data/etc/fleet.env"
printf '# instance cron\n' > "$home/data/etc/crontab"
for c in crontab rc-service rc-update pgrep; do
  printf '#!/bin/sh\necho "%s $*" >> "$OMC_TEST_CALLS"\nexit 0\n' "$c" > "$tmp/bin/$c"
  chmod +x "$tmp/bin/$c"
done
out=$(OMC_INIT=openrc PATH="$tmp/bin:$PATH" HOME="$tmp/no-harness-home" OMC_TEST_CALLS="$tmp/calls" "$home/deploy/setup.sh" --step 7 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q 'OpenClaw/PEV' <<<"$out" && grep -qx 'RUNNER_APPLY=0' "$home/data/etc/fleet.env"; } || { echo "FAIL optional-free fleet setup: rc=$rc $out"; fail=1; }
grep -q 'rc-service -U mc-team-pool stop' "$tmp/calls" || { echo 'FAIL missing optional harness did not stop coding pool'; fail=1; }
grep -q 'rc-update.*add' "$tmp/calls" && { echo 'FAIL enabled coding services without a configured harness'; fail=1; }
# The same step on systemd reaches the user manager, never rc-*.
for c in crontab systemctl pgrep; do
  printf '#!/bin/sh\necho "%s $*" >> "$OMC_TEST_CALLS"\nexit 0\n' "$c" > "$tmp/bin/$c"
  chmod +x "$tmp/bin/$c"
done
printf 'RUNNER_APPLY=1\n' > "$home/data/etc/fleet.env"
out=$(OMC_INIT=systemd PATH="$tmp/bin:$PATH" HOME="$tmp/no-harness-home" OMC_TEST_CALLS="$tmp/calls2" "$home/deploy/setup.sh" --step 7 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -qx 'RUNNER_APPLY=0' "$home/data/etc/fleet.env"; } || { echo "FAIL systemd fleet disarm: rc=$rc $out"; fail=1; }
grep -q 'systemctl --user stop mc-team-pool' "$tmp/calls2" || { echo 'FAIL systemd path did not stop the coding pool'; fail=1; }
grep -q 'systemctl --user disable mc-team-pool' "$tmp/calls2" || { echo 'FAIL systemd path did not disable the coding pool'; fail=1; }
grep -q 'rc-service' "$tmp/calls2" && { echo 'FAIL systemd path called rc-service'; fail=1; }
# No automated run may open a browser window: with no tty there is no xdg-open, even with $DISPLAY set.
printf '#!/bin/sh\necho "$*" >> "$OMC_TEST_CALLS.xdg"\n' > "$tmp/bin/xdg-open"
chmod +x "$tmp/bin/xdg-open"
out=$(DISPLAY=:99 OMC_INIT=openrc PATH="$tmp/bin:$PATH" HOME="$tmp/no-harness-home" OMC_TEST_CALLS="$tmp/calls3" "$home/deploy/setup.sh" --step 5 </dev/null 2>&1); rc=$?
{ grep -q 'localhost:2/gateways' <<<"$out"; } || { echo "FAIL step 5 did not print the gateway URL: $out"; fail=1; }
[ ! -e "$tmp/calls3.xdg" ] || { echo 'FAIL an automated run tried to open a browser'; fail=1; }
[ "$fail" = 0 ] && echo "PASS setup"
exit "$fail"
