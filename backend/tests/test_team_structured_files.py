"""Original worker file bytes survive cleanup as fields, never parsed from prose."""
# ruff: noqa: F811 - imported pytest fixture
import importlib.machinery
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING
from tests.test_team_verification import VERIFY_PLAN, _planned, claim, env  # noqa: F401

runner = importlib.machinery.SourceFileLoader('structured_file_runner', str(Path(__file__).resolve().parents[2] / 'scripts/mc-team-runner.py')).load_module()


def test_public_operator_placeholder_passes_without_exempting_real_credentials():
    from app.core.evidence_redaction import redact_evidence
    source=(Path(__file__).resolve().parents[2]/'docs/operations/autonomous-review.md').read_text()
    attached=set()
    appendix=svc.evidence_appendix({'public-policy.md':source},attached=attached)
    assert attached=={'public-policy.md'} and source in appendix
    assert redact_evidence('MC_TOKEN=<operator token>\n')=='MC_TOKEN=<operator token>\n'
    for value in ('actual-private-value','<operator token>suffix'):
        assert redact_evidence('MC_TOKEN='+value)!='MC_TOKEN='+value
    assert redact_evidence('MC_TOKEN=<operator token>\nAPI_KEY=actual-private-value')!='MC_TOKEN=<operator token>\nAPI_KEY=actual-private-value'


async def author(env):
    s, board, task = env
    cycle = await _planned(s, board, task, VERIFY_PLAN)
    worker, token = await claim(s, cycle)
    return s, board, task, cycle, worker, token


async def test_file_fields_are_retained_through_complete_and_actual_claim_endpoint(env, monkeypatch, tmp_path):
    from app.api import team as api
    from app.schemas.team import TeamClaimRequest, TeamClaimResponse
    s, board, task, cycle, worker, token = await author(env)
    original = 'Checklist\r\nLiteral [redacted] is a policy example.\nTrailing blank:\n\n'
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact='Actual complete report at /tmp/reports/checklist.md', head_sha=BASE,
        evidence_files={'reports/checklist.md':original})
    await s.commit()
    assert json.loads(worker.assignment)['evidence_files']=={'reports/checklist.md':original}
    await svc.integrate(s, cycle, BASE, ranking=RANKING)
    async def ranking(_):return RANKING
    monkeypatch.setattr(api,'_ranking',ranking)
    response=await api.claim(cycle.id,TeamClaimRequest(base_sha=BASE),board,task,s)
    parsed=TeamClaimResponse.model_validate(response)
    assert parsed.evidence_files=={'reports/checklist.md':original}
    assert 'restored from retained structured file bytes' in parsed.brief
    public=runner.evidence_dir(tmp_path/'qa');public.mkdir()
    runner.stage_evidence_files(public,parsed.evidence_files)
    assert (public/'reports/checklist.md').read_bytes()==original.encode('utf-8')
    # The disposable worker file can be gone; no text/Markdown parsing occurred.
    assert json.loads(worker.assignment)['evidence_files']['reports/checklist.md']==original


async def test_redacted_probe_is_not_attached_and_the_cited_leaf_still_completes(env):
    s, board, task, cycle, worker, token = await author(env)
    probe = 'token = "IDENTIFIER"\\nprint(token)\\n'
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact='Reproduce with /tmp/probe.py', head_sha=BASE,
        evidence_files={'probe.py': probe, 'clean.md': 'safe exact bytes\\n'})
    policy = json.loads(worker.assignment)
    assert policy['artifact_complete'] is True
    assert policy['evidence_files'] == {'clean.md': 'safe exact bytes\\n'}
    assert 'IDENTIFIER' not in worker.artifact
    assert worker.status == 'succeeded'


async def test_legacy_prose_is_not_reconstructed_and_rejected_files_not_stored(env):
    s,board,task,cycle,worker,token=await author(env)
    await svc.complete_attempt(s,board,cycle,worker,token,ok=True,
        artifact='Ran a real check; source unchanged',head_sha=BASE,
        evidence_files={'ok.md':'Whole genuine report\n','../escape.md':'not allowed'})
    files=json.loads(worker.assignment)['evidence_files']
    assert files=={'ok.md':'Whole genuine report\n'}
    qa=await svc._qa_member(s,cycle,[worker],RANKING)
    attempt=await svc._new_attempt(s,cycle,qa,'qa',ranking=RANKING)
    assert svc.qa_evidence_files(cycle,[worker],attempt)==files
    policy=json.loads(worker.assignment);policy.pop('artifact_complete');worker.assignment=json.dumps(policy)
    with pytest.raises(svc.TeamError,match='completeness unknown'):svc.qa_evidence_files(cycle,[worker],attempt)
    policy['artifact_complete']=True
    policy.pop('evidence_files');worker.assignment=json.dumps(policy)
    assert svc.qa_evidence_files(cycle,[worker],attempt)=={}  # don't parse the intact appendix


