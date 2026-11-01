# Sibling projects: what to import, what to ignore

> **Historical external-project survey — 2026-09-21, non-governing.** Preserve the comparison and import evidence below; external repo counts and proposed priorities were not re-audited here. See the current-state snapshot for baseline `006f95f8`: native CLI/team execution now exists, spend windows are telemetry, and a completed receipt is not independent acceptance.

Survey done 2026-09-21 against the three repos in `~/Downloads/ai_repos/`.

## 1. `abhi1693/openclaw-mission-control` — our upstream

**Verdict: nothing to import. Closed.**

| | |
|---|---|
| Our fork ahead of `origin/master` | 24 commits |
| Upstream commits we lack | **1** |
| That commit | `6d57d4a` "Archive OpenClaw Mission Control and redirect to Wardn AI" |

The repository is archived. The only thing we do not have is the tombstone. There is no
backlog of upstream fixes waiting to be merged, and there will be no future ones. This fork
is the trunk.

## 2. `abhi1693/wardn-ai` — the successor

**Verdict: not cherry-pickable. A few ideas worth stealing.**

Wardn is a **full rewrite** by the same author — 373 commits, different layout
(`wardn/`, `infra/`, Antora docs, new compose). No shared history with our 1111-commit tree,
so nothing can be cherry-picked; anything adopted must be re-implemented.

It is also aimed at a different problem: governed MCP tool access for teams
(organizations, workspaces, OIDC, OpenBao secrets, approval routing). Our system is a
routing and cost-control plane for a single operator's fleet. Most of Wardn's surface is
not wanted here.

Ideas that do transfer:

- **Approvals scoped to the exact stored tool call and arguments**, rather than to a task or
  an agent. We have `approvals.py` and `approval_task_links.py`; binding an approval to the
  precise call payload is a tightening, not a rewrite.
- **Feature maturity markers** (commit `7b060f3`) — labelling surfaces as experimental in the
  UI. Cheap, and this tree has a lot of T-numbered features at varying maturity.
- **Compact sidebar / reduced dashboard scrolling** (`fcdc0b2`, `0f7b7e6`) — relevant to the
  dense operator pages, if UI work is ever on the table.

Explicitly **not** worth adopting: the organization/workspace model, OIDC, the MCP catalog
governance layer. That is a team-scale product shape we do not need.

## 3. `builderz-labs/mission-control` — the closest parallel

**Verdict: the most relevant of the three. Several real gaps.**

A self-hosted control plane for AI agents, explicitly multi-runtime (OpenClaw, Claude Code,
Codex, CrewAI, LangGraph, AutoGen). Different stack — Node/pnpm on SQLite, not
FastAPI/Postgres — so again re-implementation, not cherry-picking. But it is solving our
problem, which makes its choices worth reading.

### Gaps it exposes in our system

| Gap | Their surface | Assessment |
|---|---|---|
| **Hardened deploy overlay** | `docker-compose.hardened.yml` | **Imported** — see `compose.hardened.yml`. Directly relevant: our stack now sits on a LAN-facing box. |
| **CLI** | `pnpm mc agents list --json`, `mc tasks queue`, `mc events watch` | **Imported** — `scripts/mc`, installed as `/usr/local/bin/mc` on the host. Stdlib-only generic REST client with aliases, not 191 hand-written subcommands. |
| **MCP server interface** | Ships an MCP server | Real gap. Would let agents query and drive MC through the same protocol they already speak, instead of bespoke REST calls. |
| **Completion receipts** | "Logs show what ran. A completion receipt shows what finished." | **Imported** — `mc receipt`. Built as a *view* over data already recorded, so no schema change and no migration. |
| **Runtime adapters** | `adapters/` per runtime | Only matters if we ever drive Claude Code / Codex alongside OpenClaw. |

### Where we are ahead

Worth stating plainly, so the comparison is not read as a to-do list:

- **Cost- and capability-aware routing.** Their spend tracking is observational. Our
  `model_router` + `model_ranking` (T110) + `provider_failure_gate` (T111) chain actually
  *decides* routing from complexity, price tier and measured failure.
- **Failure gating from real traffic**, not health probes — the T111 lesson (probes called
  `llamacpp` healthy while every routed request failed) is one their probe-shaped health
  model would still hit.
- **PEV loop** (planner/executor/verifier) and the QA gate.
- **Provider proxies with native-shape capture** (`proxy_ollama` handling ollama's
  `/api/chat` and `/api/generate`, not just the OpenAI-compatible surface).

## Recommended order, if these get picked up

1. `compose.hardened.yml` — **done**, and the security case for it is live today.
2. CLI over existing REST — **done**, `scripts/mc` (see below).
3. Completion receipts — **done**, `mc receipt` (see below).
4. MCP server interface — **not done**; the one remaining item. Largest and most
   speculative: it needs a new backend dependency, a decision on which tools to expose and
   under what auth, and a backend image rebuild. Worth doing only if agents would really
   drive MC through it rather than over REST.

### The CLI

`scripts/mc`, symlinked to `/usr/local/bin/mc` on the production host.

Deliberately thin. The backend already describes all 191 routes in OpenAPI, so the CLI is a
generic REST client with a handful of aliases rather than a subcommand per endpoint — there
is nothing to keep in sync when routes change.

```bash
mc health
mc agents                       # table of identifying columns
mc tasks --board <id>
mc providers / mc ranking / mc activity --limit 20
mc get /api/v1/boards
mc post /api/v1/agents '{"name":"scout","role":"researcher"}'
mc routes                       # every path the API offers
mc agents --json                # raw JSON for scripting
```

Standard library only — no dependency added for what `urllib` already does. Token resolution:
`MC_TOKEN`, else `LOCAL_AUTH_TOKEN` parsed (never sourced) from `.env` next to the repo, so it
works from any directory. `MC_URL` defaults to `http://127.0.0.1:8000`.

Exit codes are distinct so it composes in scripts: `0` success, `1` usage, `2` auth/HTTP
error, `3` host unreachable.

Self-check: `python3 scripts/test_mc_cli.py` (asserts, no framework).

### Completion receipts

```bash
mc receipt --board <board-id> <task-id>
```

Answers "did this actually finish, and what is the evidence" in one place: status against a
terminal-status set, assignment, the activity window, PEV verification state, subtask and
comment counts, budget exhaustion, and the most recent events.

Deliberately **not** a new table. Everything in a receipt is already recorded across
`tasks`, `activity-log`, `comments`, `children` and the PEV state, so this composes them
rather than introducing a second source of truth to keep in sync — no migration, no backend
change, no redeploy. Promote it to a backend endpoint if it earns its keep.

Two honesty properties it must keep, both learned the hard way while building it:

- `activity-log` and `comments` **page** (`/comments` rejects `limit > 200`). A bare call
  returns a page, not a total, and `summary.event_count` mirrors the page too. Counts are
  therefore rendered as `200+` when they hit the ceiling rather than presenting a page size
  as a total.
- A sub-resource that fails to fetch reads `unavailable`, never `0` — a failed request and
  an empty collection must not look identical in an audit artifact.

Nothing here is urgent. The routing spine, which is what makes this fork worth maintaining,
has no equivalent in any of the three and needs nothing from them.
