#!/usr/bin/env bash
# Generate the public OMC repo from this private repo's committed HEAD, through fail-closed leak gates.
# usage: scripts/omc-export.sh [--target DIR] [--denylist FILE] [--allowlist FILE]
#   exit 0: exported (one commit) or nothing changed; 1: a gate failed, nothing committed; 2: setup error
#
# Ships only tracked files the rules in scripts/omc-export.paths select (scripts/omc_ship.py). The
# denylist and allowlist are private: they live outside every repo and are never copied, printed in
# full or committed. The target never gets a remote; pushing is the owner's separate step.
set -uo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
target=$HOME/omc-public
deny=$HOME/.config/omc-export/denylist.txt
allow=$HOME/.config/omc-export/allowlist.txt
while [ $# -gt 0 ]; do
  case $1 in
    --target) target=$2; shift ;;
    --denylist) deny=$2; shift ;;
    --allowlist) allow=$2; shift ;;
    *) echo "usage: scripts/omc-export.sh [--target DIR] [--denylist FILE] [--allowlist FILE]" >&2; exit 2 ;;
  esac
  shift
done
[ -s "$deny" ] || { echo "export: denylist $deny missing or empty" >&2; exit 2; }
[ -f "$allow" ] || allow=/dev/null
gitleaks=$(command -v gitleaks || echo "$HOME/.local/bin/gitleaks")
[ -x "$gitleaks" ] || { echo "export: gitleaks not found (install a pinned release into ~/.local/bin)" >&2; exit 2; }
[ -z "$(git -C "$repo" status --porcelain -- scripts/omc-export.paths)" ] ||
  { echo "export: commit scripts/omc-export.paths first" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tree=$work/tree
mkdir -p "$tree"
python3 "$repo/scripts/omc_ship.py" "$repo" > "$work/files" || exit 2
(cd "$repo" && git archive HEAD $(cat "$work/files")) | tar -x -C "$tree" || exit 2
sha=$(git -C "$repo" rev-parse --short HEAD)
(cd "$tree" && "$gitleaks" dir . --no-banner --redact=0 --exit-code 0 --report-format json \
  --report-path "$work/gitleaks.json" >/dev/null 2>&1) || { echo "export: gitleaks failed to run" >&2; exit 2; }

python3 - "$repo" "$tree" "$target" "$deny" "$allow" "$work/gitleaks.json" <<'PY'
import json, re, subprocess, sys
from pathlib import Path
repo, tree, target, deny, allow, report = (Path(a) for a in sys.argv[1:])
sys.path.insert(0, str(repo / "backend"))
from app.core.evidence_redaction import _SHAPED  # the repo's own credential shapes

allowed = {tuple(line.split("\t", 2)) for line in allow.read_text().splitlines() if line.count("\t") == 2}
terms = [t.strip().lower() for t in deny.read_text().splitlines() if t.strip()]
bad = []

def finding(path, rule, match):
    match = match.replace("\n", "\\n")  # one allowlist line per finding
    if (path, rule, match) not in allowed:  # exceptions are reviewed lines: path, rule, exact match
        bad.append(f"{rule}: {path}: {match[:12]}…" if rule not in ("denylist", "filetype") else f"{rule}: {path}")

# Gate 1: secrets (gitleaks rules plus the repo's shaped-credential patterns).
for f in json.loads(report.read_text() or "[]"):
    finding(f["File"].removeprefix("./"), f["RuleID"], f["Match"])
files = sorted(p for p in tree.rglob("*") if p.is_file() or p.is_symlink())
for p in files:
    rel = str(p.relative_to(tree))
    data = p.read_bytes() if p.is_file() else b""
    text = data.decode("utf-8", "replace")
    for m in _SHAPED.finditer(text):
        finding(rel, "shaped", m.group(0))
    # Gate 2: private terms in contents (binary too) and in paths. Never prints the term itself.
    lowered = (rel + "\n" + data.decode("latin-1")).lower()
    if any(t in lowered for t in terms):
        finding(rel, "denylist", "")
    # Gate 3: file types that are data, not source.
    name = p.name.lower()
    if (re.search(r"\.(log|db|sqlite3?|sql|pem|key|jsonl)$", name) or name.startswith("auth") and name.endswith(".json")
            or (name.startswith(".env") and not name.endswith(".example"))
            or (re.search(r"\.(png|jpe?g|gif|webp)$", name) and not rel.startswith("frontend/public/"))):
        finding(rel, "filetype", "")

# Gate 4: the target's history holds export commits only.
if (target / ".git").exists():
    log = subprocess.run(["git", "-C", str(target), "log", "--format=%an <%ae>|%cn <%ce>|%s"],
                         capture_output=True, text=True).stdout.splitlines()
    for line in log:
        a, c, s = line.split("|", 2)
        if a != "OMC <omc@localhost.invalid>" or c != a or not s.startswith("export: "):
            bad.append(f"history: foreign commit '{s[:40]}'")
elif target.exists() and any(target.iterdir()):
    bad.append(f"target {target} exists and is not an export repo")

# Gate 5: no broken links in the shipped docs (they must not point at unshipped pages).
checker = tree / "scripts/check_markdown_links.py"
if checker.exists():
    r = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True)
    if r.returncode:
        bad.append("markdown links: " + (r.stdout + r.stderr).strip().replace("\n", "; ")[:400])

if bad:
    print(f"export: {len(bad)} gate finding(s); nothing committed")
    print("\n".join(sorted(set(bad))[:60]))
    sys.exit(1)
PY
rc=$?
[ $rc -eq 0 ] || exit $rc

export GIT_AUTHOR_NAME=OMC GIT_AUTHOR_EMAIL=omc@localhost.invalid GIT_COMMITTER_NAME=OMC GIT_COMMITTER_EMAIL=omc@localhost.invalid
mkdir -p "$target"
[ -d "$target/.git" ] || git -C "$target" init -q -b main
git -C "$target" rm -rq --cached --ignore-unmatch . >/dev/null
git -C "$target" clean -ffdxq  # nothing but the gated tree may enter the commit
cp -a "$tree/." "$target/"
git -C "$target" add -A -f .
diff -q <(git -C "$target" ls-files | sort) <(sort "$work/files") >/dev/null ||
  { echo "export: staged files differ from the gated tree; nothing committed" >&2; exit 2; }
if git -C "$target" diff --cached --quiet; then echo "export: no changes since the last export"; exit 0; fi
git -C "$target" -c commit.gpgsign=false commit -q -m "export: $sha" || exit 2
echo "export: $(git -C "$target" log --oneline -1) in $target ($(wc -l < "$work/files") files)"
