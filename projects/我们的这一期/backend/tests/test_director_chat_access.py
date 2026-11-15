"""Offline grant-scope tests: no private grant, credentials or real provider calls."""
import asyncio
from copy import deepcopy
import hashlib
import json
import socket

import httpx
import pytest

from app.models import Asset, Budget, Episode, Job, Version
from app.repository import DomainError, Repository
from app.services import deepseek as ds
from app.services import director_chat_access as access
from app.services.director import DirectorError
from app.services.director_chat_model import DeepSeekDirectorChat
from app.services.provider_config import cloud_configuration
from test_new_providers import NOW, envelope

CONFIG = cloud_configuration({'DEEPSEEK_API_KEY': 'offline-test-only'})


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ds.time, 'time', lambda: NOW)
    def denied(*a, **kw):
        pytest.fail('Real network is forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def sample(tmp_path):
    repo = Repository(tmp_path / 'data')
    eid = 'e' * 32
    grant = {'scope': 'real-materials-20261112', 'approval': '接受上述范围，仅授权这3段真实素材测试',
             'unknown_vendor_retention_accepted': True, 'authorized_adult_nonsensitive_materials': True,
             'owner_id': 'local-owner', 'episode_id': eid, 'task_limit_yuan': 3,
             'total_limit_yuan': 30, 'assets': []}
    evidence = [{'id': 'v1', 'kind': 'visual', 'asset_id': '1' * 32, 'start': 0.0,
                 'end': 2.0, 'note': '桌边有几位成年人。'}]
    with repo.transaction() as db:
        ep = Episode(id=eid, owner_id='local-owner', title='测试节目', background='轻松一点',
            evidence=evidence, analysis_state='confirmed', evidence_revision=1,
            evidence_confirmed_revision=1, analysis_asset_ids=[str(i) * 32 for i in range(1, 4)])
        db.add(ep)
        for i in range(1, 4):
            aid = str(i) * 32
            path = f'assets/{aid}.mov'
            raw = bytes([i]) * 64
            (repo.data_dir / path).write_bytes(raw)
            meta = {'id': aid, 'path': path, 'size': len(raw), 'duration': 8.0,
                    'width': 512, 'height': 512, 'has_audio': True}
            grant['assets'].append({**meta, 'sha256': hashlib.sha256(raw).hexdigest()})
            db.add(Asset(**meta, owner_id='local-owner', episode_id=eid, filename='test.mov'))
        db.add(Job(owner_id='local-owner', episode_id=eid, kind='analysis', state='succeeded',
            idempotency_key=grant['scope'] + ':analysis',
            payload={'model_profile': {'grant_digest': access._digest(grant),
                'scope': grant['scope'], 'sample_type': 'authorized-real-materials',
                'vision_model': ds.MODEL, 'unknown_vendor_retention_accepted': True}}))
        reservation = repo.reserve(db, 'local-owner', eid, .28)
    from app.services.retention import enroll_existing
    enroll_existing(repo)
    grant_path = repo.data_dir / access.GRANT_FILE
    grant_path.parent.mkdir()
    grant_path.write_text(json.dumps(grant, ensure_ascii=False))
    context = {'episode': {'id': eid, 'title': '测试节目', 'background': '轻松一点',
        'analysis_state': 'confirmed', 'evidence_revision': 1, 'brief_revision': 0,
        'focus': None, 'requirements': ''},
        'assets': [{'id': str(i) * 32, 'duration': 8.0, 'has_audio': True} for i in range(1, 4)],
        'confirmed_evidence': evidence, 'current_version': None,
        'history': [], 'question': '为什么这版没有故事感？', 'step': 'preview',
        'available_actions': ['navigate', 'revise_directions']}
    yield repo, grant, context, reservation
    repo.engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'result': {'reply': '这版只有桌边信息，先明确一个主线。', 'action': None}}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(request)
        return httpx.Response(200, json=envelope(state['result']))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def chat(sample, context=None, config=None, permit=None):
    repo, grant, original, reservation = sample
    return access.chat_adapter(config or CONFIG, repo, context or original, permit or (lambda r: r == reservation))


def run_chat(sample, adapter):
    return asyncio.run(adapter.chat(sample[2], reservation=sample[3]))


def test_scoped_chat_uses_real_transport_with_existing_budget_without_global_enable(sample, transport):
    before = deepcopy(CONFIG)
    adapter = chat(sample)
    assert adapter.ready()[0]
    result = run_chat(sample, adapter)
    assert len(transport['calls']) == 1
    assert result['result'].reply.startswith('这版') and result['usage']['cost_status'] == 'known'
    assert CONFIG == before and CONFIG['CLOUD_ENABLED'] is False and CONFIG['DIRECTOR_PROVIDER'] == 'bailian'
    assert CONFIG['DEEPSEEK_RETENTION_VERIFIED'] is False
    assert transport['calls'][0].url.host == 'api.deepseek.com'


