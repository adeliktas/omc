# Configuration

An instance keeps all of its configuration in the git-ignored `data/` directory of its clone. Git
never sees it. Back it up to keep an instance.

| Path | Mode | Written by | Contents |
|---|---|---|---|
| `data/.env` | 0600 | `install.sh`, `setup.sh` | Compose settings, ports, MC secrets (see below); `.env` links here |
| `data/.env.omniroute` | 0600 | `install.sh` | OmniRoute secrets and ports; `.env.omniroute` links here |
| `data/etc/mc-team/fleet.json` | 0600 | you | fleet features (see below); absent means off |
| `data/etc/mc-team/board-repos.json` | 0600 | you | board id → path of a clean clone the fleet publishes PRs from |
| `data/etc/fleet.env` | 0600 | `install.sh` | environment of the fleet services and cron jobs |
| `data/etc/crontab` | 0600 | `install.sh` | the instance user's crontab, installed by setup step 7 |
| `data/etc/payloadRules.json` | 0600 | `install.sh` | OmniRoute payload rules (`{"override": [], "default": []}`) |
| `data/state/`, `data/log/`, `data/run/` | 0700 | the fleet | state, logs and locks of the host scripts |

## `data/.env`

| Key | Meaning |
|---|---|
| `COMPOSE_PROJECT_NAME` | `omc-<name>` |
| `COMPOSE_PROFILES` | `omniroute` (runs the bundled OmniRoute) |
| `FRONTEND_PORT`, `BACKEND_PORT`, `POSTGRES_PORT`, `REDIS_PORT`, `OMNIROUTE_PORT`, `OMNIROUTE_PORT2` | host ports, picked free at install |
| `OMC_BIND_ADDR` | interface the UI, API and OmniRoute bind to; `127.0.0.1` (loopback only) unless you set `0.0.0.0` for LAN access |
| `OMNIROUTE_IMAGE` | OmniRoute image tag (`omniroute:omc-local`, built by `omniroute/build.sh`) |
| `AUTH_MODE`, `LOCAL_AUTH_TOKEN` | Mission Control login (`local` mode, generated 64-character token) |
| `POSTGRES_PASSWORD` | database password (generated) |
| `BASE_URL`, `CORS_ORIGINS`, `NEXT_PUBLIC_API_URL` | where the UI and API are reached |
| `OMNIROUTE_URL`, `OMNIROUTE_RUNTIME_URL` | OmniRoute's management and runtime APIs inside the Compose network |
| `OMNIROUTE_JWT_SECRET` | equals OmniRoute's `JWT_SECRET`; lets the backend mint its management token |
| `OMNIROUTE_RUNTIME_KEY` | an OmniRoute API key for Mission Control (setup step 3) |

Optional keys:

| Key | Meaning |
|---|---|
| `OMNIROUTE_DASHBOARD_URL` | where the Providers page redirects (default: the host the browser used, on `OMNIROUTE_PORT`, `/dashboard/providers`) |
| `OMNIROUTE_PAYLOAD_RULES` | host path of the payload rules file |
| `VLLM_HEALTH_URL` | `/v1/models` URL of a local vLLM, used to show its health and context size (unset: none) |
| `PRIVATE_BOARD_IDS` | comma-separated board ids whose content may only reach local models (unset: none) |
| `OPENCLAW_GATEWAY_HOST`, `OPENCLAW_GATEWAY_PORT`, `OPENCLAW_GATEWAY_TOKEN` | the OpenClaw gateway this instance drives: host-only address reachable from the containers (the Docker bridge), the port from `~/.openclaw/openclaw.json`, and its token. All three must be set, or Mission Control treats the gateway as down and never dispatches |
| `HOST_OPENCLAW_HOME`, `HOST_OPENCLAW_UID` | host path and owner of the mounted `~/.openclaw` |

## `data/etc/mc-team/fleet.json`

Every key is optional. A feature whose keys are missing stays off and logs `not configured`.

```json
{
  "github_repo": "example-org/example-repo",
  "github_board": "<board id that works the repo's issues>",
  "backlog_prefix": "[backlog]",
  "repo_language": "Go",
  "meta_board": "<board id for [meta-debug] tasks>",
  "private_board_ids": ["<board id>"],
  "private_lane": {
    "local_provider": "<Pi provider of the local model>",
    "local_model": "<model id>",
    "local_endpoint": "http://192.0.2.10:8000/v1",
    "router_model": "vllm/<model id>",
    "router_endpoint": "http://192.0.2.20:20129/v1"
  },
  "github_poll": {"repo": "example-org/private-repo", "repo_id": 0, "actor_id": 0, "actor_login": "you", "pr": 1},
  "intake": {
    "jira_url": "https://jira.example.com",
    "confluence_url": "https://wiki.example.com",
    "mattermost_url": "https://chat.example.com",
    "confluence_space": "ENG",
    "jira_project": "ENG",
    "channel_terms": ["engineering"],
    "search_terms": ["roadmap"],
    "team": "example engineering team",
    "work_dir": "<directory with redact.py and sync.py>"
  },
  "agent_memory": "<path of a notes file the watchdog's repair agent reads first>",
  "forbidden_paths": ["<paths agents must never see>"],
  "hidden_paths": ["<extra paths masked in agent sandboxes>"]
}
```

| Feature | Keys | What it does |
|---|---|---|
| GitHub fleet | `github_repo`, `github_board` | `omc-next-batch.py` turns open issues labelled `omc-fleet` into backlog tasks; `omc-github-responder.py` answers comments |
| Meta-debug | `meta_board` | a task failing three times gets one investigation task there |
| Private boards | `private_board_ids`, `private_lane` (and `PRIVATE_BOARD_IDS` in `.env`) | content of those boards reaches only the configured local model, through OmniRoute's private lane |
| GitHub poll | `github_poll` | polls one private PR's owner comments |
| Intake | `intake` | a local model pulls tracker, wiki and chat updates and files redacted todos as GitHub issues. Needs your own `redact.py` and `sync.py` in `work_dir` |

OmniRoute's private lane needs `OMC_PRIVATE_LANE_MODEL` and `OMC_PRIVATE_LANE_BASE_URL` in
`data/.env.omniroute`.
