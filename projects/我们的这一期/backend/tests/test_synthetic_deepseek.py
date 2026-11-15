"""Offline tests for the consent-scoped real API runner; never uses real keys."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from app.models import Budget, Episode, Job, ModelCall
from app.repository import Repository, DomainError
from app.services.director import DirectorError
from app.services.pipeline import ModelServices
from test_new_providers import envelope, NOW

SPEC = importlib.util.spec_from_file_location('synthetic_probe', Path(__file__).resolve().parents[2] / 'scripts/synthetic_deepseek.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
KEY = 'offline-test-value-only'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(probe.ds.time, 'time', lambda: NOW)
    def reject(*args, **kwargs):
        raise AssertionError('network prohibited in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket, 'create_connection', reject)


@pytest.fixture
def mock_transport(monkeypatch):
    state = SimpleNamespace(calls=[], missing_usage=False, fail_director=False, crash=False)
    original = httpx.AsyncClient
    def handler(request):
        state.calls.append(request)
        body = json.loads(request.content)
        kind = 'vision' if isinstance(body['messages'][0]['content'], list) else 'directions'
        assert body['messages'] == probe.allowed_messages()[kind]
        assert request.url == 'https://api.deepseek.com/chat/completions'
        if state.crash:
            raise KeyboardInterrupt('simulated crash')
        if kind == 'vision':
            value = {'observations':[{'asset_id':probe.ASSET,'time':.5,'frame_id':probe.ASSET+'_f001',
                                     'note':'左侧红色圆形，右侧蓝色正方形','verification':'unverified'}],'uncertainties':[]}
        else:
            value = {'directions': [{'id':'d1','title':'圆与方','summary':'虚构技术图形测试',
                'evidence': [{'asset_id':probe.ASSET,'start':0.,'end':1.,'note':'图形同框'}],
                'edit_plan': {'segments':[{'asset_id':probe.ASSET,'start':0.,'end':1.,'caption':'圆与方'}],
                              'narration_text':''}}], 'uncertainties':[]}
            if state.fail_director:
                value = {'bad':'incomplete schema'}
        response = envelope(value)
        if state.missing_usage:
            response.pop('usage')
        return httpx.Response(200, json=response)
    monkeypatch.setattr(probe.ds.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return state


def test_no_consent_no_key_and_arbitrary_messages_rejected_before_client(tmp_path, mock_transport):
    repo = Repository(tmp_path)
    with pytest.raises(DomainError): probe.run_probe(repo, KEY)
    with pytest.raises(DomainError): probe.run_probe(repo, '', authorized=True)
    service = probe.SyntheticDirector(KEY, kind='directions', authorized=True, budget_check=lambda _:True)
    with pytest.raises(DirectorError, match='固定'):
        asyncio.run(service.request([{'role':'user','content':'private content'}], 'r'))
    assert mock_transport.calls == []
    with repo.factory() as db:
        assert not list(db.scalars(select(Budget)))
        assert not list(db.scalars(select(Episode)))
    assert not ModelServices({'DEEPSEEK_API_KEY':KEY,'SYNTHETIC_APPROVED':True}).ready()[0]


def test_exact_two_calls_shared_ledger_checkpoint_restart_no_resend(tmp_path, mock_transport):
    repo = Repository(tmp_path)
    value = probe.run_probe(repo, KEY, authorized=True)
    assert value['state'] == 'succeeded' and len(mock_transport.calls) == 2
    assert value['budget']['total_spent'] > 0 and value['budget']['reserved'] == 0
    assert probe.run_probe(Repository(tmp_path),KEY,authorized=True)['state']=='succeeded'
    assert len(mock_transport.calls)==2
    with repo.factory() as db:
        job=db.get(Job,value['job_id'])
        assert job.payload['model_profile']['retention_verified'] is False
        assert KEY not in json.dumps(job.payload)
        assert list(job.payload['steps'])==['vision:'+probe.fixtures()[0].request_digest,'directions:default']
    assert not (tmp_path/'docs/evidence/阶段3-2/冒烟记录.md').exists()


def test_unknown_usage_stops_director_and_repeat(tmp_path, mock_transport):
    mock_transport.missing_usage=True
    repo=Repository(tmp_path)
    with pytest.raises(DomainError):probe.run_probe(repo,KEY,authorized=True)
    with pytest.raises(DomainError,match='费用'):probe.run_probe(Repository(tmp_path),KEY,authorized=True,retry_failed=True)
    assert len(mock_transport.calls)==1
    with repo.factory() as db:
        row=db.scalars(select(Budget)).one()
        assert row.state=='unknown' and row.amount==100000


def test_known_failure_requires_explicit_retry_and_reuses_vision(tmp_path,mock_transport):
    mock_transport.fail_director=True
    repo=Repository(tmp_path)
    with pytest.raises(DomainError):probe.run_probe(repo,KEY,authorized=True)
    assert len(mock_transport.calls)==2
    with pytest.raises(DomainError,match='显式重试'):probe.run_probe(repo,KEY,authorized=True)
    assert len(mock_transport.calls)==2
    mock_transport.fail_director=False
    assert probe.run_probe(repo,KEY,authorized=True,retry_failed=True)['state']=='succeeded'
    assert len(mock_transport.calls)==3


def test_crash_keeps_reservation_and_does_not_resend(tmp_path,mock_transport):
    mock_transport.crash=True
    repo=Repository(tmp_path)
    with pytest.raises(KeyboardInterrupt):probe.run_probe(repo,KEY,authorized=True)
    with pytest.raises(DomainError,match='费用'):probe.run_probe(Repository(tmp_path),KEY,authorized=True,retry_failed=True)
    assert len(mock_transport.calls)==1


@pytest.mark.parametrize('global_cost,task_cost',[(29900000,0),(0,2900000)])
def test_global_or_task_budget_blocks_before_overrun(tmp_path,mock_transport,global_cost,task_cost):
    repo=Repository(tmp_path)
    with repo.transaction() as db:
        db.add(Budget(owner_id='other',episode_id='other',amount=global_cost,cost=global_cost,state='settled'))
        db.add(Budget(owner_id=probe.OWNER,episode_id=probe.EPISODE_ID,amount=task_cost,cost=task_cost,state='settled'))
    with pytest.raises(DomainError,match='预算'):probe.run_probe(repo,KEY,authorized=True)
    assert len(mock_transport.calls)==1  # First bounded step fits, second preflight refuses.


def test_fixed_preview_cannot_become_uploaded_input(tmp_path,mock_transport):
    out=probe.prepare(tmp_path)
    (out/'图案.jpg').write_bytes(b'private replacement')
    assert probe.run_probe(Repository(tmp_path),KEY,authorized=True)['state']=='succeeded'
    assert b'private replacement' not in b''.join(r.content for r in mock_transport.calls)
    with pytest.raises(DomainError,match='预览'):probe.prepare(tmp_path)


def test_changed_prompt_profile_blocks_paid_reuse(tmp_path,mock_transport,monkeypatch):
    repo=Repository(tmp_path)
    probe.run_probe(repo,KEY,authorized=True)
    previous=probe.allowed_messages
    def changed():
        value=previous();value['directions'][1]['content']+='changed';return value
    monkeypatch.setattr(probe,'allowed_messages',changed)
    with pytest.raises(DomainError,match='已变化'):probe.run_probe(repo,KEY,authorized=True)
    assert len(mock_transport.calls)==2


def test_concurrent_probe_lock_refuses(tmp_path):
    with probe.probe_lock(tmp_path):
        with pytest.raises(DomainError,match='运行中'):
            with probe.probe_lock(tmp_path):pass


def test_secure_key_writer_preserves_flags_and_never_overwrites(tmp_path,monkeypatch,capsys):
    file=tmp_path/'.env';file.write_text('APP_INVITE_CODE=local-test\nCLOUD_ENABLED=false\nDEEPSEEK_RETENTION_VERIFIED=false\n')
    monkeypatch.setattr(probe.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    probe.save_key(file,KEY)
    assert 'CLOUD_ENABLED=false' in file.read_text() and 'DEEPSEEK_RETENTION_VERIFIED=false' in file.read_text()
    assert file.stat().st_mode & 0o777==0o600
    with pytest.raises(DomainError,match='不覆盖'):probe.save_key(file,KEY)
    assert KEY not in capsys.readouterr().out


def test_key_writer_refuses_unignored_env_and_nonterminal(tmp_path,monkeypatch):
    file=tmp_path/'.env';file.write_text('CLOUD_ENABLED=false\n')
    monkeypatch.setattr(probe.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(DomainError,match='Git忽略'):probe.save_key(file,KEY)
    assert file.read_text()=='CLOUD_ENABLED=false\n'
    monkeypatch.setattr(probe.sys,'stdin',SimpleNamespace(isatty=lambda:False))
    monkeypatch.setattr(probe.getpass,'getpass',lambda *a:pytest.fail('secret prompt'))
    assert probe.main(['configure-key'])==2


def test_explicit_revision_keeps_old_results_same_budget_and_reuses_revision(tmp_path,mock_transport,monkeypatch):
    repo=Repository(tmp_path)
    first=probe.run_probe(repo,KEY,authorized=True)
    original=probe.fixtures
    def revised_fixture():
        prepared,evidence,assets,background=original()
        return prepared,evidence,assets,background+' 固定样例修订测试。'
    monkeypatch.setattr(probe,'fixtures',revised_fixture)
    with pytest.raises(DomainError,match='已变化'):probe.run_probe(repo,KEY,authorized=True)
    second=probe.run_probe(repo,KEY,authorized=True,new_revision=True)
    assert second['state']=='succeeded' and second['job_id']!=first['job_id']
    assert second['budget']['spent']==pytest.approx(first['budget']['spent']*2)
    assert probe.run_probe(repo,KEY,authorized=True,new_revision=True)['job_id']==second['job_id']
    assert len(mock_transport.calls)==4
    with repo.factory() as db:
        assert len(list(db.scalars(select(Episode))))==1
        assert db.get(Job,first['job_id']).state=='succeeded'
        assert len(list(db.scalars(select(Budget))))==4
