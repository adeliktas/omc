# Security

## Reporting

Report vulnerabilities privately to the repository owner, through the hosting platform's private
security advisory feature. Do not use public issues.

## Model

- **Secrets:** every secret of an instance lives in its git-ignored `data/` directory (mode 0700,
  files 0600). It is generated at install and never committed. The repository holds no keys,
  tokens, passwords or instance data.
- **Least privilege:** each instance runs as its own unprivileged user, with no `wheel` and no
  `docker` group, on its own rootless Docker daemon. Root is used once, by `deploy/host-prep.sh`.
- **Agent sandboxes:** agents run in `bwrap` sandboxes. Credential directories, `.env` files and
  any configured `hidden_paths` are masked from every worker.
- **Private boards:** they reach only the configured local model, through OmniRoute's
  self-authenticating private lane, which refuses every request while unconfigured.
- **Network exposure:** Compose publishes the UI, API and OmniRoute on all interfaces. Use
  `compose.hardened.yml` to bind them to loopback and reach them through an SSH tunnel.
