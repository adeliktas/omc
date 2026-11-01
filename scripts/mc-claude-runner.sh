#!/usr/bin/env bash
# mc-claude-runner — run board tasks through ranked coding-agent harnesses.
#
# Polls Mission Control for marked tasks, fans analysis across every eligible
# cloud harness, then lets the highest-ranked available harness implement and
# synthesize. Quota failures advance to the next ranked provider.
#
#   MC_URL / MC_TOKEN      passed through to scripts/mc
#   RUNNER_BOARD           board id to poll                        (required)
#   RUNNER_MARKERS         comma-separated prefixes
#                          (default "[agent],[claude],[codex]")
#   RUNNER_WORKDIR         where the agent runs (default a git worktree, see below)
#   RUNNER_APPLY=1         allow writes/commits; default is READ-ONLY advice
#   RUNNER_SESSION         session id to --resume, if any
#   RUNNER_RECURRING=1     return successful tasks to the queue so the loop continues
#   RUNNER_ENSEMBLE=1      parallel cloud advisory fan-out          (default 1)
#   RUNNER_FANOUT          maximum parallel advisers               (default 16)
#
# SAFETY — read before setting RUNNER_APPLY=1.
#   Default mode is read-only: claude may inspect the repo and the running stack
#   and must answer with findings and recommendations, which land on the board.
#   Nothing is written, nothing is deployed, the live stack is not touched.
#   RUNNER_APPLY=1 lets it edit files and commit — on a dedicated branch in a
#   separate worktree, never on the checkout the host deploys from. Even then it
#   must not restart services or docker: that stays a human decision, because an
#   unattended agent that can bounce the gateway can take the stack down with
#   nobody watching.
set -uo pipefail

PATH="$HOME/.local/bin:$PATH"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
MC="$HERE/mc"

MARKERS="${RUNNER_MARKERS:-${RUNNER_MARKER:-[agent],[claude],[codex]}}"
# Ponytail (ultra) is loaded into every harness run: the laziest change that
# works, and no speculative scaffolding. claude discovers it from ~/.claude;
# pi needs the path passed explicitly.
PONYTAIL_SKILL="${RUNNER_PONYTAIL_SKILL:-$HOME/.claude/skills/ponytail}"
[ -d "$PONYTAIL_SKILL" ] || PONYTAIL_SKILL=""
APPLY="${RUNNER_APPLY:-0}"
BOARD="${RUNNER_BOARD:-}"
LOCK="${OMC_DATA:-}/run/mc-claude-runner.lock"
LOG="${RUNNER_LOG:-${OMC_DATA:-/var}/log/mc-claude-runner.log}"
WORKDIR="${RUNNER_WORKDIR:-$HOME/claude-work}"
BRANCH="${RUNNER_BRANCH:-claude/auto}"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >>"$LOG"; }

task_state() {
  "$MC" get "/api/v1/boards/$BOARD/tasks" --json 2>/dev/null | python3 -c '
import json,sys
tid=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(0)
for task in (data if isinstance(data,list) else data.get("items",[])):
    if task.get("id")==tid:
        print(str(task.get("status","")).lower(), task.get("updated_at", ""), sep="\t"); break
' "$TASK_ID"
}

[ -n "$BOARD" ] || { log "FATAL: RUNNER_BOARD unset"; exit 78; }
if ! command -v claude >/dev/null && ! command -v pi >/dev/null; then
  log "FATAL: neither claude nor pi is on PATH"
  exit 78
fi

# Team-mode tasks are handed to mc-team-runner BEFORE this legacy lane takes
# the global lock: the pool may drive many team cycles concurrently, and each
# team runner serializes only its own publication/deployment through the same
# global lock (see mc-team-runner.py). Claiming here is lock-free because the
# team runner's per-task /run/mc-team-<uuid>.lock and the API's fenced claim
# make a double start impossible.

# Claim exactly one task per tick: bounded blast radius, and a failing task
# cannot starve the queue by being retried in a tight loop.
task_json="$("$MC" get "/api/v1/boards/$BOARD/tasks" --json 2>>"$LOG")" || {
  log "cannot reach Mission Control; skipping tick"; exit 0; }

