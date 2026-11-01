#!/usr/bin/env bash
# Guided setup of this OMC instance, step by step. Run as the instance user after deploy/install.sh.
# Resumable: finished steps are remembered in data/state/setup-done and skipped.
# usage: deploy/setup.sh [--step N] [--show-token] [--components]
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=$(dirname "$here")
data=$home/data
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$HOME/.pi/agent/bin:$PATH"
only="" show=0 components=0
while [ $# -gt 0 ]; do
  case $1 in
    --step) only=$2; shift ;;
    --show-token) show=1 ;;
    --components) components=1 ;;
    *) echo "usage: deploy/setup.sh [--step N] [--show-token] [--components]" >&2; exit 2 ;;
  esac
  shift
done
set -a; . "$data/.env"; set +a
. "$here/lib.sh"
MC=${OMC_SETUP_MC_URL:-http://127.0.0.1:$BACKEND_PORT}
UI=http://localhost:$FRONTEND_PORT
OR=http://localhost:$OMNIROUTE_PORT
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
done_file=$data/state/setup-done
mkdir -p "$data/state" && touch "$done_file"
[ "$components" = 0 ] || exec python3 "$here/components.py"

api() { curl -fsS -H "Authorization: Bearer $LOCAL_AUTH_TOKEN" -H "Content-Type: application/json" "$@"; }
items() { python3 -c 'import json, sys; d = json.load(sys.stdin); d = d.get("items", d.get("data", [])) if isinstance(d, dict) else d; print("\n".join(str(x.get("id", "")) for x in d))'; }  # MC pages or OpenAI lists
show_url() { echo "  -> $1"; if [ -n "${DISPLAY:-}" ] && command -v xdg-open >/dev/null; then xdg-open "$1" >/dev/null 2>&1 || true; fi; }
ask() { local a=""; [ -t 0 ] && read -rp "  $1" a; echo "$a"; }
ask_secret() { local a=""; if [ -t 0 ]; then read -rsp "$1" a; echo >&2; fi; printf '%s' "$a"; }
set_env() { # <key> <value>: replace or append in data/.env
  grep -q "^$1=" "$data/.env" && sed -i "s|^$1=.*|$1=$2|" "$data/.env" || echo "$1=$2" >> "$data/.env"
}

info_1() { echo "  Checks that this instance's containers are up."; }
check_1() {
  curl -fsS -o /dev/null "$MC/health" || { echo "  backend not healthy at $MC/health"; return 1; }
  local want have
  want=$(cd "$home" && docker compose config --services | sort)
  have=$(cd "$home" && docker compose ps --status running --services | sort)
  [ "$want" = "$have" ] || { echo "  not running: $(comm -23 <(echo "$want") <(echo "$have") | tr '\n' ' ')"; return 1; }
}

info_2() {
  echo "  Open Mission Control and log in with the token LOCAL_AUTH_TOKEN from $data/.env."
  [ "$show" = 1 ] && echo "  token: $LOCAL_AUTH_TOKEN"
  show_url "$UI"
}
check_2() { api -o /dev/null "$MC/api/v1/boards" || { echo "  the API refused the token"; return 1; }; }

info_3() {
  cat <<EOF
  Connect your LLM in this instance's OmniRoute (docs/install.md has the same walkthrough):
    1. Open the dashboard and log in with INITIAL_PASSWORD from $data/.env.omniroute.
    2. Providers -> "Add OpenAI Compatible": a name, a prefix, base URL http://<your-server>:<port>/v1.
       Open the new provider -> Connections -> Add (API key only if your server needs one) -> Save,
       then "Import from /models".
    3. Combos -> Create Combo named  omniroute-auto  with your provider and model as its step
       (Mission Control's planner, executor and verifier tiers fall back to it).
    4. API Keys -> Create API Key; paste it below so Mission Control can call OmniRoute.
EOF
  show_url "$OR/dashboard"
  local url key
  url=$(ask "Your server's /v1 base URL, to list its models (blank to skip): ")
  if [ -n "$url" ]; then  # best effort: what to type into OmniRoute
    curl -fsS "${url%/}/models" | python3 -c 'import json, sys
for m in json.load(sys.stdin).get("data", []):
    print("    model %s  context %s" % (m.get("id"), m.get("max_model_len") or m.get("context_length") or "?"))' ||
      echo "    could not list $url/models"
  fi
  key=$(ask_secret "OmniRoute API key for Mission Control (hidden; blank keeps the current one): ")
  if [ -n "$key" ]; then
    set_env OMNIROUTE_RUNTIME_KEY "$key"
    (cd "$home" && docker compose up -d backend webhook-worker >/dev/null 2>&1)
    OMNIROUTE_RUNTIME_KEY=$key
  fi
}
check_3() {
  local ids
  ids=$(curl -fsS -H "Authorization: Bearer ${OMNIROUTE_RUNTIME_KEY:-}" "$OR/v1/models" | items) ||
    { echo "  OmniRoute did not answer /v1/models"; return 1; }
  grep -qx omniroute-auto <<<"$ids" || { echo "  no combo named omniroute-auto yet"; return 1; }
}

info_4() { echo "  Checks that Mission Control's backend gets an answer from OmniRoute's omniroute-auto combo."; }
check_4() {
  (cd "$home" && docker compose exec -T backend python -c '
import json, os, urllib.request
req = urllib.request.Request(os.environ["OMNIROUTE_RUNTIME_URL"].rstrip("/") + "/chat/completions",
    data=json.dumps({"model": "omniroute-auto", "max_tokens": 512,
                     "messages": [{"role": "user", "content": "Reply with the word OK."}]}).encode(),
    headers={"Authorization": "Bearer " + os.environ.get("OMNIROUTE_RUNTIME_KEY", ""), "Content-Type": "application/json"})
m = json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]  # reasoning models may answer in "reasoning"
print("  answer:", (m["message"].get("content") or m["message"].get("reasoning") or "")[:60])') ||
    { echo "  the backend could not get a completion (set the API key in step 3)"; return 1; }
}

info_5() {
  echo "  Boards run on an OpenClaw gateway, which you install yourself (harness/HARNESS-SETUP.md,"
  echo "  section OpenClaw), then register under Gateways -> Add. A free port for it: $(omc_pick_ports 1 $(( ${OMC_PORT_BASE:-$(sed -n 's/^OMC_PORT_BASE=//p' "$HOME/.omc-instance" 2>/dev/null)} + 10 )))."
  echo "  Pi and Claude Code are optional coding harnesses, described in the same file."
  for c in openclaw pi claude; do echo "    $c: $(command -v $c || echo 'not installed')"; done
  show_url "$UI/gateways"
  if [ -t 0 ]; then python3 "$here/components.py"; fi
}
check_5() {
  [ -n "$(api "$MC/api/v1/gateways" | items)" ] || { echo "  no gateway registered yet"; return 1; }
}

info_6() { echo "  Create your first board (Boards -> New board)."; show_url "$UI/boards"; }
check_6() {
  [ -n "$(api "$MC/api/v1/boards" | items)" ] || { echo "  no board yet"; return 1; }
}

info_7() { echo "  Starts the fleet: the team pool and review agent (user services) and the cron jobs."; }
check_7() {
  crontab "$data/etc/crontab" || return 1
  local s
  if ! python3 "$here/components.py" --fleet-ready; then
    sed -i 's/^RUNNER_APPLY=.*/RUNNER_APPLY=0/' "$data/etc/fleet.env"
    for s in mc-team-pool mc-review-agent; do
      if omc_svc status "$s" >/dev/null 2>&1; then omc_svc stop "$s" || return 1; fi
      omc_svc disable "$s" >/dev/null 2>&1 || true
    done
    echo "  No configured optional coding harness: fleet disarmed; OpenClaw/PEV remains available."
    return 0
  fi
  sed -i 's/^RUNNER_APPLY=.*/RUNNER_APPLY=1/' "$data/etc/fleet.env"
  for s in mc-team-pool mc-review-agent; do
    omc_svc enable "$s" >/dev/null 2>&1
    omc_svc status "$s" >/dev/null 2>&1 || omc_svc start "$s" >/dev/null 2>&1
  done
  sleep 3
  pgrep -u "$(id -u)" -f "scripts/mc-team-pool.py" >/dev/null || { echo "  the pool is not running (see $data/log/mc-claude-runner.log)"; return 1; }
}

info_8() { echo "  Runs a smoke task on your first board through Mission Control's PEV lane and OmniRoute."; }
check_8() {
  local board task state
  board=$(api "$MC/api/v1/boards" | items | head -1)
  [ -n "$board" ] || { echo "  no board (step 6)"; return 1; }
  task=$(api -X POST "$MC/api/v1/boards/$board/tasks" \
    -d '{"title": "OMC smoke test", "description": "Reply with the word OK."}' |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])') || { echo "  could not create the task"; return 1; }
  api -o /dev/null -X POST "$MC/api/v1/pev/tasks/$task/start" || { echo "  could not start PEV"; return 1; }
  for _ in $(seq 30); do  # 30 phases at most; each phase is one model call
    api -o /dev/null -X POST "$MC/api/v1/pev/tasks/$task/advance"
    state=$(api "$MC/api/v1/pev/tasks/$task" | python3 -c 'import json, sys; s = json.load(sys.stdin); print(s["status"], s.get("last_verdict_pass"))')
    case $state in done\ True) echo "  smoke task passed: $UI/boards/$board?taskId=$task"; return 0 ;;
      done*|rolled_back*|escalated*) echo "  smoke task ended '$state': $UI/boards/$board?taskId=$task"; return 1 ;; esac
    sleep 5
  done
  echo "  smoke task still '$state' after 30 phases: $UI/boards/$board?taskId=$task"
  return 1
}

titles=("" "Stack is up" "Log into Mission Control" "Connect your LLM in OmniRoute" "Mission Control reaches OmniRoute"
  "OpenClaw gateway" "First board" "Start the fleet" "Smoke task")
run_step() {
  local n=$1 a
  if [ "$n" != 7 ] && grep -qx "$n" "$done_file"; then echo "[$n] ${titles[$n]}: already done"; return 0; fi
  echo; echo "[$n] ${titles[$n]}"
  "info_$n" || return 1
  while true; do
    if "check_$n"; then grep -qx "$n" "$done_file" || echo "$n" >> "$done_file"; echo "  ok"; return 0; fi
    [ -t 0 ] || return 1
    read -rp "  [c]heck again / [s]kip / [q]uit: " a || return 1  # input closed
    case $a in s) return 0 ;; q) exit 0 ;; esac
  done
}
if [ -n "$only" ]; then run_step "$only"; exit; fi
for n in 1 2 3 4 5 6 7 8; do run_step "$n" || exit 1; done
echo; echo "Setup complete. Mission Control: $UI"
