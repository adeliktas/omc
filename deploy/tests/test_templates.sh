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
[ "$fail" = 0 ] && echo "PASS templates"
exit "$fail"
