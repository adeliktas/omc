# Agent harness setup

OpenClaw is the core gateway harness required for boards. Pi and Claude Code are optional coding
harnesses. `deploy/setup.sh --components`, run as the instance user, offers explicit interactive
install/configure/skip choices; nothing is installed without your choice. Missing optional
harnesses do not block base OpenClaw/PEV setup. See the [install guide](../docs/install.md) for
an attachable tmux setup terminal.

**To hand this to an agent:** "Read `harness/HARNESS-SETUP.md` and set up the harnesses I want."

Every harness section below has the same subsections, in the same order:
- Purpose
- Version
- Install
- Templates
- Check
- Wiring

The placeholders in the templates are:

| Placeholder | Value |
|---|---|
| `@OMNIROUTE_PORT@` | `OMNIROUTE_PORT` in your instance's `data/.env` |
| `@OMNIROUTE_API_KEY@` | an API key you create in this instance's OmniRoute dashboard |
| `@MODEL_ID@` | a model or combo id that this OmniRoute lists at `/v1/models` |

## Pi

### Purpose
Optional coding-fleet harness. When configured, the team runner starts Pi for PM, developer,
specialist and QA attempts. OpenClaw boards and the built-in PEV smoke task do not require Pi.

### Version
Verified baseline: `@earendil-works/pi-coding-agent` 1.4.0. The official installer selects its
current managed release; check the installed version before using it.

### Install
```bash
curl -fsSL https://pi.dev/install.sh | sh
```

Prefer `deploy/setup.sh --components` for the installer plus guided instance-local model/settings
configuration, existing-file protection, runner executable wiring and a real model check.
The extensions/packages below are optional manual additions, not prerequisites:

```bash
mkdir -p ~/.pi/agent/npm ~/.pi/agent/extensions
cp harness/pi/package.json ~/.pi/agent/npm/package.json && (cd ~/.pi/agent/npm && npm install)
```

### Templates
| Source | Destination |
|---|---|
| `harness/pi/models.json.example` | `~/.pi/agent/models.json` (fill the placeholders) |
| `harness/pi/settings.json.example` | `~/.pi/agent/settings.json` |
| `harness/pi/mcp.json.example` | `~/.pi/agent/mcp.json` (optional, for browser tools) |
| `harness/pi/APPEND_SYSTEM.md` | `~/.pi/agent/APPEND_SYSTEM.md` |
| `harness/pi/extensions/*` | `~/.pi/agent/extensions/` |

What the extensions do:
- `qwen-compaction.ts` compacts long sessions with a local model.
- `goal-limit-retry` resumes a goal after a usage-limit stop.

### Check
```bash
pi --version        # 1.4.0
```

### Wiring
`models.json` points Pi at this instance's OmniRoute. The fleet runner reads `~/.pi/agent` of the
instance user.

## OpenClaw

### Purpose
**Required for boards.** Every Mission Control board belongs to an OpenClaw gateway, which also
runs the board's OpenClaw agents.

### Version
`openclaw` 2026.9.4.

### Install
OpenClaw needs Node.js 22 or newer: `emerge net-libs/nodejs`, or unpack the official Linux
binary tarball from nodejs.org into `~/.local/node` and put its `bin/` on your `PATH`.

```bash
npm install -g --prefix ~/.local openclaw@2026.9.4
```

### Templates
| Source | Destination |
|---|---|
| `harness/openclaw/openclaw.json.example` | `~/.openclaw/openclaw.json` (fill the placeholders) |

### Check
```bash
openclaw --version  # OpenClaw 2026.9.4
```

### Wiring
- Put a long random token into `gateway.auth.token`. Pick a free port: `deploy/setup.sh --step 5`
  suggests one.
- Run `openclaw gateway --port <port>`, as a user service or by hand. With `bind: lan`, the
  instance's rootless containers reach it through the host's LAN address; they cannot reach the
  host's loopback.
- In Mission Control, go to **Gateways → Add**. Enter URL `ws://<host LAN address>:<port>` and the
  token. The first attempt reports *pairing required*. Approve Mission Control's device with
  `openclaw devices list` and then `openclaw devices approve <requestId>`, and add the gateway again.
  Keep device pairing on: without it the gateway grants no `operator.admin` scope, and provisioning
  fails.
- `scripts/gateway-watchdog.sh` can restart a stuck gateway from your crontab.

## Claude Code

### Purpose
Optional. It is a harness for engines that run through the Claude CLI.

### Version
Any current release.

### Install
```bash
curl -fsSL https://claude.ai/install.sh | bash
```

### Templates
| Source | Destination |
|---|---|
| `harness/claude/settings.json.example` | merge into `~/.claude/settings.json` |

Those settings do two things:
- they enable the ponytail hooks, so `~/.claude/ponytail` must be installed from
  `github.com/DietrichGebert/ponytail`;
- they enable the superpowers plugin.

### Check
```bash
claude --version
```

### Wiring
Log in with `claude` once as the instance user.

## Skills used by every harness

- **ponytail:** keeps generated code minimal, at level `ultra`, from `~/.config/ponytail/config.json`.
- **superpowers:** the fleet runner gives coding and QA roles a role-specific subset of these skills.

`docs/harness.md` explains how both are applied.
