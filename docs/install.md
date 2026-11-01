# Install OMC

OMC is installed as one or more **instances**. Each instance has its own:

- unprivileged user `omc-<name>` and home directory;
- rootless Docker daemon;
- ports;
- `data/` directory.

Instances never share state. Each one can be removed completely with `deploy/purge.sh`.

## Requirements

Requirements are the same Gentoo stack OMC is developed on:

- **System:** Gentoo Linux with OpenRC 0.63 or newer.
- **Packages:**
  - `app-containers/docker`, version 29 or newer;
  - `sys-apps/shadow`, for `newuidmap`;
  - `sys-apps/bubblewrap`;
  - `app-containers/slirp4netns`;
  - `dev-vcs/git`;
  - `net-misc/curl`;
  - Python 3.12 or newer.
- **Hardware:** about 8 GB of free RAM for the image builds, and 20 GB of disk per instance.

`deploy/host-prep.sh` lists anything missing. With `--install-packages` it installs them through
`emerge --noreplace`.

## 1. Prepare the host (root, once per instance)

```bash
git clone <this repository> /tmp/omc && cd /tmp/omc
deploy/host-prep.sh --install-packages demo      # instance "demo" -> user omc-demo
```

Host prep does the following, and nothing else on the host:

1. It creates the user `omc-demo`. The user is not in `wheel` and not in the `docker` group.
2. It assigns a subordinate uid and gid range.
3. It installs Docker's static rootless extras (`rootlesskit`) for the host's Docker version under
   `/opt/omc/rootless-extras/`.
4. It starts the user's own rootless Docker daemon as an OpenRC user service (`user.omc-demo`).
5. It enables that service for future boots. No reboot is needed.

## 2. Install (as the instance user)

```bash
su - omc-demo
git clone <this repository> ~/omc && cd ~/omc
deploy/install.sh
```

Install does six things:

1. It creates `data/` (mode 0700) with freshly generated secrets in `data/.env` (mode 0600).
2. It picks free ports and records them in `data/.env`.
3. It builds OmniRoute from the pinned upstream release plus `omniroute/patches/`.
4. It starts the stack with Docker Compose.
5. It prepares the fleet services and cron jobs.
6. It waits for the backend and frontend to be healthy.

Re-running install never regenerates `data/.env`.

## 3. Guided setup

```bash
deploy/setup.sh            # resumable; deploy/setup.sh --step 3 runs one step
```

| Step | What you do | What setup checks |
|---|---|---|
| 1 | — | all containers run, backend healthy |
| 2 | Log into the UI at `http://localhost:<FRONTEND_PORT>` with `LOCAL_AUTH_TOKEN` from `data/.env` | the token works |
| 3 | Connect your LLM in OmniRoute (below) | OmniRoute lists the `omniroute-auto` combo |
| 4 | — | the backend gets a completion through OmniRoute |
| 5 | Register the required OpenClaw gateway; choose whether to install/configure optional coding harnesses ([harness setup](../harness/HARNESS-SETUP.md)) | a gateway is registered; optional harnesses may be skipped |
| 6 | Create your first board in the UI | a board exists |
| 7 | — | enables the coding fleet only with a configured optional harness; otherwise disarms it while OpenClaw/PEV remains usable |
| 8 | — | a smoke task on your board passes through OmniRoute |

### Interactive component setup

OpenClaw is **core**: every board needs its gateway. Pi and Claude Code are **optional** coding
harnesses. Missing Pi does not prevent base setup or the built-in PEV smoke task from completing.

As the instance user, run `deploy/setup.sh --components` at any time, including after step 5 was
previously completed. It offers use-existing, install, configure, skip (optional components), and
return-later choices. No external installer runs without your explicit install choice. Installers
run as the instance user, never as root. Failed installs can be retried without resetting setup.

For an attachable terminal (requires tmux), as `omc-demo`:

```bash
cd ~/omc
tmux new-session -s omc-setup 'cd "$HOME/omc"; deploy/setup.sh --components; exec bash -l'
```

Detach with **Ctrl-b**, then **d**; reattach as that same user with `tmux attach -t omc-setup`.
From a root terminal, use `su - omc-demo -c 'tmux attach -t omc-setup'`. Do not reuse or delete
another existing session. The setup terminal is not a runtime service manager.

Choosing Pi offers `curl -fsSL https://pi.dev/install.sh | sh`, then helps configure that user's
`~/.pi/agent/models.json` and `settings.json` using this instance's OmniRoute and available models.
Enter the actual context/output limits from your provider or model configuration; these fields
are required and have no guessed defaults. Existing custom providers/settings are
preserved; replacing a changed file requires confirmation and creates a mode-0600 backup. Secret
input is hidden and credentials are not passed in process arguments. A tool-free, in-memory Pi
request checks the actual CLI and model configuration. Optional plugins are not auto-installed.

Choosing skip leaves base OpenClaw/PEV operation available. Re-run `deploy/setup.sh --step 7`
after changing component choices: it rechecks availability even when previously marked done,
and starts or disarms the optional coding services accordingly. Skipped harnesses are not
reported as functioning. Do not create coding-fleet work until a coding harness is configured.

### Connecting your LLM (step 3)

OMC talks to models only through the instance's own OmniRoute.

1. Open `http://localhost:<OMNIROUTE_PORT>/dashboard` and log in with `INITIAL_PASSWORD` from
   `data/.env.omniroute`.
2. Go to **Providers → Add OpenAI Compatible**. Enter a name, a prefix such as `local`, and your
   server's base URL, for example `http://192.0.2.10:8000/v1`.
3. Open the new provider. Under **Connections → Add**, save a connection; give an API key only if
   your server needs one. Then click **Import from /models**. Setup can also list the server's
   models with their context length if you paste the URL.
4. Go to **Combos → Create Combo** and name it **`omniroute-auto`**. Add your provider and model as
   its step, then go through to **Create Combo**. Mission Control's planner, executor and verifier
   tiers fall back to it.
5. Go to **API Keys → Create API Key** and paste the key when setup asks. Setup stores it as
   `OMNIROUTE_RUNTIME_KEY` in `data/.env` and restarts the backend.

Add more providers and models the same way at any time.

## Removing an instance

```bash
deploy/purge.sh demo        # as root
```

Purge stops and removes everything that belongs to the instance:

- its services, containers, volumes and images;
- its crontab and subuid lines;
- the user and its home directory.

It then checks that nothing is left behind: no process, port, file or entry. It refuses any user
that `host-prep.sh` did not create.

Shared host prerequisites stay, because every instance uses them:

- `/opt/omc/rootless-extras/`;
- `/etc/modules-load.d/omc-tun.conf`;
- the installed packages.

Remove them by hand once no instance is left.

See also [configuration](configuration.md), [operations](operations.md) and [architecture](architecture/README.md).
