"""Canonical Claude can atomically maintain auth; unrelated existing files stay RO."""
import importlib.util
from pathlib import Path
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[2]


def test_canonical_auth_directory_maintenance_does_not_unlock_other_files(tmp_path):
    spec=importlib.util.spec_from_file_location('canonical_runner',ROOT/'scripts/mc-team-runner.py')
    r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    canonical=Path('/root/.claude')
    nonce=uuid.uuid4().hex
    other=canonical/f'.mc-ro-test-{nonce}'
    fresh=canonical/f'.mc-auth-tmp-test-{nonce}'
    other.write_text('unchanged')
    cli=tmp_path/'claude'
    cli.write_text(f'''#!/usr/bin/env python3
from pathlib import Path
p=Path({str(other)!r})
try: p.write_text('must-not-write')
except OSError: pass
else: raise AssertionError('existing canonical file became writable')
Path({str(fresh)!r}).write_text('temporary auth maintenance')
''');cli.chmod(0o755)
    cwd=tmp_path/'snapshot';cwd.mkdir()
    scratch=tmp_path/'scratch';scratch.mkdir()
    try:
        subprocess.run(r.sandbox_command([str(cli)],cwd,scratch,writable=False),check=True)
        assert other.read_text()=='unchanged'
        assert fresh.read_text()=='temporary auth maintenance'
    finally:
        other.unlink(missing_ok=True);fresh.unlink(missing_ok=True)