def test_ready_checks_metadata_send_checks_every_byte(sample, transport, monkeypatch):
    adapter = chat(sample)
    calls = []
    original = access._file_digest
    monkeypatch.setattr(access, '_file_digest', lambda p: calls.append(p) or original(p))
    assert adapter.ready()[0] and calls == []
    run_chat(sample, adapter)
    assert len(calls) == 3 and len(transport['calls']) == 1


@pytest.mark.parametrize('mode', ['missing', 'extra', 'deleted', 'owner', 'wrong_episode', 'metadata', 'bytes',
                                 'symlink', 'expired', 'consent', 'analysis', 'grant_changed'])
def test_scope_drift_stops_before_http(sample, transport, mode):
    repo, grant, context, reservation = sample
    adapter = chat(sample)
    assert adapter.ready()[0]
    grant_path = repo.data_dir / access.GRANT_FILE
    first = grant['assets'][0]
    if mode == 'missing': grant_path.unlink()
    if mode == 'consent':
        grant['unknown_vendor_retention_accepted'] = False
        grant_path.write_text(json.dumps(grant))
    if mode == 'grant_changed':
        grant['modified'] = True
        grant_path.write_text(json.dumps(grant))
    if mode == 'wrong_episode': adapter._context['episode']['id'] = 'f' * 32
    if mode == 'bytes': (repo.data_dir / first['path']).write_bytes(b'x' * first['size'])
    if mode == 'symlink':
        path = repo.data_dir / first['path']
        backup = path.with_suffix('.backup')
        path.rename(backup)
        path.symlink_to(backup)
    with repo.transaction() as db:
        if mode == 'extra':
            db.add(Asset(owner_id='local-owner', episode_id=grant['episode_id'], filename='extra',
                path='assets/extra', size=1, duration=1, width=1, height=1, has_audio=False))
        if mode == 'deleted': db.get(Asset, first['id']).deleted = True
        if mode == 'owner': db.get(Episode, grant['episode_id']).owner_id = 'other'
        if mode == 'metadata': db.get(Asset, first['id']).duration = 4
        if mode == 'analysis':
            ep = db.get(Episode, grant['episode_id'])
            ep.analysis_asset_ids = []
        if mode == 'expired':
            from app.retention_models import Retention
            db.get(Retention, grant['episode_id']).expires_at = 1
    with pytest.raises(DirectorError): run_chat(sample, adapter)
    assert not transport['calls']


@pytest.mark.parametrize('field,value', [('DEEPSEEK_BASE_URL', 'https://example.org'),
                                        ('DEEPSEEK_MODEL', 'other'), ('DEEPSEEK_API_KEY', 'bad key'),
                                        ('DIRECTOR_PROVIDER', 'unsupported')])
def test_grant_does_not_replace_independent_provider_constraints(sample, transport, field, value):
    adapter = chat(sample, config={**CONFIG, field: value})
    assert not adapter.ready()[0]
    with pytest.raises(DirectorError): run_chat(sample, adapter)
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['permit', 'other_reservation', 'settled', 'total', 'task'])
def test_budget_is_still_enforced(sample, transport, mode):
    repo, grant, _, reservation = sample
    adapter = chat(sample, permit=(lambda _: False) if mode == 'permit' else None)
    with repo.transaction() as db:
        if mode == 'other_reservation': db.get(Budget, reservation).episode_id = 'different'
        if mode == 'settled': db.get(Budget, reservation).state = 'settled'
        if mode in ('task', 'total'):
            db.add(Budget(owner_id='local-owner', episode_id=grant['episode_id'] if mode == 'task' else 'other',
                          state='unknown', amount=3_000_000 if mode == 'task' else 30_000_000))
    with pytest.raises(DirectorError): run_chat(sample, adapter)
    assert not transport['calls']


@pytest.mark.parametrize('field', ['confirmed_evidence', 'episode', 'assets', 'current_version'])
def test_foreign_or_changed_context_does_not_reach_provider(sample, transport, field):
    context = deepcopy(sample[2])
    if field == 'confirmed_evidence': context[field][0]['note'] = 'Injected fact'
    if field == 'episode': context[field]['background'] = 'Unsaved background'
    if field == 'assets': context[field][0]['id'] = 'f' * 32
    if field == 'current_version': context[field] = {'id': 'f' * 32}
    adapter = chat(sample, context=context)
    with pytest.raises((DirectorError, DomainError)):
        asyncio.run(adapter.chat(context, reservation=sample[3]))
    assert not transport['calls']


