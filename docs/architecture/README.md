# Architecture

> **Current scope — updated 2026-10-28.** The [fleet autonomy layer](#fleet-autonomy-layer-2026-10-28) describes what runs today. The current-state snapshot holds the 2026-10-05 source citations, controls and results. Native teams and standing routine acceptance coexist with legacy harness, gateway and PEV lanes. Dated catalogue/fork counts below are snapshots, not current inventory guarantees.

## Fork status (2026-10-01 snapshot)

This repository is a fork of [`abhi1693/openclaw-mission-control`](https://github.com/abhi1693/openclaw-mission-control),
branch `customizations-2026-08`, **665 commits ahead** of upstream's final commit as of 2026-10-28.

Upstream is **archived**. Its final commit (`6d57d4a`) is a tombstone redirecting to
[Wardn AI](https://github.com/abhi1693/wardn-ai), a full rewrite on a different codebase.
There is nothing left to pull from upstream: apart from that tombstone, this fork already
contains every upstream commit. Treat this fork as the trunk, not as a branch awaiting merges.

## What this system is

A self-hosted control plane that orchestrates LLM agents across many providers, and decides
*which model should do which work at what cost*. Upstream shipped the task/agent/board
substrate; the customizations on top turned it into a cost- and capability-aware routing
system with its own observability and self-healing loops.

The distinguishing feature versus the upstream product is the **routing spine** plus a
cloud coding-agent control plane. OmniRoute handles API endpoints; subscription coding
products remain separate agent harnesses. The ranking board controls both without
pretending a subscription CLI is an API upstream.

Work has explicit gateway and harness ownership. Within the harness lane, native team
cycles add pool → coordinator → PM → scoped workers → independent exact-SHA QA →
serialized publication/deployment → Review. Standing independent routine acceptance is a
separate lifecycle decision, not the runner's success flag. Text-oriented PEV remains
separate and refuses harness-owned tasks. The dated lane investigation
explains earlier failures; it is not the current runtime specification.

## Runtime topology

```
                        ┌──────────────────────────────────────────┐
  browser ──▶ :3000     │  frontend (Next.js)                      │
                        └───────────────┬──────────────────────────┘
                                        │ REST
                        ┌───────────────▼──────────────────────────┐
  agents  ──▶ :8000     │  backend (FastAPI)                       │
                        │   routing · control loops · proxies      │
                        └──┬────────┬────────┬─────────────┬───────┘
                           │        │        │             │
                   ┌───────▼──┐ ┌───▼───┐ ┌──▼──────────┐  │ webhooks
                   │ postgres │ │ redis │ │ traffic.db  │  │
                   │  :5432   │ │ :6379 │ │ (SQLite WAL)│  ▼
                   └──────────┘ └───────┘ └─────────────┘ webhook-worker
                           │
                           │ model calls
                   ┌───────▼────────────────┐      ┌────────────────────────┐
                   │ OmniRoute :20128/:20129│      │ OpenClaw gateway :18789│
                   │ model gateway + catalog│◀────▶│ host process (not a    │
                   └───────┬────────────────┘      │ container)             │
                           │                       └────────────────────────┘
          ┌────────────────┼─────────────────┐
          ▼                ▼                 ▼
    cloud providers   local vLLM        local ollama
    (16 connections)  (Qwen3.8-27B)     (jetson / desktop)
```

`db` and `redis` publish on `127.0.0.1` only. `backend`, `frontend`, `omniroute` and the
gateway bind `0.0.0.0` — see deployment/nuc-host.md for what
that means on the current host, and `compose.hardened.yml` for the loopback-only overlay.

## Components

| Component | Form | Port | Role |
|---|---|---|---|
| `frontend` | container, Next.js | 3000 | Operator UI: boards, pipeline, traffic, office, ranking |
| `backend` | container, FastAPI | 8000 | OpenAPI-described routing, control loops, team/review APIs and provider proxies |
| `db` | container, Postgres 16 | 5432 (lo) | Control-plane state: tasks, agents, boards, approvals |
| `redis` | container, Redis 7 | 6379 (lo) | Queues, cache, OmniRoute DB 3 |
| `webhook-worker` | container | — | Async webhook delivery |
| `omniroute` | container, patched image | 20128/20129 | Model gateway: catalog, combos, provider fan-out |
| OpenClaw gateway | **host process** | 18789 | Agent runtime; owns `~/.openclaw` state |

The gateway is deliberately *not* containerised: it owns the OpenClaw state directory and its
SQLite store, and is supervised by the host init system.

## The routing spine

This is the part that does not exist upstream.

1. **`complexity_assessor`** scores the incoming task.
2. **`model_router`** (T80, rewritten T103) picks a model from complexity + IQ score +
   provider priority, and returns an **OmniRoute-canonical model id**. Agents send that id as
   their `model`; they never address a provider directly.
3. **`model_ranking`** (T110) supplies the level/rank/budget/pricing metadata the picker reads
   — the ranking board is the policy surface for "which tier may spend what".
4. **`provider_failure_gate`** (T111) removes providers from selection based on **real traffic
   errors** recorded in the traffic SQLite, not `<base_url>/models` probes. This was added
   because probes reported `llamacpp` healthy while every routed request to it failed.
5. **`omniroute_sync` / `omniroute_combo_sync`** keep the MC-side catalog aligned with
   OmniRoute's.
6. **`provider_benchmark`** and **`model_eval` / `llm_evaluator`** feed measured quality back
   into the ranking.

The patched OmniRoute image matters here: it adds `dispatchEvent()` calls on provider and
node mutation routes so MC reconciles **event-driven via webhook** instead of polling.

## Cloud coding-agent orchestration

The native team pool runs durable, opt-in team tasks
concurrently; the legacy runner retains the single-run
and adviser path. Explicit harness lane selection is supported; title markers are legacy
compatibility, not a UI prerequisite. The native team PM is read-only, developers
have disjoint scoped worktrees, and QA is an independent invocation on the exact integration.
Changed production requires fresh QA before serialized publication/deployment.

Default pool capacity is 8 tasks / 8 workers per cycle / 24 global running workers; native
caps are 16 parallel jobs, 32 members and 16 specialists. These are admission
limits, not proven throughput. Manual disables, auth/quota holds, capabilities, explicit
pins, leases, deadlines and cancellation remain gates; spend/token windows are telemetry.
LG Codex refresh belongs exclusively to OmniRoute; native AD authorization must cover the
whole lease plus 300 seconds and future refresh ownership remains unresolved.

Publication leaves Review (recurring work returns to Inbox). A separate read-only reviewer
may accept routine work through exact-published-SHA/succeeded-independent-QA/CAS lifecycle
gates. Reviewer order is GPT-6.1 Sol → Opus 5.5 → full GLM 5.3, not the implementation
ranking order. New major architecture/security/destructive-data/goals stay human.
Jetson context metadata is not inference readiness.

## Fleet autonomy layer (2026-10-28)

Team work is driven by host processes outside the Compose stack. All of them run from the repo
checkout:

| Process | Runs as | What it does |
|---|---|---|
| `scripts/mc-team-pool.py` | user service (`mc-team-pool`) | Admits team tasks, schedules cycles and re-admits a blocked task at most twice. A third failure pauses the task, raises a `/review` item and opens one `[meta-debug]` task on the operations board. |
| `scripts/mc-team-runner.py` | Started by the pool | Runs PM → developer and specialists → independent QA → publication. Harness runs go through a `bwrap` sandbox. Every prompt starts with the ponytail rules, and coding roles also get a role-specific superpowers skill index. |
| `scripts/mc-review-agent.py` | user service (`mc-review-agent`) | Accepts routine published work independently. |
| `scripts/mc-fleet-watchdog.py` | cron, every 10 min, backing off to 4 h | Runs scripted outcome checks. A finding starts a frontier coding agent to repair the system. When that engine is out of quota, a second engine continues from a shared handoff file. |
| `scripts/omc-github-responder.py` | cron, every 2 min | Answers human comments on the mapped GitHub repo. Change requests become board tasks. |
| `scripts/omc-next-batch.py` | cron, every 15 min | Closes a settled `[backlog]` lead task and seeds the next batch from open issues. When the backlog is drained, it starts the intake, at most every 12 h. |
| `scripts/omc-intake.py` and `omc-intake-evaluate.py` | Started by next-batch | Pull tracker, wiki and chat sources through a local model. Raw data stays local; only redacted todos become GitHub issues. Progress goes to a metadata-only log. |
| `scripts/omniroute-quota-balancer.py` | cron, every 15 min | Balances subscription accounts in OmniRoute by remaining quota. |
| `scripts/mc-disk-janitor.py` | cron, daily | Prunes per-attempt worktrees, leaked test volumes and build cache. |

**Engine roles.**
- Frontier models plan and review.
- Pooled lower-cost models and local models implement.
- A draft-only local engine writes first drafts.
- Expensive engines are used sparingly.

Each model's and role's settings are on `/models`. Every installed agent skill is listed, and can
be viewed or edited, on `/skills/installed`.

## Provider catalogue

Snapshot 2026-10-01, read from the live OmniRoute catalogue rather than configuration.

**16 provider connections, 1017 addressable models.** By provider:

| Provider | Models | Note |
|---|---|---|
| openrouter | 356 | broad third-party aggregation |
| kilocode | 344 | aggregation incl. frontier/balanced/free tiers |
| dashscope | 156 | Qwen family |
| gemini | 51 | |
| opencode-zen | 40 | |
| ollama | 40 | cloud subscription |
| groq | 16 | low-latency inference |
| zai | 7 | GLM family |
| claude-code-p | 3 | Haiku / Sonnet / Opus via the CLI adapter |
| deepseek | 2 | V4 Pro / V4 Flash |
| vllm | 1 | local Qwen3.8-27B |
| llamacpp | 1 | local |

Further connections carry no catalogue entries of their own: account-based subscription and
local-runtime connections.

The point of this breadth is not choice for its own sake: it is that no single quota,
outage or price change can stall delivery. The ranking board decides which of these are
*eligible* for a given task; breadth is the pool, not the policy.

## Control loops

| Loop | Service | Behaviour |
|---|---|---|
| Watchdog | `watchdog_loop` | Scans tasks, posts nudges, escalates stale items, auto-reassigns, reconciles resolved reviews. Best-effort: never crashes the loop. |
| PEV | `pev_loop` | Planner / Executor / Verifier per-task orchestration (T105 phase 3). |
| Queue | `queue_worker` | Task queue drain. |
| Traffic ETL | `traffic_etl` | Normalises captured LLM traffic into the traffic store. |

The gateway has its **own** host-side watchdogs, outside this stack — see the deployment doc.

## Agent role profiles

Agent identity (`IDENTITY.md`) and soul (`SOUL.md`) markdown are defined **per role, not per
agent**: `backend/templates/roles/<slug>/` holds one canonical profile pair, and every agent
whose normalized `identity_profile.role` resolves to that slug renders exactly the same
profile. Free-text roles normalize deterministically (parenthetical model suffixes like
"(vLLM)" and Senior/Alpha/Beta qualifiers are not part of a role; `RE Lead` → `reverse-engineer`).
Lookup order: agent-level `identity_template`/`soul_template` override → role registry →
shared `BOARD_*` templates (the "Generalist" fallback). Two agents may differ in name,
model and tier while sharing one role definition.

## Provider proxies

Only `proxy_ollama` remains, for the ollama-native `/api/chat` and `/api/generate` shapes. It
has carried no traffic since 2026-09-23: OmniRoute's own call log is the traffic source the
`/traffic` ETL reads. `proxy_vllm` and `proxy_generic` were disabled in May and deleted on
2026-10-02.

## Data stores

| Store | Location | Holds |
|---|---|---|
| Postgres | `postgres_data` volume | Control-plane state |
| Redis | in-memory + volume | Queues, cache, OmniRoute DB 3 |
| Traffic DB | `traffic_data` volume, SQLite WAL | LLM traffic monitor (migrated off Postgres for write throughput) |
| OmniRoute data | `omniroute_data` volume | Catalog, provider nodes, `payloadRules.json` |
| OpenClaw state | `~/.openclaw/state/openclaw.sqlite` | Gateway state, sessions, transcripts, audit ledger |

The OpenClaw state DB is mounted into `backend` and `webhook-worker` **writable** — that is
deliberate (commits `ccbad41`, `156eecb`); a read-only mount breaks them.

## Interfaces

Present: Web UI, REST (OpenAPI-described), webhooks, WebSocket/SSE for live
views, and a CLI — `scripts/mc`, stdlib-only, installed as `/usr/local/bin/mc` on the
production host. `mc receipt` assembles a per-task completion receipt.

Absent, and worth knowing about when comparing against sibling projects: there is **no MCP
server interface**. See [comparison.md](comparison.md).

Work can also be driven unattended from the board — see
operations/unattended-claude.md.
