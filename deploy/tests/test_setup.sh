#!/usr/bin/env bash
# setup.sh: a step's check fails until the step is done, then it is remembered and skipped.
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
tmp=$(mktemp -d)
home=$tmp/omc
mkdir -p "$home/data/state"
cp -r "$root/deploy" "$home/deploy"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
printf 'BACKEND_PORT=1\nFRONTEND_PORT=2\nOMNIROUTE_PORT=3\nLOCAL_AUTH_TOKEN=t\n' > "$home/data/.env"
echo '[]' > "$tmp/boards.json"
python3 - "$port" "$tmp/boards.json" <<'PY' &
import http.server, sys
port, boards = int(sys.argv[1]), sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = open(boards, "rb").read()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
stub=$!
trap 'kill $stub; rm -rf "$tmp"' EXIT
sleep 0.5
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -ne 0 ] && grep -q "no board yet" <<<"$out"; } || { echo "FAIL empty board list passed: rc=$rc $out"; fail=1; }
grep -qx 6 "$home/data/state/setup-done" && { echo "FAIL step 6 recorded without a board"; fail=1; }
echo '{"items": [{"id": "b1", "name": "Demo"}]}' > "$tmp/boards.json"
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -qx 6 "$home/data/state/setup-done"; } || { echo "FAIL board present did not pass: rc=$rc $out"; fail=1; }
echo '[]' > "$tmp/boards.json"
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 6 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q "already done" <<<"$out"; } || { echo "FAIL finished step was not skipped: rc=$rc $out"; fail=1; }
out=$(OMC_SETUP_MC_URL=http://127.0.0.1:$port "$home/deploy/setup.sh" --step 5 </dev/null 2>&1); rc=$?
{ [ $rc -ne 0 ] && grep -q "no gateway registered yet" <<<"$out"; } || { echo "FAIL step 5 passed without a gateway: rc=$rc $out"; fail=1; }
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
out=$(PATH="$tmp/bin:$PATH" HOME="$tmp/no-harness-home" OMC_INIT=openrc OMC_TEST_CALLS="$tmp/calls" "$home/deploy/setup.sh" --step 7 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q 'OpenClaw/PEV' <<<"$out" && grep -qx 'RUNNER_APPLY=0' "$home/data/etc/fleet.env"; } || { echo "FAIL optional-free fleet setup: rc=$rc $out"; fail=1; }
grep -q 'rc-service -U mc-team-pool stop' "$tmp/calls" || { echo 'FAIL missing optional harness did not stop coding pool'; fail=1; }
grep -q 'rc-update.*add' "$tmp/calls" && { echo 'FAIL enabled coding services without a configured harness'; fail=1; }
# The same step on a systemd host goes through systemctl --user.
printf '#!/bin/sh\necho "systemctl $*" >> "$OMC_TEST_CALLS"\nexit 0\n' > "$tmp/bin/systemctl"; chmod +x "$tmp/bin/systemctl"
: > "$tmp/calls"
out=$(PATH="$tmp/bin:$PATH" HOME="$tmp/no-harness-home" OMC_INIT=systemd OMC_TEST_CALLS="$tmp/calls" "$home/deploy/setup.sh" --step 7 </dev/null 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q 'systemctl --user stop mc-team-pool' "$tmp/calls" && grep -q 'systemctl --user disable mc-team-pool' "$tmp/calls"; } || { echo "FAIL systemd fleet disarm: rc=$rc $(cat "$tmp/calls")"; fail=1; }
grep -qE 'rc-|systemctl --user enable' "$tmp/calls" && { echo 'FAIL systemd disarm called OpenRC or enabled a service'; fail=1; }
[ "$fail" = 0 ] && echo "PASS setup"
exit "$fail"
