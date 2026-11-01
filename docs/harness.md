# Agent harnesses

OpenClaw is the core gateway harness required for boards. Pi and Claude Code are optional coding
harnesses. `deploy/setup.sh --components` offers interactive install/configure/skip choices as the
instance user; an installer only runs after an explicit choice. Skipping optional harnesses leaves
OpenClaw and the built-in PEV lane available, with the optional coding fleet disarmed. [harness/HARNESS-SETUP.md](../harness/HARNESS-SETUP.md) gives the install
command, version, templates and check for each. You can follow it yourself or hand it to your own
coding agent.

## How the fleet uses them

- **Runs:** the team runner starts a harness per attempt (PM, developer, specialist, QA, reviewer)
  inside a `bwrap` sandbox. Each attempt gets a scoped worktree and no access to credentials.
- **Models:** they are reached through the instance's OmniRoute (`models.json` in Pi,
  `openclaw.json` in OpenClaw).
- **Private boards:** they use only the configured local model, through OmniRoute's private lane.

## Skills

- **ponytail:** every harness prompt starts with the ponytail rules (keep code minimal), at the
  level set in `~/.config/ponytail/config.json` (`ultra` by default). The watchdog reports any run
  without them.
- **superpowers:** coding roles get an index of the `superpowers` skills that fit them, mounted
  read-only at `/opt/mc-skills/superpowers`, and open a skill's file only when it applies.

  | Role | Skills |
  |---|---|
  | developers and specialists | test-driven-development, systematic-debugging, verification-before-completion, receiving-code-review |
  | verify and QA | systematic-debugging, verification-before-completion |
  | PM | none |
- **Where to see them:** the `/models` page shows engine and role settings; `/skills/installed`
  lists every installed skill and lets an admin view or edit its files.
