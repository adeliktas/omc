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
| 5 | Install the OpenClaw gateway and register it under Gateways ([harness setup](../harness/HARNESS-SETUP.md)); boards need it | a gateway is registered |
| 6 | Create your first board in the UI | a board exists |
| 7 | — | starts the team pool, review agent and cron jobs |
| 8 | — | a smoke task on your board passes through OmniRoute |

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