IFS=$'\t' read -r TASK_ID COMPLEXITY TASK_BUDGET TASK_WORKSPACE TASK_REPEATS TASK_PROVIDER TASK_MODEL TASK_THINKING TASK_MODE TASK_TITLE < <(printf '%s' "$task_json" | python3 -c '
import json, sys
markers = tuple(filter(None, sys.argv[1].split(",")))
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = d if isinstance(d, list) else d.get("items", [])
def owns(t):
    """This lane owns the task when it says so, else fall back to the prefix.

    `execution_lane` is the explicit, UI-settable selector. The title prefix
    is the legacy convention and stays supported so existing boards keep
    working, but a task that names the other lane is never claimed here.
    """
    lane = (t.get("execution_lane") or "").strip().lower()
    if lane:
        return lane == "harness"
    return (t.get("title") or "").startswith(markers)


def ready(t):
    """A task whose upstream steps are unfinished is not ready to run.

    This is the pipeline gate: step 3 depends on step 2, so it stays unclaimed
    until the owner moves step 2 to done. Without this the runner claimed by
    status alone and every step of a pipeline fired at once, with step 3
    editing the site before a human had checked step 2 output.
    """
    if t.get("is_blocked"):
        return False
    if t.get("blocked_by_task_ids") or []:
        return False
    if t.get("scheduled_at"):
        from datetime import datetime, timezone
        try:
            due = datetime.fromisoformat(str(t["scheduled_at"]).replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        if due > datetime.now(timezone.utc):
            return False
    # A repeating task returns to inbox when a run ends; it is due again only
    # after its interval, else it would rerun on the very next tick.
    # (A team task already in_progress is a cycle to resume, not a new run.)
    if t.get("repeat_enabled") and t.get("repeat_interval_minutes") and str(t.get("status", "")).lower() == "inbox":
        from datetime import datetime, timedelta, timezone
        last = datetime.fromisoformat(str(t.get("updated_at"))[:26])
        now = datetime.now(timezone.utc).replace(tzinfo=None)  # MC stores naive UTC
        if now - last < timedelta(minutes=int(t["repeat_interval_minutes"])):
            return False
    return True


claimable = [
    t for t in rows
    if owns(t)
    and ready(t)
    # `inbox` is the only claimable status MC defines; a native team task that is
    # already in_progress is a durable cycle this runner (sole lock holder) resumes.
    and (
        str(t.get("status", "")).lower() == "inbox"
        or (t.get("execution_mode") == "team" and str(t.get("status", "")).lower() == "in_progress")
    )
]
# Oldest-touched first. A recurring task returns to inbox every tick, so taking
# whatever the API lists first would let it starve every task added later.
claimable.sort(key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""))
if claimable:
    t = claimable[0]
    try:
        complexity = max(1, min(5, int(t.get("complexity") or 3)))
    except (TypeError, ValueError):
        complexity = 3
    budget = max(120, int(t.get("max_seconds_budget") or 3600))
    # Tab is IFS whitespace, so bash `read` collapses runs of it and an empty
    # middle field silently shifts every later field left. Emit "-" for empty
    # and translate back after reading.
    def field(value):
        return (str(value or "").replace("\n", " ").replace("\t", " ").strip()) or "-"

    print(
        t["id"], complexity, budget,
        field(t.get("workspace_path")),
        "1" if t.get("repeat_enabled") else "0",
        field(t.get("harness_provider")),
        field(t.get("harness_model")),
        field(t.get("thinking_level")),
        field(t.get("execution_mode")),
        (t.get("title") or "").replace("\n", " "),
        sep="\t",
    )
' "$MARKERS") || true

[ -n "${TASK_ID:-}" ] || { log "no claimable task"; exit 0; }

# "-" is the placeholder for an empty field (see the claim printer).
for _v in TASK_WORKSPACE TASK_PROVIDER TASK_MODEL TASK_THINKING TASK_MODE; do
  [ "$(eval "printf '%s' \"\$$_v\"")" = "-" ] && eval "$_v=''"
done

# Native team cycle: hand over to the team runner. This happens BEFORE the
# legacy global lock is taken; mc-team-runner.py holds a per-task lifecycle
# lock and grabs the global lock itself only while publishing/deploying.
if [ "${TASK_MODE:-}" = "team" ]; then
  if [ "$APPLY" != "1" ]; then
    log "team task $TASK_ID needs RUNNER_APPLY=1; leaving it untouched"
    exit 0
  fi
  log "team task=$TASK_ID -> mc-team-runner"
  exec python3 "$HERE/mc-team-runner.py" "$TASK_ID"
fi

# One legacy runner at a time. A second cron tick must not start a parallel
# claude; team cycles above are coordinated by the per-task locks instead.
exec 9>"$LOCK" || exit 1
flock -n 9 || { log "another runner holds the lock; skipping tick"; exit 0; }

TOTAL_TIMEOUT="${RUNNER_TIMEOUT:-3600}"
[ "$TASK_BUDGET" -lt "$TOTAL_TIMEOUT" ] && TOTAL_TIMEOUT="$TASK_BUDGET"
ADVISOR_TIMEOUT="${RUNNER_ADVISOR_TIMEOUT:-300}"
if [ "${RUNNER_ENSEMBLE:-1}" = "1" ]; then
  [ "$ADVISOR_TIMEOUT" -ge "$TOTAL_TIMEOUT" ] && ADVISOR_TIMEOUT=$((TOTAL_TIMEOUT / 2))
else
  ADVISOR_TIMEOUT=0
fi
LEAD_TIMEOUT=$((TOTAL_TIMEOUT - ADVISOR_TIMEOUT))

# A pipeline step may name its own engine. Extraction and a careful HTML edit
# want different models and different amounts of reasoning, and most steps want
# a cheap model with thinking off rather than whatever the board picks for a
# generic task. Naming one skips the ranking plan and its fallback chain: the
# step is deliberate, so a silent upgrade to an expensive model is wrong.
if [ -n "${TASK_PROVIDER:-}" ] && [ -n "${TASK_MODEL:-}" ]; then
  # A pinned engine has no fallback by design, so it must still respect a
  # known quota hold: otherwise every tick re-runs against an exhausted
  # provider. Observed burning 38 ticks in 30 minutes against a claude
  # subscription that had reported its own reset time.
  HOLD="$("$MC" get /api/v1/ranking --json 2>>"$LOG" | python3 -c '
import json, sys
from datetime import datetime, timezone
provider, model = sys.argv[1], sys.argv[2]
# LG was transferred to OmniRoute; the generic Pi auth also duplicates LG.
if provider in ("openai-codex-1", "openai-codex"):
    print("LG refresh ownership belongs to OmniRoute")
    sys.exit(0)
try:
    board = json.load(sys.stdin)
except Exception:
    print("ranking unavailable")
    sys.exit(0)
matched = False
for level in board.get("levels", []):
    for entry in level.get("entries", []):
        if entry.get("provider") != provider or entry.get("model") != model:
            continue
        matched = True
        if entry.get("write_enabled", provider != "vllm") is not True:
            print("engine is on read-only probation; legacy execution writes")
            sys.exit(0)
        if int(sys.argv[3]) > int(entry.get("max_complexity", 5)):
            print("task exceeds engine complexity ceiling")
            sys.exit(0)
        if entry.get("disabled") or entry.get("eligible") is False:
            print(entry.get("availability_reason") or "engine disabled or ineligible")
            sys.exit(0)
        until = entry.get("unavailable_until")
        if not until:
            continue
        try:
            reset = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            print("invalid availability timestamp")
            sys.exit(0)
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        if reset > datetime.now(timezone.utc):
            reason = entry.get("availability_reason") or "unavailable"
            print(reason + " until " + str(until))
if not matched:
    print("pinned engine is not registered in ranking")
' "$TASK_PROVIDER" "$TASK_MODEL" "$COMPLEXITY" 2>/dev/null)"
  if [ -n "$HOLD" ]; then
    log "task-pinned engine $TASK_PROVIDER/$TASK_MODEL is on hold ($HOLD); leaving task in inbox"
    exit 0
  fi
  PLAN="$(printf '%s\t%s\t0\n' "$TASK_PROVIDER" "$TASK_MODEL")"
  log "task-pinned engine $TASK_PROVIDER/$TASK_MODEL thinking=${TASK_THINKING:-default}"
else
ranking_json="$("$MC" get /api/v1/ranking --json 2>>"$LOG")" || {
  log "cannot read model ranking; leaving task in inbox"; exit 0; }
PLAN="$(printf '%s' "$ranking_json" | python3 -c '
import json, sys
complexity, title = int(sys.argv[1]), sys.argv[2].lower()
board = json.load(sys.stdin)
max_level = {int(k): int(v) for k, v in board["max_level_for_complexity"].items()}[complexity]
entries = [
    entry
    for level in board.get("levels", [])
    for entry in level.get("entries", [])
    if entry.get("execution") == "agent_harness"
    and entry.get("provider") not in ("openai-codex-1", "openai-codex")
    and not entry.get("disabled")
    and entry.get("eligible")
    and entry.get("write_enabled", entry.get("provider") != "vllm") is True
    and complexity <= int(entry.get("max_complexity", 5))
]
rank = lambda e: (
    int(e.get("provider_priority", 999)), -int(e.get("level", 5)),
    int(e.get("rank", 0)), -float(e.get("iq_score") or 0),
)
qualified = sorted(
    (e for e in entries if int(e.get("level", 5)) <= max_level), key=rank,
)
weaker = sorted(
    (e for e in entries if int(e.get("level", 5)) > max_level),
    key=lambda e: (int(e.get("provider_priority", 999)), int(e["level"]), int(e.get("rank", 0))),
)
for entry in qualified:
    print(entry["provider"], entry["model"], "qualified", sep="\t")
for entry in weaker:
    print(entry["provider"], entry["model"], "fallback", sep="\t")
' "$COMPLEXITY" "$TASK_TITLE")"
fi
[ -n "$PLAN" ] || { log "no eligible harness for complexity=$COMPLEXITY"; exit 0; }

# Fetch the full description — the title alone is not the instruction.
DESC="$("$MC" get "/api/v1/boards/$BOARD/tasks" --json 2>/dev/null | python3 -c '
import json,sys
tid=sys.argv[1]
d=json.load(sys.stdin); rows=d if isinstance(d,list) else d.get("items",[])
for t in rows:
    if t.get("id")==tid:
        print(t.get("description") or ""); break
' "$TASK_ID")"

# A dedicated worktree keeps the agent off the checkout the host deploys from.
if [ "$APPLY" = "1" ]; then
  RUN_DIR="$REPO"
  if [ -z "${TASK_WORKSPACE:-}" ]; then
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
      log "deployment checkout is dirty; leaving task unclaimed"
      exit 0
    fi
    # Each attempt starts clean on its own branch. Never sweep the owner's
    # pre-existing ~/claude-work edits into an automatic result commit.
    WORKDIR="$(mktemp -d "${WORKDIR}-attempt.XXXXXX")" || exit 1
    BRANCH="${BRANCH}-${TASK_ID}-${WORKDIR##*.}"
    git -C "$REPO" worktree add -b "$BRANCH" "$WORKDIR" HEAD >>"$LOG" 2>&1 \
      || { log "cannot create isolated attempt worktree; leaving task unclaimed"; exit 1; }
    RUN_DIR="$WORKDIR"
    ATTEMPT_BASE="$(git -C "$REPO" rev-parse HEAD)"
    log "isolated attempt branch=$BRANCH dir=$RUN_DIR"
  fi
  MODE_RULES="You MAY edit files and commit to the branch '$BRANCH' in this worktree.
You MUST NOT push, force-push, or touch $REPO — it is the live deployment checkout.
Leave this task's status alone (blocked for DECISION NEEDED is the one exception):
the runner moves it to review after publishing, and any other change cancels the publish.

DEPLOY — do not deploy, rebuild or restart anything yourself. After it publishes your
commits, the runner deploys from the production checkout exactly the services they
touch (backend/ -> backend + webhook-worker, frontend/ -> frontend, both through
scripts/mc-deploy.sh with health checks and automatic rollback; the Claude Code P
bridge by service restart) and reports the result on this task. omniroute, db, redis
and the gateway are never restarted.

Tests: this worktree has no virtualenv or node_modules. Run the backend suite from
backend/ with $REPO/backend/.venv/bin/python -m pytest (it imports this worktree's
code), and frontend checks after linking $REPO/frontend/node_modules into frontend/
(node_modules is gitignored, so the link is never committed)."
else
  RUN_DIR="$REPO"
  MODE_RULES="READ-ONLY TICK. Do not modify any file, do not commit, and do not change
the running system. Investigate and answer with findings and concrete recommendations.
The one exception: you MAY post comments on this task and change this task's own status
— that is how you report and escalate, and it is not a change to the system."
fi

# A task may name the folder it operates on (`workspace_path`), so one runner
# can serve this repo, a website checkout, or anything else the owner points
# at. Without it every task was forced into the runner's own checkout, which
# made "update the site in /srv/www" impossible to express.
if [ -n "${TASK_WORKSPACE:-}" ]; then
  if [ -d "$TASK_WORKSPACE" ]; then
    RUN_DIR="$TASK_WORKSPACE"
    log "task workspace override dir=$RUN_DIR"
    MODE_RULES="$MODE_RULES

WORKSPACE: this task names its own working directory, $RUN_DIR. Work there.
It is NOT this project's checkout: do not assume its layout, build system, or
git state. If it is not a git repository, there is no commit step — leave the
files in place and report what you changed."
  else
    log "task workspace does not exist; leaving task unclaimed"
    exit 0
  fi
fi

if [ "$APPLY" = "1" ]; then
  case "$(realpath "$RUN_DIR")" in
    "$(realpath "$REPO")"|"$(realpath "$REPO")"/*)
      log "refusing writes in the live deployment checkout; leaving task unclaimed"
      exit 0 ;;
  esac
fi

# The API locks the row and checks both preconditions atomically. A parallel
# worker with the same snapshot must lose the claim before invoking a model.
CLAIM_BODY="$(printf '%s' "$task_json" | python3 -c '
import json,sys
rows=json.load(sys.stdin)
rows=rows if isinstance(rows,list) else rows.get("items",[])
task=next(t for t in rows if t.get("id")==sys.argv[1])
print(json.dumps({"status":"in_progress", "expected_status":"inbox", "expected_updated_at":task["updated_at"]}))
' "$TASK_ID")" || exit 1
CLAIM="$("$MC" patch "/api/v1/boards/$BOARD/tasks/$TASK_ID" "$CLAIM_BODY" --json 2>>"$LOG")" \
  || { log "claim rejected; no model started"; exit 0; }
CLAIM_VERSION="$(printf '%s' "$CLAIM" | python3 -c 'import json,sys; print(json.load(sys.stdin)["updated_at"])')" \
  || { log "claim response invalid; no model started"; exit 1; }
log "claimed task=$TASK_ID version=$CLAIM_VERSION complexity=$COMPLEXITY budget=${TOTAL_TIMEOUT}s title=$TASK_TITLE"

# Files the owner attached to this task, as absolute paths on this host. The
# agent opens them directly (an image to analyse, a spec to read) rather than
# being handed a description of them.
ATTACHMENTS="$("$MC" get "/api/v1/boards/$BOARD/tasks/$TASK_ID/attachments" --json 2>/dev/null \
  | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    sys.exit(0)
for a in items:
    path = a.get("storage_path") or ""
    if path:
        print(f"- {path}  ({a.get(\"content_type\")}, {a.get(\"size_bytes\")} bytes, original name: {a.get(\"filename\")})")
' 2>/dev/null)"
if [ -n "$ATTACHMENTS" ]; then
  ATTACHMENT_BLOCK="
Attached files (read them from disk at these paths):
$ATTACHMENTS"
else
  ATTACHMENT_BLOCK=""
fi


GUARDRAILS="Operating rules for this run:
- Use ponytail ultra: the smallest change that works. No speculative scaffolding.
- Think before acting, but keep it short. This step is deliberately cheap.
- Stay inside the working directory for this task. Do NOT scan the filesystem
  looking for things. If a path you need is not given, ask rather than search.
- Read only the files you actually need. Do not load a whole tree into context.
- When anything is uncertain (which file, which element, whether a change is
  safe), STOP and post a comment asking the owner. A wrong edit to a live site
  costs more than a round trip. Never guess to keep moving.
- Do not touch files outside the task's scope, and never change layout, CSS or
  structure when the task is about content.
- Report what you changed in one short comment. No essays."

PROMPT="$(cat <<EOF
Use the ponytail skill for this work: the laziest solution that actually works.

You are running unattended from the Mission Control board on the production host
(this host). No human is watching this tick. You own this work: carry it as far
as it safely goes on your own rather than deferring routine judgement to a human.

$MODE_RULES

ESCALATION — how to ask, when you genuinely cannot proceed.

Do not stop for things you can decide yourself. Escalate only when continuing would
risk damage you cannot undo, or when two reasonable readings of the task lead to
materially different work and picking wrong wastes real effort.

When that happens, do not guess and do not silently stall. Put the decision on the
board where the operator will see it:

  ./scripts/mc post "/api/v1/boards/$BOARD/tasks/$TASK_ID/comments" \\
    '{"message": "**DECISION NEEDED** ... "}'

Write it so it can be answered in one word. State the blocker in one or two lines,
then give 2-4 concrete numbered options, each with its consequence, and say which
you recommend and why. Then set the task to blocked so it stops being reclaimed:

  ./scripts/mc patch "/api/v1/boards/$BOARD/tasks/$TASK_ID" '{"status":"blocked"}'

The operator answers by commenting on the task and setting it back to inbox.
Read the full comment history at the start of every tick — an answer to an earlier
DECISION NEEDED will be sitting there, and acting on it is the first thing to do.

Everything else — which file to touch, how to structure a fix, what to investigate
next, whether something is worth doing at all — is yours to decide. Reporting back
"I could have done X but did not" without a real blocker is a failed tick.

Operational context lives in docs/deployment/nuc-host.md and
docs/architecture/README.md. Read them before acting.

A headless browser is available through the camoufox MCP server (mcp__camoufox__*).
Use it when something needs the web and no CLI or MCP equivalent exists: checking
upstream release notes, reading docs or an issue tracker, verifying a rendered page.
Prefer a CLI (curl, gh, mc) when one will do — the browser is the fallback, not the
first choice. Always close the session when finished.

Task: $TASK_TITLE

$DESC
$ATTACHMENT_BLOCK

$GUARDRAILS
EOF
)"

TMP="$(mktemp -d /tmp/mc-agent-run.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# Each adviser/lead gets its own session directory: parallel Pi processes must
# not append to the same conversation. The task ID remains its visible label.
# Attempts start fresh rather than replaying stale tool state in a new worktree.
# Normalize every harness to the same files: <prefix>.raw/.json/.rc. Usage is
# posted independently, so subscription calls remain visible even on failure.
run_harness() {
  local provider="$1" model="$2" prompt="$3" run_dir="$4" prefix="$5" timeout_seconds="${6:-$LEAD_TIMEOUT}" kind pi_provider="" pi_model="" pi_dir="" rc usage account
  if [ "$provider" = "claude-code" ]; then
    kind=claude
    case "$model" in
      opus-5) pi_model=claude-opus-5 ;;
      opus-4-8) pi_model=claude-opus-4-8 ;;
      *) pi_model="$model" ;;
    esac
    local args=(-p "$prompt" --model "$pi_model" --output-format json)
    [ "$model" = "opus-5" ] && args+=(--fallback-model claude-opus-4-8)
    [ -n "${RUNNER_SESSION:-}" ] && args+=(--resume "$RUNNER_SESSION")
    [ -n "${RUNNER_PERMISSION_MODE:-}" ] && args+=(--permission-mode "$RUNNER_PERMISSION_MODE")
    (cd "$run_dir" && timeout "$timeout_seconds" claude "${args[@]}") >"$prefix.raw" 2>&1
    rc=$?
  else
    kind=pi
    case "$provider/$model" in
      openai-codex-1/*|openai-codex/*)
        printf 'LG refresh ownership belongs to OmniRoute; Pi execution denied\n' >"$prefix.raw"
        rc=78 ;;
      openai-codex-[A-Za-z0-9_-]*/*)
        account="${provider#openai-codex-}"
        case "$account" in *[!A-Za-z0-9_-]*) account="" ;; esac
        pi_provider=openai-codex; pi_model="$model"
        pi_dir="$HOME/.pi/accounts/$provider"
        [ -n "$account" ] && [ -r "$pi_dir/auth.json" ] || pi_provider=""
        ;;
      pi-neuralwatt/*) pi_provider=neuralwatt; pi_model="$model"; pi_dir="$HOME/.pi/agent" ;;
      ollama-cloud-sub/glm-5.2) pi_provider=ollama-glm; pi_model=glm-5.2; pi_dir="$HOME/.pi/agent" ;;
      ollama-cloud-sub/kimi-k3) pi_provider=ollama-kimik3; pi_model=kimi-k3; pi_dir="$HOME/.pi/agent" ;;
      ollama-cloud-sub/deepseek-v4-pro:cloud) pi_provider=ollama-deepseek; pi_model=deepseek-v4-pro:cloud; pi_dir="$HOME/.pi/agent" ;;
      deepseek-sub/*) pi_provider=deepseek; pi_model="$model"; pi_dir="$HOME/.pi/agent" ;;
      *) printf 'Unsupported harness: %s/%s\n' "$provider" "$model" >"$prefix.raw"; rc=78 ;;
    esac
    if [ -n "${pi_provider:-}" ]; then
      (cd "$run_dir" && PI_CODING_AGENT_DIR="$pi_dir" timeout "$timeout_seconds" \
        pi -p --approve --session-dir "$prefix.sessions" --session-id "mc-task-$TASK_ID" --mode json --provider "$pi_provider" \
        --model "$pi_model" --thinking "${TASK_THINKING:-low}" \
        ${PONYTAIL_SKILL:+--skill "$PONYTAIL_SKILL"} "$prompt") \
        >"$prefix.raw" 2>&1
      rc=$?
    else
      printf 'Missing or unsupported harness profile: %s/%s\n' "$provider" "$model" >"$prefix.raw"
      rc=78
    fi
  fi
  python3 "$HERE/mc-agent-result.py" "$kind" "$prefix.raw" "$provider" "$model" "$rc" \
    >"$prefix.json" 2>>"$LOG" || printf '{"text":"result parser failed","usage":{}}' >"$prefix.json"
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("effective_rc", 1))' \
    "$prefix.json" >"$prefix.rc"
  usage="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("usage",{})))' "$prefix.json")"
  "$MC" post /api/v1/ranking/usage "$usage" >>"$LOG" 2>&1 || log "WARN: usage report failed for $provider/$model"
}

# Advisory map phase: every qualified cloud model independently inspects a
# clean snapshot. Only the reduce/implementation agent may touch the worktree.
if [ "${RUNNER_ENSEMBLE:-1}" = "1" ]; then
  SNAPSHOT="$TMP/snapshot"
  mkdir -p "$SNAPSHOT"
  git -C "$RUN_DIR" archive HEAD | tar -x -C "$SNAPSHOT"
  ADVISOR_PROMPT="You are one independent adviser in a multi-model coding ensemble.
Inspect the repository snapshot for the task below. Do not edit anything. Return a concise
implementation plan, likely root causes, risky interactions, and the smallest useful checks.
A separate lead agent will synthesize all reports and implement.

Task: $TASK_TITLE

$DESC"
  n=0
  while IFS=$'\t' read -r provider model strength; do
    [ "$strength" = qualified ] || continue
    n=$((n + 1))
    [ "$n" -le "${RUNNER_FANOUT:-16}" ] || break
    log "starting adviser=$provider/$model"
    run_harness "$provider" "$model" "$ADVISOR_PROMPT" "$SNAPSHOT" "$TMP/adviser-$n" "$ADVISOR_TIMEOUT" &
  done <<<"$PLAN"
  wait

  REPORTS=""
  for normalized in "$TMP"/adviser-*.json; do
    [ -e "$normalized" ] || continue
    report="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("text", ""))' "$normalized" | tail -c 6000)"
    REPORTS+=$'\n\n--- Independent adviser report ---\n'"$report"
  done
  if [ -n "$REPORTS" ]; then
    PROMPT+=$'\n\nMULTI-MODEL ADVISORY REPORTS\nUse these as evidence, not authority. Resolve disagreements and then implement:\n'
    PROMPT+="$REPORTS"
  fi
fi

RC=78
OUT="No installed eligible harness could run this task."
HARNESS="none"
MODEL="none"
RETRYABLE_EXHAUSTED=0
declare -A FAILED_PROVIDERS=()
n=0
DEADLINE=$((SECONDS + LEAD_TIMEOUT))
while IFS=$'\t' read -r PROVIDER CANDIDATE _STRENGTH; do
  [ -n "$PROVIDER" ] || continue
  [ -n "${FAILED_PROVIDERS[$PROVIDER]:-}" ] && continue
  REMAINING=$((DEADLINE - SECONDS))
  if [ "$REMAINING" -lt 60 ]; then
    RETRYABLE_EXHAUSTED=1
    log "lead phase budget exhausted; deferring to next tick"
    break
  fi
  n=$((n + 1))
  HARNESS="$PROVIDER"
  MODEL="$CANDIDATE"
  log "running lead=$PROVIDER/$CANDIDATE apply=$APPLY dir=$RUN_DIR timeout=${REMAINING}s"
  run_harness "$PROVIDER" "$CANDIDATE" "$PROMPT" "$RUN_DIR" "$TMP/lead-$n" "$REMAINING"
  RC="$(cat "$TMP/lead-$n.rc")"
  OUT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("text", ""))' "$TMP/lead-$n.json")"
  log "$PROVIDER/$CANDIDATE exited rc=$RC output_bytes=${#OUT}"
  [ "$RC" -eq 0 ] && break

  RETRYABLE="$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1])).get("retryable") else "0")' "$TMP/lead-$n.json")"
  if [ "$RETRYABLE" = 1 ]; then
    RETRYABLE_EXHAUSTED=1
    FAILED_PROVIDERS[$PROVIDER]=1
    log "$PROVIDER/$CANDIDATE failed transiently; trying next provider"
    continue
  fi
  RETRYABLE_EXHAUSTED=0
  break  # A task/tool failure is not a reason to repeat work with another agent.
done <<<"$PLAN"

# Status alone is not ownership: another claim can also be in_progress.
# Lifecycle changes invalidate this attempt, even if they end in review/done.
# ponytail: updated_at fences state changes; durable attempt IDs and a
# publication reservation are needed before cross-host autonomous publishing.
CUR_STATUS=""; CUR_VERSION=""
IFS=$'\t' read -r CUR_STATUS CUR_VERSION < <(task_state) || true
LEASE_HELD=0
[ "$CUR_STATUS" = in_progress ] && [ "$CUR_VERSION" = "$CLAIM_VERSION" ] && LEASE_HELD=1
[ "$LEASE_HELD" = 1 ] || log "task lease lost (status=${CUR_STATUS:-unknown}); preserving current state"

# Production must run what git says it runs: deploy exactly the services the
# published commits touch. mc-deploy.sh health-checks and rolls back on failure.
deploy_published() {
  local changed report=""
  changed="$(git -C "$REPO" diff --name-only "$1" HEAD)"
  if grep -qE '^(backend/|compose.yml$)' <<<"$changed"; then
    for svc in backend webhook-worker; do
      if "$REPO/scripts/mc-deploy.sh" "$svc" >>"$LOG" 2>&1; then report+="deployed $svc; "
      else report+="DEPLOY FAILED $svc (previous image restored); "; RC=1; fi
    done
  fi
  if grep -qE '^(frontend/|compose.yml$)' <<<"$changed"; then
    if "$REPO/scripts/mc-deploy.sh" frontend >>"$LOG" 2>&1; then report+="deployed frontend; "
    else report+="DEPLOY FAILED frontend (previous image restored); "; RC=1; fi
  fi
  if grep -qx 'scripts/mc-claude-code-p.py' <<<"$changed"; then
    if rc-service mc-claude-code-p restart >>"$LOG" 2>&1; then report+="restarted mc-claude-code-p; "
    else report+="RESTART FAILED mc-claude-code-p; "; RC=1; fi
  fi
  [ -n "$report" ] || return 0
  log "$report"
  OUT+=$'\n\nDeploy: '"$report"
}

# A successful unattended change becomes the deployment checkout only when it
# is committed, still owns the task lease, and can fast-forward cleanly.
if [ "$RC" -eq 0 ] && [ "$APPLY" = "1" ] && [ "$LEASE_HELD" != 1 ]; then
  RC=75
  RETRYABLE_EXHAUSTED=1
  OUT+=$'\n\nTask lease was lost before publish; preserving the newer board state.'
elif [ "$RC" -eq 0 ] && [ "$APPLY" = "1" ] && [ -n "${TASK_WORKSPACE:-}" ]; then
  # A task workspace is the owner's own directory, not this project's
  # checkout: the edited files ARE the deliverable. There is no branch to
  # fast-forward and often no git at all, so the publish guard would fail the
  # task and requeue it forever -- redoing the same edit every tick.
  log "task workspace run: files left in place in $RUN_DIR, no publish step"
elif [ "$RC" -eq 0 ] && [ "$APPLY" = "1" ]; then
  # Agents often edit without committing ("not requested"); a successful run's
  # edits are the result, so commit them here instead of refusing to publish.
  if [ -n "$(git -C "$RUN_DIR" status --porcelain)" ]; then
    if git -C "$RUN_DIR" add -A >>"$LOG" 2>&1 \
      && git -C "$RUN_DIR" commit -q -m "runner: $TASK_TITLE" \
        -m "Board task $TASK_ID, harness $HARNESS/$MODEL; left in review for the owner." >>"$LOG" 2>&1; then
      log "committed the isolated attempt's edits on $BRANCH"
    else
      RC=1
      OUT+=$'\n\nCommit failed; result retained in the attempt worktree, not published.'
    fi
  fi
  if [ "$RC" -eq 0 ] && [ -z "$(git -C "$RUN_DIR" status --porcelain)" ] \
    && [ -z "$(git -C "$REPO" status --porcelain)" ]; then
    # Production may have moved on during the run (owner commits): replay the
    # result on top of it so the fast-forward below can still publish.
    if ! git -C "$RUN_DIR" rebase -q "$(git -C "$REPO" rev-parse HEAD)" >>"$LOG" 2>&1; then
      git -C "$RUN_DIR" rebase --abort >>"$LOG" 2>&1
      log "rebase onto production conflicted; result kept on $BRANCH unpublished"
    fi
    PRE_PUBLISH="$(git -C "$REPO" rev-parse HEAD)"
    if git -C "$REPO" merge --ff-only "$BRANCH" >>"$LOG" 2>&1; then
      log "deployment checkout fast-forwarded to $BRANCH"
      deploy_published "$PRE_PUBLISH"
      # Published: the attempt has nothing left to inspect. Failed attempts stay.
      git -C "$REPO" worktree remove "$RUN_DIR" >>"$LOG" 2>&1 \
        && git -C "$REPO" branch -q -d "$BRANCH" >>"$LOG" 2>&1
    else
      RC=1
      RETRYABLE_EXHAUSTED=1
      OUT+=$'\n\nAutomatic fast-forward failed; production was not changed.'
    fi
  else
    RC=1
    RETRYABLE_EXHAUSTED=1
    OUT+=$'\n\nUncommitted changes in the shared checkout; refusing to publish a partial result.'
  fi
fi

# An attempt that left nothing behind has nothing to inspect: a 5-minute failure
# loop kept 25 empty attempt worktrees (2026-10-04). Attempts with changes stay.
if [ -n "${ATTEMPT_BASE:-}" ] && [ -d "$RUN_DIR" ] && [ "$RUN_DIR" != "$REPO" ] \
  && [ -z "$(git -C "$RUN_DIR" status --porcelain 2>/dev/null)" ] \
  && [ "$(git -C "$RUN_DIR" rev-parse HEAD 2>/dev/null)" = "$ATTEMPT_BASE" ]; then
  git -C "$REPO" worktree remove "$RUN_DIR" >>"$LOG" 2>&1 \
    && git -C "$REPO" branch -q -D "$BRANCH" >>"$LOG" 2>&1
fi

# Post the result back to the board. Comment bodies are bounded: a runaway
# transcript must not be pushed into the database in one blob.
BODY="$(printf '%s' "$OUT" | tail -c 12000)"
printf '%s' "$BODY" | python3 -c '
import json,sys
body=sys.stdin.read()
harness, model, rc, apply = sys.argv[1:]
print(json.dumps({"message": f"**agent-runner** (harness={harness}, rc={rc}, model={model}, apply={apply})\n\n```\n{body}\n```"}))
' "$HARNESS" "$MODEL" "$RC" "$APPLY" > /tmp/mc-runner-comment.json 2>/dev/null \
  && "$MC" post "/api/v1/boards/$BOARD/tasks/$TASK_ID/comments" "$(cat /tmp/mc-runner-comment.json)" >>"$LOG" 2>&1 \
  || log "WARN: could not post result comment"
rm -f /tmp/mc-runner-comment.json

# Do not overwrite a watchdog requeue or an agent's DECISION NEEDED block.
CUR_STATUS=""; CUR_VERSION=""
IFS=$'\t' read -r CUR_STATUS CUR_VERSION < <(task_state) || true
if [ "$CUR_STATUS" != in_progress ] || [ "$CUR_VERSION" != "$CLAIM_VERSION" ]; then
  log "task=$TASK_ID lifecycle already changed to ${CUR_STATUS:-unknown}; no final transition"
  exit 0
fi

if [ "$RC" -eq 0 ]; then
  # Follow the TASK's own repeat flag. RUNNER_RECURRING is a global default
  # from the cron env; on its own it sent every finished task back to inbox,
  # so a one-shot task looped forever, redoing the same work each tick.
  if [ "${TASK_REPEATS:-}" = "1" ]; then
    NEXT=inbox
  elif [ "${TASK_REPEATS:-}" = "0" ]; then
    NEXT=review
  elif [ "${RUNNER_RECURRING:-0}" = "1" ]; then
    NEXT=inbox
  else
    NEXT=review
  fi
  # Hand it back UNASSIGNED. MC's own watchdog nudges tasks matching
  # (status='inbox' AND assigned_agent_id IS NOT NULL); MC auto-assigns inbox
  # tasks to a pool agent, that agent never replies because the work is done by
  # this runner, and after ~33 min it auto-blocks the task. With hourly ticks the
  # block always lands before the next tick, so the loop stalls permanently.
  # Clearing the assignee takes the task out of that query entirely.
  "$MC" patch "/api/v1/boards/$BOARD/tasks/$TASK_ID" \
    "{\"status\":\"$NEXT\",\"assigned_agent_id\":null,\"expected_status\":\"in_progress\",\"expected_updated_at\":\"$CLAIM_VERSION\"}" >>"$LOG" 2>&1 \
    || log "WARN: could not set $NEXT"
  log "task=$TASK_ID -> $NEXT (unassigned)"
elif [ "$RETRYABLE_EXHAUSTED" = 1 ]; then
  "$MC" patch "/api/v1/boards/$BOARD/tasks/$TASK_ID" \
    "{\"status\":\"inbox\",\"assigned_agent_id\":null,\"expected_status\":\"in_progress\",\"expected_updated_at\":\"$CLAIM_VERSION\"}" >>"$LOG" 2>&1 \
    || log "WARN: could not defer transient failure"
  log "task=$TASK_ID -> inbox (all available harnesses failed transiently)"
else
  "$MC" patch "/api/v1/boards/$BOARD/tasks/$TASK_ID" \
    "{\"status\":\"blocked\",\"expected_status\":\"in_progress\",\"expected_updated_at\":\"$CLAIM_VERSION\"}" >>"$LOG" 2>&1 \
    || log "WARN: could not set blocked"
  log "task=$TASK_ID -> blocked (non-retryable rc=$RC)"
fi
exit 0
