"""Actual local media + production Pipeline, with explicitly fake HTTP models."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.main import create_app
from app.models import Asset, Budget, Episode, Job, ModelCall
from app.repository import Repository, DomainError
from app.services import deepseek as ds
from app.services.director import DirectorError
from app.services.speech import SpeechError, AudioChunk
from test_minimax_speech import wav_bytes
from test_minimax_asr import envelope as asr_envelope
from test_new_providers import envelope as ds_envelope, NOW

scripts = Path(__file__).resolve().parents[2]/'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('chain_probe', scripts/'synthetic_chain.py')
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
KEYS = {'DEEPSEEK_API_KEY': 'offline-test-only-key', 'MINIMAX_API_KEY': 'offline-test-only-key'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ds.time, 'time', lambda: NOW)
    def denied(*a, **k): pytest.fail('Real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = Repository(tmp_path/'data')
    source = repo.data_dir/probe.SOURCE; source.write_bytes(wav_bytes())
    monkeypatch.setattr(probe, 'SOURCE_SHA', hashlib.sha256(source.read_bytes()).hexdigest())
    folder = tmp_path/'expected'; folder.mkdir()
    movie = probe.build_fixture(source.read_bytes(), folder)
    monkeypatch.setattr(probe, 'FIXTURE_SHA', hashlib.sha256(movie.read_bytes()).hexdigest())
    probe.prepare(repo)
    yield repo
    repo.engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'unknown': False, 'asr_timeout': False}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(request)
        if request.url.host == 'api.deepseek.com':
            content = json.loads(request.content)['messages'][0]['content']
            if isinstance(content, list):
                frame = json.loads(content[1]['text'])
                output = {'observations': [{**frame, 'note': '左红圆右蓝方，同框静态图案', 'verification': 'unverified'}],
                          'uncertainties': []}
            else:
                evidence = json.loads(json.loads(request.content)['messages'][1]['content'])['verified_evidence']
                source = next(row for row in evidence if row['kind'] == 'speech')
                output = {'directions': [{'id': 'd1', 'title': '圆与方同框', 'summary': '固定几何样片',
                    'evidence': [{k: source[k] for k in ('asset_id', 'start', 'end', 'note')}],
                    'edit_plan': {'segments': [{**{k: source[k] for k in ('asset_id', 'start', 'end')},
                                               'caption': '同框', 'purpose': '保留固定样片完整原声',
                                               'evidence_ids': [source['id']]}],
                                  'narration_text': '圆和方，同框亮相。'}}], 'uncertainties': []}
            response = ds_envelope(output)
            if state['unknown']: response.pop('usage')
            return httpx.Response(200, json=response)
        assert request.url.host == 'api.minimax.cn'
        if state['asr_timeout']: raise httpx.ReadTimeout('private detail')
        return httpx.Response(200, json=asr_envelope())
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def test_actual_analysis_stops_before_human_confirmation(repo, transport):
    with repo.transaction() as db:
        db.add(Budget(owner_id='old', episode_id='old', amount=24500, state='unknown'))
    result = probe.analyze(repo, KEYS, authorized=True)
    assert len(transport['calls']) == 2
    assert result['analysis_state'] == 'review' and result['evidence_confirmed'] is False
    assert result['budget']['spent'] > 0 and result['budget']['total_reserved'] == .0245
    assert 'step=direction' in result['url']
    with repo.factory() as db:
        ep = db.get(Episode, probe.EPISODE_ID)
        assert {row['kind'] for row in ep.evidence} == {'visual', 'speech'}
        assert ep.selected_direction is None and not ep.directions
        assert ep.evidence_confirmed_revision is None
        job = db.scalars(select(Job).where(Job.kind == 'analysis')).one()
        assert job.payload['synthetic_evidence_digest'] == probe.digest(ep.evidence)
        assert job.payload['model_profile']['retention_verified'] is False
    again = Repository(repo.data_dir)
    assert probe.analyze(again, KEYS, authorized=True)['calls'] == result['calls']
    again.engine.dispose()
    assert len(transport['calls']) == 2
    assert not list(repo.data_dir.rglob('冒烟记录.md'))
    assert KEYS['MINIMAX_API_KEY'] not in json.dumps(result)


@pytest.mark.parametrize('mode', ['unknown', 'asr_timeout'])
def test_unknown_preserves_checkpoint_and_never_retries(repo, transport, mode):
    transport[mode] = True
    with pytest.raises(DomainError): probe.analyze(repo, KEYS, authorized=True)
    with pytest.raises(DomainError, match='费用'): probe.analyze(repo, KEYS, authorized=True)
    assert len(transport['calls']) == (1 if mode == 'unknown' else 2)
    with repo.factory() as db:
        job = db.scalars(select(Job).where(Job.kind == 'analysis')).one()
        assert job.state == 'failed'
        assert bool(job.payload.get('steps', {}).get('vision')) == (mode == 'asr_timeout')
        assert db.get(Episode, probe.EPISODE_ID).analysis_state != 'confirmed'
        assert any(row.state == 'unknown' for row in db.scalars(select(Budget)))


@pytest.mark.parametrize('mode', ['approval', 'key', 'background', 'bytes', 'asset', 'extra_asset', 'expired', 'budget'])
def test_scope_and_budget_before_send(repo, transport, mode):
    if mode == 'background':
        with repo.transaction() as db: db.get(Episode, probe.EPISODE_ID).background = 'private user text'
    if mode == 'bytes': (repo.data_dir/probe.RELATIVE).write_bytes(b'private data')
    if mode == 'asset':
        with repo.transaction() as db: db.get(Asset, probe.ASSET_ID).deleted = True
    if mode == 'extra_asset':
        with repo.transaction() as db:
            db.add(Asset(owner_id=probe.OWNER, episode_id=probe.EPISODE_ID, filename='private.mov',
                path='assets/private.mov', size=1, duration=1, width=1, height=1, has_audio=False))
    if mode == 'expired':
        with repo.transaction() as db: db.get(probe.Retention, probe.EPISODE_ID).expires_at = 1
    if mode == 'budget':
        with repo.transaction() as db: db.add(Budget(owner_id='other', episode_id='other', amount=30_000_000, state='unknown'))
    with pytest.raises(DomainError):
        probe.analyze(repo, {} if mode == 'key' else KEYS, authorized=mode != 'approval')
    assert not transport['calls']


def test_changed_configuration_does_not_reset_paid_task(repo, transport, monkeypatch):
    probe.analyze(repo, KEYS, authorized=True)
    monkeypatch.setattr(probe, 'VISION_MODEL', 'changed')
    with pytest.raises(DomainError, match='配置'): probe.analyze(repo, KEYS, authorized=True)
    assert len(transport['calls']) == 2


def test_scope_rejects_other_source_before_media_read(repo, transport, tmp_path):
    s = probe.Services(repo, KEYS, authorized=True)
    with pytest.raises(DomainError): s.prepare_vision([{'asset_id': 'private', 'path': tmp_path/'private.mov'}], repo.data_dir/'assets')
    with pytest.raises(DomainError):
        s.prepare_audio(type('A', (), {'id': 'private'})(), tmp_path/'private.mov', tmp_path)
    p = tmp_path/'private.wav'; p.write_bytes(wav_bytes(rate=16000))
    with pytest.raises(SpeechError):
        asyncio.run(probe.ScopedASR(KEYS['MINIMAX_API_KEY'], [], True).transcribe([AudioChunk(p, 'private', 0, 1)]))
    assert not transport['calls']


def approve_fixture_via_product_api(repo):
    settings = Settings(data_dir=repo.data_dir, invites={'fixture-only': probe.OWNER}, worker_enabled=False)
    with TestClient(create_app(settings)) as client:
        login = client.post('/api/v1/auth/login', json={'invite_code': 'fixture-only'})
        assert login.status_code == 200
        evidence = client.get('/api/v1/episodes/'+probe.EPISODE_ID).json()
        result = client.put('/api/v1/episodes/'+probe.EPISODE_ID+'/evidence',
            json={'revision': evidence['evidence_revision'], 'items': evidence['evidence'], 'confirmed': True},
            headers={'X-CSRF-Token': login.json()['csrf_token']})
        assert result.status_code == 200


def test_director_requires_human_review_then_stops_at_selection(repo, transport):
    probe.analyze(repo, KEYS, authorized=True)
    with pytest.raises(DomainError, match='用户核对'): probe.directions(repo, KEYS, authorized=True)
    assert len(transport['calls']) == 2
    approve_fixture_via_product_api(repo)
    result = probe.directions(repo, KEYS, authorized=True)
    assert len(transport['calls']) == 3
    with repo.factory() as db:
        ep = db.get(Episode, probe.EPISODE_ID)
        assert ep.directions[0]['id'] == 'd1' and ep.selected_direction is None
        assert ep.directions_revision == ep.evidence_revision
    assert probe.directions(repo, KEYS, authorized=True)['calls'] == result['calls']
    assert len(transport['calls']) == 3


def test_guidance_changed_after_preflight_cannot_return_old_success(repo, transport, monkeypatch):
    from app.services import director_guidance as guidance
    probe.analyze(repo, KEYS, authorized=True)
    approve_fixture_via_product_api(repo)
    probe.directions(repo, KEYS, authorized=True)
    original = probe.Services.profile

    def profile_then_change(service):
        result = original(service)
        with repo.transaction() as db:
            guidance.record(repo, db, db.get(Episode, probe.EPISODE_ID), revision=0,
                            client_id='after-preflight', choice='auto')
        return result

    monkeypatch.setattr(probe.Services, 'profile', profile_then_change)
    with pytest.raises(DomainError) as error:
        probe.directions(repo, KEYS, authorized=True)
    assert error.value.code == 'DIRECTOR_BRIEF_CHANGED'
    assert len(transport['calls']) == 3


def test_user_edits_do_not_widen_synthetic_upload_scope(repo, transport):
    probe.analyze(repo, KEYS, authorized=True)
    approve_fixture_via_product_api(repo)
    with repo.transaction() as db:
        ep = db.get(Episode, probe.EPISODE_ID)
        ep.evidence = [{**row, 'note': 'private user note'} for row in ep.evidence]
    with pytest.raises(DomainError, match='外发范围'): probe.directions(repo, KEYS, authorized=True)
    assert len(transport['calls']) == 2


def test_paid_analysis_checkpoints_recover_before_publication(repo, transport):
    probe.analyze(repo, KEYS, authorized=True)
    with repo.transaction() as db:
        ep = db.get(Episode, probe.EPISODE_ID)
        ep.evidence, ep.analysis_state, ep.evidence_revision = [], 'empty', 0
        job = db.scalars(select(Job).where(Job.kind == 'analysis')).one()
        job.state = 'failed'
        job.payload = {k: v for k, v in job.payload.items() if k != 'synthetic_evidence_digest'}
    result = probe.analyze(repo, KEYS, authorized=True)
    assert result['analysis_state'] == 'review' and not result['evidence_confirmed']
    assert len(transport['calls']) == 2
    with repo.factory() as db:
        job = db.scalars(select(Job).where(Job.kind == 'analysis')).one()
        assert job.payload['synthetic_evidence_digest'] == probe.digest(db.get(Episode, probe.EPISODE_ID).evidence)
