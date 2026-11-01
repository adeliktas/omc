"""Capacity never overrides scheduling, quota or credential isolation."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys

from app.services import mc_sidecar, model_ranking

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_scheduled_jobs_and_disarm_are_respected(monkeypatch):
    pool = module('mc-team-pool')
    job = dict(execution_mode='team', execution_lane='harness', status='inbox')
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert not pool.eligible({**job, 'scheduled_at': future})
    assert not pool.eligible({**job, 'scheduled_at': 'invalid'})
    assert pool.eligible({**job, 'scheduled_at': '2020-01-01T00:00:00Z'})
    monkeypatch.setenv('RUNNER_APPLY', '0')
    assert pool.main() == 78


def test_old_success_cannot_clear_live_account_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(mc_sidecar, '_SIDECAR_PATH', tmp_path / 'metadata.json')
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    mc_sidecar.set_model_meta('provider', 'model', unavailable_until=future)
    model_ranking.record_usage('provider', 'model', {'success': True})
    assert mc_sidecar.get_model_meta('provider', 'model')['unavailable_until'] == future


def test_router_cooldown_cannot_shorten_existing_account_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(mc_sidecar, '_SIDECAR_PATH', tmp_path / 'metadata.json')
    now = datetime.now(timezone.utc)
    short = (now + timedelta(seconds=22)).isoformat()
    for until, reason in (((now + timedelta(days=6)).isoformat(), 'quota_exhausted'),
                          ('9999-01-01T00:00:00+00:00', 'credential_repair_required'),
                          ('invalid', 'credential_repair_required')):
        mc_sidecar.set_model_meta('omniroute-codex', 'held', unavailable_until=until,
                                  availability_reason=reason, disabled=True)
        model_ranking.record_usage('omniroute-codex', 'gpt-6.1-sol',
            {'success': False, 'total_tokens': 123, 'unavailable_until': short,
             'availability_reason': 'router_cooldown'})
        held = mc_sidecar.get_model_meta('omniroute-codex', 'held')
        assert held['unavailable_until'] == until and held['availability_reason'] == reason
        assert held['disabled'] is True
        assert not model_ranking._availability({}, 'omniroute-codex')[0]
    used = mc_sidecar.get_model_meta('omniroute-codex', 'gpt-6.1-sol')['observed_usage']
    assert used['calls'] == 3 and used['total_tokens'] == 369


def test_reported_router_cooldown_is_retained_when_no_stronger_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(mc_sidecar, '_SIDECAR_PATH', tmp_path / 'metadata.json')
    short = (datetime.now(timezone.utc) + timedelta(seconds=22)).isoformat()
    mc_sidecar.set_model_meta('omniroute-codex', 'gpt-6.1-sol', max_concurrency=3)
    mc_sidecar.set_model_meta('omniroute-codex', 'gpt-6-luna', write_enabled=False)
    model_ranking.record_usage('omniroute-codex', 'gpt-6.1-sol',
        {'success': False, 'total_tokens': 123, 'unavailable_until': short,
         'availability_reason': 'router_cooldown'})
    for model in ('gpt-6.1-sol', 'gpt-6-luna'):
        meta = mc_sidecar.get_model_meta('omniroute-codex', model)
        assert (meta['unavailable_until'], meta['availability_reason']) == (short, 'router_cooldown')
    assert mc_sidecar.get_model_meta('omniroute-codex', 'gpt-6-luna')['write_enabled'] is False


def test_other_credential_roots_are_hidden_in_actual_sandbox(tmp_path):
    runner = module('mc-team-runner')
    scratch = tmp_path / 'scratch'; scratch.mkdir()
    cwd = tmp_path / 'snapshot'; cwd.mkdir()
    code = """from pathlib import Path
assert not list(Path('/root/.local/state').iterdir())
assert not list(Path('/root/.pi/accounts').iterdir())
for name in ('models.json', 'auth.json'):
    try: assert Path('/root/.pi/agent', name).read_bytes() == b''
    except PermissionError: pass
"""
    subprocess.run(runner.sandbox_command([sys.executable, '-c', code], cwd, scratch, writable=False), check=True)