async def test_qa_only_owner_files_are_retained_and_conflicts_refused(env):
    from tests.test_team_report_qualified_qa import report_ranking
    ranking=report_ranking()
    s,board,task,cycle,worker,token=await author(env)
    await svc.complete_attempt(s,board,cycle,worker,token,ok=True,
        artifact='Original /tmp/check.md',head_sha=BASE,evidence_files={'check.md':'actual original\n'})
    await svc.integrate(s,cycle,BASE,ranking=ranking)
    qa,token=await claim(s,cycle)
    await svc.complete_attempt(s,board,cycle,qa,token,ok=True,head_sha=BASE,
        verdict='fail',artifact='CORRECTION_KIND: EVIDENCE\nNeed genuine supplemental proof\nVERDICT: FAIL')
    await svc.retry_qa(s,cycle,cycle.version,ranking=ranking,evidence_files={'new-proof.md':'Authentic owner proof\n'})
    pending=[a for a in await svc._attempts(s,cycle.id) if a.role_kind=='qa' and a.status=='queued'][0]
    assert svc.qa_evidence_files(cycle,[worker],pending)=={'check.md':'actual original\n','new-proof.md':'Authentic owner proof\n'}
    qa,token=await claim(s,cycle)
    await svc.complete_attempt(s,board,cycle,qa,token,ok=True,head_sha=BASE,
        verdict='pass',artifact='Fresh complete owner proof independently checked\nVERDICT: PASS')
    deadline=cycle.deadline_at
    await svc.refresh_integration(s,cycle,'e'*40,'e'*40,cycle.version,ranking=ranking)
    pending=[a for a in await svc._attempts(s,cycle.id) if a.role_kind=='qa' and a.status=='queued'][0]
    assert cycle.deadline_at==deadline and cycle.reviewed_sha is None
    assert svc.qa_evidence_files(cycle,[worker],pending)=={'check.md':'actual original\n','new-proof.md':'Authentic owner proof\n'}
    policy=json.loads(pending.assignment);policy['evidence_files']={'check.md':'conflicting replacement'};pending.assignment=json.dumps(policy)
    assert svc.qa_evidence_files(cycle,[worker],pending)=={'check.md':'actual original\n','qa/check.md':'conflicting replacement'}


async def test_acceptance_snapshot_and_explicit_owner_qa_retain_whole_files(env):
    from app.api import review
    from tests.test_team_report_qualified_qa import report_ranking
    ranking = report_ranking()
    s, board, task, cycle, worker, token = await author(env)
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact='Whole original worker report', head_sha=BASE)
    await svc.integrate(s, cycle, BASE, ranking=ranking)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True, head_sha=BASE,
        verdict='fail', artifact='CORRECTION_KIND: EVIDENCE\nNeed whole report\nVERDICT: FAIL')
    files = {'owner-report.md': 'Actual revised report\r\nComplete bytes\n\n'}
    await svc.retry_qa(s, cycle, cycle.version, ranking=ranking, evidence_files=files)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True, head_sha=BASE,
        verdict='pass', artifact='Fresh independently checked whole report\nVERDICT: PASS')
    cycle.state, cycle.published_sha, task.status = 'published', BASE, 'review'
    await s.flush()
    snapshot = await review._task_snapshot(s, task, board)
    assert snapshot['evidence_files'] == files
    assert snapshot['evidence_files_problem'] is None
    await svc.retry_qa(s, cycle, cycle.version, ranking=ranking)
    pending = [a for a in await svc._attempts(s, cycle.id) if a.status == 'queued'][0]
    assert json.loads(pending.assignment)['evidence_files'] == files
    assert svc.qa_evidence_files(cycle, [worker], pending) == files


@pytest.mark.parametrize('files',[
    {'../escape.md':'bad'}, {'/tmp/escape.md':'bad'}, {'a/../escape.md':'bad'},
    {'mc-team-attempt.fake/out.raw':'bad'}, {'a/./x.md':'bad'}, {'empty.md':'\ud800'},
    {'huge.md':'x'*65537}, {str(i)+'.md':'x' for i in range(17)},
])
def test_stage_refuses_invalid_entire_payload_before_writing(tmp_path,files):
    with pytest.raises(ValueError):runner.stage_evidence_files(tmp_path,files)
    assert not list(tmp_path.rglob('*'))


