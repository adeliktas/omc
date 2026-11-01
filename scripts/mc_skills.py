"""Skills every fleet agent gets, whatever its harness (owner 2026-10-26 and 2026-10-28).

- ponytail at the level ~/.config/ponytail/config.json sets: ponytail's own ruleset, prepended to every
  prompt. Isolated harnesses load no hooks, extensions or skills, so the prompt is the one place that
  reaches Claude, Pi, the canonical codex profile, the private-board worker and the acceptance reviewer alike.
- a superpowers subset per role: an index (name, description, file) in the prompt, with the skill files
  mounted read-only at SKILLS_MOUNT in every sandbox, so the model reads a skill when it applies. That is
  Pi's own skill mechanism, done once for every harness, at ~1 KB per prompt instead of 3-20 KB per skill.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # omc_paths when loaded by path
from omc_paths import HOME  # noqa: E402

PONYTAIL_HOOKS = os.environ.get("RUNNER_PONYTAIL_HOOKS", str(HOME / ".claude/ponytail/hooks"))
SUPERPOWERS = Path(os.environ.get("RUNNER_SUPERPOWERS", str(HOME / ".pi/agent/git/github.com/obra/superpowers/skills")))
SKILLS_MOUNT = "/opt/mc-skills/superpowers"  # outside ~/.pi, which the private-board worker masks
# Unattended roles only get skills that fit the coordinator. Left out: brainstorming and using-superpowers
# (a human partner to question; a skill check before every reply), writing/executing plans and subagents
# (the PM's JSON plan and one worker per cycle own that), git worktrees and branch finishing (workers have
# no git; the host integrates and publishes), requesting reviews (QA is a separate stage), meta skills.
SUPERPOWERS_BY_ROLE = {
    "implement": ("test-driven-development", "systematic-debugging", "verification-before-completion",
                  "receiving-code-review"),
    "verify": ("systematic-debugging", "verification-before-completion"),
    "qa": ("systematic-debugging", "verification-before-completion"),
}


def ponytail() -> tuple[str, str]:
    """(level, ruleset) exactly as ponytail's own session hooks inject them; ("off", "") without ponytail."""
    js = ("const c=require(process.argv[1]+'/ponytail-config.js'),i=require(process.argv[1]+"
          "'/ponytail-instructions.js'),m=c.getDefaultMode();"
          "process.stdout.write(JSON.stringify([m,m==='off'?'':i.getPonytailInstructions(m)]))")
    try:
        node = shutil.which("node") or str(HOME / ".local/share/pi-node/current/bin/node")
        out = subprocess.run([node, "-e", js, PONYTAIL_HOOKS], capture_output=True, text=True, timeout=20)
        level, rules = json.loads(out.stdout)
        return (level, rules) if isinstance(rules, str) else ("off", "")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "off", ""


def superpowers(role_kind: str | None, capability: str | None) -> list[str]:
    """The installed superpowers skills for one attempt's role (PM and backlog PM get none)."""
    if role_kind == "qa":
        key = "qa"
    elif role_kind in ("developer", "specialist"):
        key = "implement" if capability == "write_worktree" else "verify"
    else:
        return []
    return [name for name in SUPERPOWERS_BY_ROLE[key] if (SUPERPOWERS / name / "SKILL.md").is_file()]


def superpowers_index(names: list[str]) -> str:
    lines = []
    for name in names:
        found = re.search(r'^description:\s*"?(.*?)"?\s*$', (SUPERPOWERS / name / "SKILL.md").read_text(errors="replace"), re.M)
        lines.append(f"- {name}: {(found[1] if found else '')[:300].rstrip('.')}. Read {SKILLS_MOUNT}/{name}/SKILL.md")
    if not lines:
        return ""
    return ("SKILLS (superpowers). When one applies, read its file first and follow it. You run unattended: there is "
            "no human partner to ask, so where a skill says to ask, decide, note the decision in your report and "
            "continue. Ponytail wins where they overlap. Do not create plan files or start subagents.\n" + "\n".join(lines))