def test_changed_grant_during_async_budget_permit_stops_before_post(sample, transport):
    repo, grant, context, reservation = sample
    calls = 0
    async def permit(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo.data_dir / grant['assets'][1]['path']).write_bytes(b'x' * grant['assets'][1]['size'])
        return True
    with pytest.raises(DirectorError): run_chat(sample, chat(sample, permit=permit))
    assert calls == 2 and not transport['calls']


def test_normal_enabled_configuration_needs_no_grant(sample):
    config = {**CONFIG, 'CLOUD_ENABLED': True, 'DIRECTOR_PROVIDER': 'deepseek', 'DEEPSEEK_ENABLED': True,
              'DEEPSEEK_RETENTION_VERIFIED': True, 'DEEPSEEK_REGION_VERIFIED': True}
    assert type(access.chat_adapter(config, sample[0], {'episode': {'id': 'other'}})) is DeepSeekDirectorChat
    assert type(access.directions_adapter(config, sample[0], {'episode': {'id': 'other'}})) is ds.DeepSeekDirector


def test_missing_context_does_not_enable_global_director():
    assert not access.chat_adapter(CONFIG).ready()[0]
    assert not access.directions_adapter(CONFIG).ready()[0]


def test_scoped_direction_checks_confirmed_payload_then_uses_existing_transport(sample, transport):
    repo, grant, context, reservation = sample
    evidence = context['confirmed_evidence']
    spans = [{k: evidence[0][k] for k in ('asset_id', 'start', 'end', 'note')}]
    transport['result'] = {'directions': [{'id': 'one', 'title': '桌边这一刻', 'summary': '用同桌画面开场。',
        'evidence': spans, 'edit_plan': {'segments': [{'asset_id': spans[0]['asset_id'], 'start': 0.0,
        'end': 2.0, 'caption': '', 'purpose': '交代聚会环境', 'evidence_ids': ['v1']}], 'narration_text': ''}}], 'uncertainties': []}
    adapter = access.directions_adapter(CONFIG, repo, {'episode': {'id': grant['episode_id']}}, lambda _: True)
    assert adapter.ready()[0]
    result = asyncio.run(adapter.propose(evidence, {a['id']: a['duration'] for a in context['assets']},
                                        context['episode']['background'], reservation=reservation))
    assert result['result'].directions[0].id == 'one' and len(transport['calls']) == 1


def test_scoped_direction_rejects_unconfirmed_or_changed_payload(sample, transport):
    repo, grant, context, reservation = sample
    adapter = access.directions_adapter(CONFIG, repo, context, lambda _: True)
    with pytest.raises(DirectorError):
        asyncio.run(adapter.propose(context['confirmed_evidence'], {a['id']: a['duration'] for a in context['assets']},
                                    'different background', reservation=reservation))
    with repo.transaction() as db:
        db.get(Episode, grant['episode_id']).evidence_confirmed_revision = None
    with pytest.raises(DirectorError):
        asyncio.run(adapter.propose(context['confirmed_evidence'], {a['id']: a['duration'] for a in context['assets']},
                                    context['episode']['background'], reservation=reservation))
    assert not transport['calls']


def test_scoped_provider_is_bound_to_original_analysis_model(sample, transport):
    repo, grant, context, reservation = sample
    with repo.transaction() as db:
        from sqlalchemy import select
        job = db.scalars(select(Job).where(Job.kind == 'analysis')).one()
        profile = {**job.payload['model_profile'], 'vision_model': 'different-provider'}
        job.payload = {'model_profile': profile}
    adapter = chat(sample)
    assert not adapter.ready()[0]
    with pytest.raises(DirectorError): run_chat(sample, adapter)
    assert not transport['calls']


def test_raw_request_cannot_send_arbitrary_payload_under_grant(sample, transport):
    adapter = chat(sample)
    assert adapter.ready()[0]
    with pytest.raises(DirectorError):
        asyncio.run(adapter.request([{'role': 'user', 'content': 'other private data'}], sample[3]))
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['duplicate', 'oversize', 'symlink', 'missing_field'])
def test_malformed_grant_fails_closed_without_exposing_private_input(sample, transport, mode):
    repo, grant, _, _ = sample
    path = repo.data_dir / access.GRANT_FILE
    if mode == 'duplicate':
        path.write_text('{"private":"secret-data","private":"other"}')
    elif mode == 'oversize':
        path.write_text(' ' * 20_001)
    elif mode == 'symlink':
        backup = path.with_suffix('.backup')
        path.rename(backup)
        path.symlink_to(backup)
    else:
        grant.pop('approval')
        path.write_text(json.dumps(grant))
    adapter = chat(sample)
    assert not adapter.ready()[0]
    with pytest.raises(DirectorError) as caught:
        run_chat(sample, adapter)
    assert 'secret-data' not in str(caught.value)
    assert not transport['calls']
