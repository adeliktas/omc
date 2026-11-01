"""A healthy deploy never prunes the image it replaced, however old (QA 2026-10-06)."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_replaced_image_survives_pruning_even_when_older(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = (ROOT / "scripts/mc-deploy.sh").read_text().replace("CHECK_INTERVAL=3", "CHECK_INTERVAL=0")
    (tmp_path / "scripts/mc-deploy.sh").write_text(script)
    (tmp_path / ".env").write_text("X=1\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    created = {"old1": "2026-10-01", "old4": "2026-10-04", "old5": "2026-10-05", "old6": "2026-10-06"}
    lines = "\n".join(f'sha256:{k}) echo "{v}T00:00:00Z sha256:{k}";;' for k, v in created.items())
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
echo "$*" >> {log}
case "$*" in
  *"compose"*" ps -q"*) echo cid1 ;;
  "inspect -f {{{{.Image}}}} cid1") echo sha256:old1 ;;
  "inspect -f {{{{.Config.Image}}}} cid1") echo mc-backend:latest ;;
  "images -q --no-trunc -f dangling=true"*) printf 'sha256:old1\\nsha256:old4\\nsha256:old5\\nsha256:old6\\n' ;;
  "inspect -f {{{{.Created}}}} {{{{.Id}}}}"*)
    for id in "${{@:4}}"; do case "$id" in {lines} esac; done ;;
esac
exit 0
""")
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "free").write_text("#!/bin/sh\necho 'Mem: 1 1 1 1 1 99999'\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", MC_ENV_FILE=str(tmp_path / ".env"))
    subprocess.run(["bash", str(tmp_path / "scripts/mc-deploy.sh"), "backend"], env=env,
                   check=True, capture_output=True, text=True, timeout=60)
    removed = [line for line in log.read_text().splitlines() if line.startswith("rmi ")]
    # Two kept: old1 (the rollback target, though oldest) and the newest other, old6.
    assert removed == ["rmi sha256:old5 sha256:old4"]
    assert shutil.which("bash")
