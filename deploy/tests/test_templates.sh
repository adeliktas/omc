#!/usr/bin/env bash
# Every shipped JSON template parses, and no harness file carries a denylisted term (when the
# private denylist exists on this machine).
set -u
root=$(cd "$(dirname "$0")/../.." && pwd)
fail=0
while IFS= read -r f; do
  python3 -m json.tool "$f" >/dev/null 2>&1 || { echo "FAIL invalid JSON: ${f#"$root"/}"; fail=1; }
done < <(find "$root/harness" "$root/deploy" -name '*.json.example' -o -name 'package.json' -path '*/harness/*')
deny=${OMC_DENYLIST:-$HOME/.config/omc-export/denylist.txt}
if [ -f "$deny" ] && grep -rliF -f "$deny" "$root/harness" >/dev/null; then
  echo "FAIL denylisted term in: $(grep -rliF -f "$deny" "$root/harness" | sed "s|$root/||" | tr '\n' ' ')"; fail=1
fi
# Both inits must ship the same fleet services, and every systemd unit is a complete user unit
# whose only placeholders are the ones host-prep.sh and install.sh substitute.
for s in mc-team-pool mc-review-agent openclaw-gateway; do
  [ -f "$root/deploy/templates/openrc/$s" ] || { echo "FAIL no openrc template: $s"; fail=1; }
  [ -f "$root/deploy/templates/systemd/$s.service" ] || { echo "FAIL no systemd template: $s.service"; fail=1; }
done
for f in "$root"/deploy/templates/systemd/*.service; do
  for want in '[Unit]' '[Service]' '[Install]' 'WantedBy=default.target'; do
    grep -qF "$want" "$f" || { echo "FAIL ${f#"$root"/} lacks $want"; fail=1; }
  done
  unknown=$(grep -ohE '@[A-Z_]+@' "$f" | sort -u | grep -vE '^@(EXTRAS|OMC_HOME|OMC_DATA|HOME)@$' || true)
  [ -z "$unknown" ] || { echo "FAIL ${f#"$root"/} unknown placeholders: $unknown"; fail=1; }
  grep -qE '^Restart=' "$f" || { echo "FAIL ${f#"$root"/} never restarts"; fail=1; }
done
[ "$fail" = 0 ] && echo "PASS templates"
exit "$fail"
