# Default coding style

For coding tasks, run Ponytail in **ultra** mode by default.

Before planning or editing code, load and follow `~/.pi/agent/git/github.com/DietrichGebert/ponytail/skills/ponytail/SKILL.md` as if the user invoked `/skill:ponytail ultra`, unless the user explicitly says `stop ponytail`, `normal mode`, or asks for a different Ponytail intensity.

Keep Ponytail active for the whole session once loaded.

# Ponytail + Superpowers

Both are always on and work at different layers (ponytail#49): Superpowers owns the workflow (brainstorming, plans, systematic-debugging, TDD, review); Ponytail owns how the code comes out (smallest thing that works). Never pick one: run the Superpowers process, then let Ponytail shrink what it produces.

- Skill lookup happens before acting, but a skill is a checklist, not a licence to write more. Loading a skill never adds prose; "code first, at most three short lines" still applies.
- Brainstorming's one-question-at-a-time interview is for genuinely ambiguous design decisions. For an unambiguous "do X" request, ship the lazy version and question it in the same response instead of stalling.
- Ponytail pushing back on a plan ("do you need this, or does X cover it?") is the intended interaction, not a reason to skip the plan.
- User instructions outrank both skills (Superpowers' own rule). `stop ponytail`, `/ponytail <level>`, and explicit `/skill:<name>` are user instructions and win.

# Token accounting

Do not invent a remaining token or time budget from your reasoning. Context-window capacity, output limits, account quota, and an explicitly configured goal budget are different quantities. Only report measured usage or an explicit harness/tool-provided limit; otherwise say the remaining budget is unknown. Let Pi manage context compaction rather than shortening or abandoning work based on a guessed budget.

# Camoufox profile
When using the Camoufox MCP for browser navigation, pass the profile that holds your logged-in sessions
(for example `default`).
