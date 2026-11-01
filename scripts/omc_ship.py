#!/usr/bin/env python3
"""Which tracked files ship in the public OMC repo, per scripts/omc-export.paths.

Rules are fnmatch patterns on repo-relative paths ("*" crosses "/"); later rules win and a
leading "!" excludes. Only files git tracks can ship, so untracked junk never leaks.
"""
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path


def shipped_files(repo: Path) -> list[str]:
    rules = [ln.strip() for ln in (repo / "scripts/omc-export.paths").read_text().splitlines()]
    rules = [r for r in rules if r and not r.startswith("#")]
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True)
    out = []
    for path in tracked.stdout.decode().split("\0"):
        ship = False
        for rule in rules:
            if fnmatch(path, rule.lstrip("!")):
                ship = not rule.startswith("!")
        if path and ship:
            out.append(path)
    return sorted(out)


if __name__ == "__main__":
    print("\n".join(shipped_files(Path(sys.argv[1] if len(sys.argv) > 1 else "."))))