def test_actual_isolated_qa_tmp_can_read_original_bytes(tmp_path):
    private=tmp_path/'qa-private';private.mkdir()
    public=runner.evidence_dir(private);public.mkdir()
    source=tmp_path/'source';source.mkdir()
    original='Whole original\r\n[redacted] literal policy\n\n'
    runner.stage_evidence_files(public,{'reports/proof.md':original})
    code="from pathlib import Path; print(Path('/tmp/reports/proof.md').read_bytes().hex())"
    argv=runner.sandbox_command([sys.executable,'-P','-c',code],source,private,writable=False)
    result=subprocess.run(argv,capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stderr
    assert bytes.fromhex(result.stdout.strip())==original.encode('utf-8')


def test_stage_refuses_collision_without_overwriting_original(tmp_path):
    (tmp_path/'proof.md').write_bytes(b'genuine\n')
    with pytest.raises(ValueError):runner.stage_evidence_files(tmp_path,{'proof.md':'replacement','second.md':'new'})
    assert (tmp_path/'proof.md').read_bytes()==b'genuine\n' and not (tmp_path/'second.md').exists()


async def test_corrected_owner_evidence_can_recover_a_qa_only_evidence_fail_without_replay(env):
    from tests.test_team_report_qualified_qa import report_ranking
    ranking = report_ranking()
    s, board, task, cycle, worker, token = await author(env)
    await svc.complete_attempt(s, board, cycle, worker, token, ok=True,
        artifact='Whole original report', head_sha=BASE)
    await svc.integrate(s, cycle, BASE, ranking=ranking)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True, head_sha=BASE,
        verdict='fail', artifact='CORRECTION_KIND: EVIDENCE\nMissing report\nVERDICT: FAIL')
    old = {'report.md': 'REPORT CHECK: 19 lines\n'}
    await svc.retry_qa(s, cycle, cycle.version, ranking=ranking, evidence_files=old)
    qa, token = await claim(s, cycle)
    await svc.complete_attempt(s, board, cycle, qa, token, ok=True, head_sha=BASE,
        verdict='fail', artifact='CORRECTION_KIND: EVIDENCE\nActual count is 20\nVERDICT: FAIL')
    assert cycle.block_reason == 'QA-only recovery failed; reconcile retained findings before further recovery'
    deadline, version = cycle.deadline_at, cycle.version
    for packet in (None, old):
        with pytest.raises(svc.TeamError, match='cause-specific'):
            await svc.retry_qa(s, cycle, version, ranking=ranking, evidence_files=packet)
        assert cycle.deadline_at == deadline and cycle.version == version
    corrected = {'report.md': 'REPORT CHECK: 20 lines\n'}
    original = qa.artifact
    qa.artifact = 'CORRECTION_KIND: CODE\nReal code defect\nVERDICT: FAIL'
    with pytest.raises(svc.TeamError, match='cause-specific'):
        await svc.retry_qa(s, cycle, version, ranking=ranking, evidence_files=corrected)
    qa.artifact = original
    await svc.retry_qa(s, cycle, version, ranking=ranking, evidence_files=corrected)
    attempts = await svc._attempts(s, cycle.id)
    assert sum(a.role_kind == 'qa' for a in attempts) == 3
    assert sum(a.role_kind in ('developer', 'specialist') for a in attempts) == 1
    assert worker.status == 'succeeded' and qa.artifact == original
    pending = next(a for a in attempts if a.status == 'queued')
    assert json.loads(pending.assignment)['evidence_files'] == corrected


def test_combined_leaf_evidence_over_budget_hands_off_what_fits(monkeypatch):
    # 2026-10-22: two leaves each within budget overflowed together and the QA
    # claim was refused forever. QA now gets what fits; the rest is listed by hash.
    from types import SimpleNamespace
    leaf = lambda i, name: SimpleNamespace(id=i, assignment=json.dumps(
        {'artifact_complete': True, 'evidence_files': {name: 'x' * 40_000}}))
    a, b = leaf('a1', 'one.log'), leaf('a2', 'two.log')
    qa = SimpleNamespace(id='q', assignment='{}')
    monkeypatch.setattr(svc, 'worker_evidence', lambda cycle, history: (json.dumps([{'attempt_id': 'a1'}, {'attempt_id': 'a2'}]), None))
    files = svc.qa_evidence_files(None, [a, b], qa)
    assert files == {'one.log': 'x' * 40_000}


def test_same_evidence_path_from_parallel_leaves_is_namespaced(monkeypatch):
    # 2026-10-22: two leaves each captured their own go-cache/trim.txt; the
    # QA claim was refused forever. The later copy is kept under its stage key.
    from types import SimpleNamespace
    leaf = lambda i, stage, text: SimpleNamespace(id=i, stage_key=stage, assignment=json.dumps(
        {'artifact_complete': True, 'evidence_files': {'go-cache/trim.txt': text}}))
    a, b = leaf('a1', 'dev-one', '1\n'), leaf('a2', 'dev-two', '2\n')
    qa = SimpleNamespace(id='q', stage_key='qa', assignment='{}')
    monkeypatch.setattr(svc, 'worker_evidence', lambda cycle, history: (json.dumps([{'attempt_id': 'a1'}, {'attempt_id': 'a2'}]), None))
    assert svc.qa_evidence_files(None, [a, b], qa) == {
        'go-cache/trim.txt': '1\n', 'dev-two/go-cache/trim.txt': '2\n'}
