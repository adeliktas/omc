# Contributing to OpenClaw Mission Control

Thanks for your interest in improving Mission Control.

This repo welcomes contributions in three broad categories:

- **Issues**: bug reports, feature requests, and design discussions
- **Documentation**: improvements to clarity, correctness, onboarding, and runbooks
- **Code**: fixes, features, tests, and refactors

## Where to start

- Source-cited architecture, results and full page index: Current state
- Docs landing page: Docs landing
- Development workflow: [Development](./docs/development/README.md)
- Testing guide: [Testing](./docs/testing/README.md)
- Release checklist: Release checklist

## Filing issues

When opening an issue, please include:

- What you expected vs what happened
- Steps to reproduce (commands, env vars, links)
- Logs and screenshots where helpful
- Your environment (OS, Docker version, Node/Python versions)

## Pull requests

### Branching hygiene (required)

Create feature branches from the latest `origin/master` to avoid unrelated commits in PRs:

```bash
git fetch origin
git worktree add -b <branch-name> ../mc-feature origin/master
cd ../mc-feature
```

Use an isolated worktree and explicit base; preserve unrelated edits and never reset, blanket-stage or stash a shared checkout. If the base is wrong, cherry-pick only the intended commits into a fresh worktree/PR. On the deployed host, publication is serialized and requires independent exact-SHA review; do not run installation or deployment scripts as documentation checks.

### Expectations

- Keep PRs **small and focused** when possible.
- Include a clear description of the change and why it’s needed.
- Add/adjust tests when behavior changes.
- Update docs when contributor-facing or operator-facing behavior changes.
- Changes that adapt third-party source or add a dependency must follow [Third-party source, licenses and dependencies](#third-party-source-licenses-and-dependencies).

### Local checks

From repo root, the closest “CI parity” command is:

```bash
make check
```

If you’re iterating on a specific area, the Makefile also provides targeted commands (lint, typecheck, unit tests, etc.). See `make help`.

## Docs contribution guidelines

- The numbered pages under `docs/` are **entrypoints**. Prefer linking to deeper pages instead of duplicating large blocks of content.
- Use concise language and concrete examples.
- When documenting operational behavior, call out risk areas (secrets, data loss, migrations).

## Third-party source, licenses and dependencies

- **Prefer independent implementations.** Use existing Mission Control helpers, the standard library, browser APIs or already-installed dependencies before copying anything.
- **Copying or adapting third-party code.** This includes tests and fixtures, and code from upstream projects studied in the research. Before copying, follow the exact-file review in License, provenance and dependency procedure. Attach a completed, reviewed provenance record. Uncertain terms are escalated before copying. A root MIT license never clears vendored, native, generated, font, asset, marketplace or cryptography content.
- **Prohibited imports.** Do not import `.repos/` reference trees, vendored or generated protocols, native code or binaries, fonts, icons or sounds, Ghostty/ripgrep/grammar components, marketplace packs, downloaded archives or cryptography implementations. The only exception is content that has passed the exact-file review **and** has separate owner authority (standing exclusions).
- **Attribution.** Place attribution as described in attribution placement. Do not create notice files or attribution records for code that was not actually adapted.
- **New dependencies.** Any newly proposed dependency must pass the dependency closure review before it is adopted. A missing closure or uncertain license terms blocks adoption.
- **Research backlog.** Research-to-backlog boundaries links the pinned research and lists every human-gated or rejected proposal with its reconsideration condition. Model initiative never unblocks those.

## Security and vulnerability reporting

If you believe you’ve found a security vulnerability:

- **Do not** open a public issue.
- Prefer GitHub’s private reporting flow:
  - https://github.com/abhi1693/openclaw-mission-control/security/advisories/new

If that’s not available in your environment, contact the maintainers privately.

## Code of conduct

If this repository adopts a Code of Conduct, we will link it here.

## License

By contributing, you agree that your contributions will be licensed under the MIT License. See [`LICENSE`](./LICENSE).
