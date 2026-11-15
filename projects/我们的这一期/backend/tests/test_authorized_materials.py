"""Real local media and production pipeline; model HTTP is explicitly simulated."""
import asyncio
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import socket
import sys

import httpx
import pytest
from sqlalchemy import select
from app.models import Asset, Budget, Episode, Job, ModelCall, Version
from app.repository import DomainError, Repository
from app.services import deepseek as ds
from app.services.speech import AudioChunk, SpeechError
from test_minimax_speech import wav_bytes
from test_minimax_asr import envelope as asr_envelope
from test_new_providers import envelope as ds_envelope, NOW

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
trial = importlib.import_module('authorized_materials')
from synthetic_chain import build_fixture

KEYS = {'DEEPSEEK_API_KEY': 'offline-test-only', 'MINIMAX_API_KEY': 'offline-test-only'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ds.time, 'time', lambda: NOW)
    def denied(*a, **kw):
        pytest.fail('Real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture(scope='module')
def movie(tmp_path_factory):
    folder = tmp_path_factory.mktemp('trial-video')
    return build_fixture(wav_bytes(), folder).read_bytes()


@pytest.fixture
def setup(tmp_path, movie):
    repo = Repository(tmp_path / 'data')
    ep = Episode(id='e' * 32, owner_id='local-owner', title='offline only')
    grant = {'scope': 'real-materials-20261112', 'approval': '接受上述范围，仅授权这3段真实素材测试',
             'unknown_vendor_retention_accepted': True, 'authorized_adult_nonsensitive_materials': True,
             'owner_id': 'local-owner', 'episode_id': ep.id, 'task_limit_yuan': 3, 'total_limit_yuan': 30, 'assets': []}
    with repo.transaction() as db:
        db.add(ep)
        for i in range(1, 4):
            aid = str(i) * 32
            path = repo.data_dir / f'assets/{aid}.mov'
            path.write_bytes(movie + bytes([i]))
            metadata = {'id': aid, 'path': f'assets/{aid}.mov', 'size': path.stat().st_size,
                        'duration': 8.0, 'width': 512, 'height': 512, 'has_audio': True}
            db.add(Asset(**metadata, owner_id=ep.owner_id, episode_id=ep.id, filename='offline.mov'))
            grant['assets'].append({**metadata, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    # Enroll through the same existing-episode retention migration as the server.
    from app.services.retention import enroll_existing
    enroll_existing(repo)
    yield repo, grant
    repo.engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'timeout': False, 'bad_second': False}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(request)
        if request.url.host == 'api.deepseek.com':
            content = json.loads(request.content)['messages'][0]['content']
            frames = [json.loads(part['text']) for part in content[1:] if part['type'] == 'text']
            output = {'observations': [{**f, 'note': '测试几何画面', 'verification': 'unverified'} for f in frames],
                      'uncertainties': []}
            return httpx.Response(200, json=ds_envelope(output))
        assert request.url.host == 'api.minimax.cn'
        if state['timeout']:
            raise httpx.ReadTimeout('private upstream data')
        data = asr_envelope()
        if state['bad_second'] and len(state['calls']) >= 3:
            data['segments'][0]['end'] = 0
        return httpx.Response(200, json=data)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def test_complete_analysis_persists_without_confirming_or_recharging(setup, transport):
    repo, grant = setup
    with repo.transaction() as db:
        db.add(Budget(owner_id='other', episode_id='other', amount=24500, state='unknown'))
    first = trial.analyze(repo, KEYS, grant)
    assert len(transport['calls']) == 4
    assert first['analysis_state'] == 'review' and not first['human_confirmed']
    assert first['visual_count'] == 3 and first['speech_count'] == 3
    assert first['budget']['total_reserved'] == .0245
    again = Repository(repo.data_dir)
    assert trial.analyze(again, KEYS, grant)['calls'] == first['calls']
    again.engine.dispose()
    assert len(transport['calls']) == 4
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert ep.selected_direction is None and not ep.directions
        job = db.scalars(select(Job)).one()
        assert job.payload['model_profile']['retention_verified'] is False
    assert '测试几何画面' not in json.dumps(first, ensure_ascii=False)
    assert KEYS['MINIMAX_API_KEY'] not in json.dumps(first)


@pytest.mark.parametrize('mode', ['consent', 'extra', 'deleted', 'bytes', 'metadata', 'symlink', 'budget', 'expired', 'key'])
def test_scope_and_budget_stop_before_network(setup, transport, mode):
    repo, original = setup
    grant = deepcopy(original)
    aid = grant['assets'][0]['id']
    if mode == 'consent': grant['unknown_vendor_retention_accepted'] = False
    if mode == 'bytes': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'other')
    if mode == 'symlink':
        p = repo.data_dir / grant['assets'][0]['path']
        backup = p.with_suffix('.backup'); p.rename(backup); p.symlink_to(backup)
    with repo.transaction() as db:
        if mode == 'extra':
            db.add(Asset(owner_id='local-owner', episode_id=grant['episode_id'], filename='other', path='assets/other',
                         size=1, duration=1, width=1, height=1, has_audio=False))
        if mode == 'deleted': db.get(Asset, aid).deleted = True
        if mode == 'metadata': db.get(Asset, aid).duration = 9
        if mode == 'budget': db.add(Budget(owner_id='other', episode_id='other', amount=30_000_000, state='unknown'))
        if mode == 'expired':
            from app.retention_models import Retention
            db.get(Retention, grant['episode_id']).expires_at = 1
    with pytest.raises(DomainError):
        trial.analyze(repo, {} if mode == 'key' else KEYS, grant)
    assert not transport['calls']


def test_timeout_keeps_paid_vision_and_unknown_reservation_no_retry(setup, transport):
    repo, grant = setup
    transport['timeout'] = True
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant)
    with pytest.raises(DomainError, match='不会自动重发'): trial.analyze(repo, KEYS, grant)
    assert len(transport['calls']) == 2
    with repo.factory() as db:
        job = db.scalars(select(Job)).one()
        assert job.state == 'failed' and 'vision' in job.payload['steps']
        assert any(b.state == 'unknown' for b in db.scalars(select(Budget)))
        assert db.get(Episode, grant['episode_id']).analysis_state != 'confirmed'


def test_other_inputs_rejected_before_preparing_or_sending(setup, transport, tmp_path):
    repo, grant = setup
    s = trial.Services(repo, KEYS, grant)
    with pytest.raises(DomainError): s.prepare_vision([], repo.data_dir / 'assets')
    with pytest.raises(DomainError):
        s.prepare_audio(type('A', (), {'id': 'other'})(), tmp_path / 'private.mov', tmp_path)
    p = tmp_path / 'voice.wav'; p.write_bytes(wav_bytes(rate=16000))
    with pytest.raises(SpeechError):
        asyncio.run(trial.GrantedASR(KEYS['MINIMAX_API_KEY'], [], s.recheck).transcribe([AudioChunk(p, 'other', 0, 1)]))
    assert not transport['calls']


def test_changed_profile_cannot_reuse_or_resend_paid_task(setup, transport):
    repo, grant = setup
    trial.analyze(repo, KEYS, grant)
    changed = {**grant, 'changed': True}
    with pytest.raises(DomainError, match='配置发生变化'): trial.analyze(repo, KEYS, changed)
    assert len(transport['calls']) == 4


def test_active_job_not_duplicated(setup, transport):
    repo, grant = setup
    with repo.transaction() as db:
        db.add(Job(owner_id='local-owner', episode_id=grant['episode_id'], kind='analysis', state='running',
                   idempotency_key='other', payload={}))
    with pytest.raises(DomainError, match='已有任务'): trial.analyze(repo, KEYS, grant)
    assert not transport['calls']


def test_explicit_known_cost_resume_reuses_successful_steps(setup, transport):
    repo, grant = setup
    transport['bad_second'] = True
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant)
    assert len(transport['calls']) == 3
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant)
    transport['bad_second'] = False
    result = trial.analyze(repo, KEYS, grant, resume=True)
    assert result['analysis_state'] == 'review'
    assert len(transport['calls']) == 5  # No repeat of vision or first audio.
    assert len([c for c in result['calls'] if c['state'] == 'failed']) == 1


def test_explicit_resume_does_not_allow_unknown_or_endless_retries(setup, transport):
    repo, grant = setup
    transport['bad_second'] = True
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant)
    with repo.transaction() as db:
        failed = db.scalars(select(ModelCall).where(ModelCall.state == 'failed')).one()
        ledger = db.get(Budget, failed.reservation_id); ledger.state = 'unknown'
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant, resume=True)
    assert len(transport['calls']) == 3
    with repo.transaction() as db:
        db.get(Budget, failed.reservation_id).state = 'settled'
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant, resume=True)
    assert len(transport['calls']) == 4
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant, resume=True)
    assert len(transport['calls']) == 5
    with pytest.raises(DomainError): trial.analyze(repo, KEYS, grant, resume=True)
    assert len(transport['calls']) == 5


@pytest.fixture
def confirmed(setup):
    """Persist an already reviewed offline analysis; no provider work is repeated."""
    repo, grant = setup
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        ep.analysis_state = 'confirmed'
        ep.evidence_revision = ep.evidence_confirmed_revision = 5
        ep.analysis_asset_ids = [asset['id'] for asset in grant['assets']]
        ep.evidence = [{'id': 'reviewed-' + str(i), 'asset_id': asset['id'],
                        'start': 1., 'end': 4., 'note': '已确认的几何画面', 'kind': 'visual'}
                       for i, asset in enumerate(grant['assets'])]
        ep.background = '离线测试的固定背景'
        db.add(Job(owner_id=ep.owner_id, episode_id=ep.id, kind='analysis', state='succeeded',
                   idempotency_key=grant['scope'] + ':analysis', payload={
                       'model_profile': trial.Services(repo, KEYS, grant).profile(), 'steps': {}}))
    return repo, grant


@pytest.fixture
def director_transport(monkeypatch):
    state = {'calls': [], 'mode': 'valid', 'during_request': None}
    original = httpx.AsyncClient

    def handler(request):
        state['calls'].append(request)
        assert request.url.host == 'api.deepseek.com'
        body = json.loads(request.content)
        assert all(isinstance(message['content'], str) for message in body['messages'])
        payload = json.loads(body['messages'][1]['content'])
        assert body['model'] == ds.MODEL
        if state['during_request']:
            state['during_request']()
        if state['mode'] == 'timeout':
            raise httpx.ReadTimeout('private provider detail must not be returned')
        evidence = payload['verified_evidence'][:1]
        value = {'directions': [{'id': 'd1', 'title': '几何开场', 'summary': '根据已确认片段组织开场',
                                 'evidence': [{k: row[k] for k in ('asset_id', 'start', 'end', 'note')} for row in evidence], 'edit_plan': {
                                     'segments': [{**{k: evidence[0][k] for k in ('asset_id', 'start', 'end')},
                                                   'caption': '几何开场', 'purpose': '交代画面场景',
                                                   'evidence_ids': [evidence[0]['id']]}], 'narration_text': ''}}],
                 'uncertainties': []}
        if state['mode'] == 'invalid':
            value = {'unrecognized': True}
        elif state['mode'] == 'ungrounded':
            value['directions'][0]['evidence'][0]['end'] = 5.
        return httpx.Response(200, json=ds_envelope(value))

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def direction_job(db, grant):
    return db.scalars(select(Job).where(Job.idempotency_key == grant['scope'] + ':directions')).one()


def change_prompt(monkeypatch):
    """Change only the read result; never edit the actual product prompt."""
    target = trial.PRODUCT / 'backend/app/services/prompts/director.txt'
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) + (b' changed' if path == target else b''))


def test_directions_only_one_text_call_and_restart_is_idempotent(confirmed, director_transport):
    repo, grant = confirmed
    keys = {'DEEPSEEK_API_KEY': KEYS['DEEPSEEK_API_KEY']}
    with repo.factory() as db:
        evidence = deepcopy(db.get(Episode, grant['episode_id']).evidence)
    first = trial.directions(repo, keys, grant)
    assert len(director_transport['calls']) == 1
    assert first['direction_count'] == 1 and first['directions_revision'] == 5
    assert [call['kind'] for call in first['calls']] == ['directions']
    assert first['calls'][0]['ledger_state'] == 'settled'
    assert first['calls'][0]['cost_yuan'] > 0
    again = Repository(repo.data_dir)
    try:
        again.recover()
        assert trial.directions(again, keys, grant)['calls'] == first['calls']
    finally:
        again.engine.dispose()
    assert len(director_transport['calls']) == 1
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert ep.evidence == evidence and ep.evidence_confirmed_revision == 5
        assert ep.selected_direction is None and not list(db.scalars(select(Version)))
        job = direction_job(db, grant)
        assert job.state == 'succeeded' and job.payload['model_profile']['retention_verified'] is False
        assert job.payload['model_profile']['input_digest']
    public = json.dumps(first, ensure_ascii=False)
    assert keys['DEEPSEEK_API_KEY'] not in public
    assert '已确认的几何画面' not in public and '离线测试的固定背景' not in public



def test_validation_direction_preserves_current_choice_and_never_recharges(confirmed, director_transport):
    repo, grant = confirmed
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        ep.directions = [{'id': 'keep-current', 'title': '已有方案'}]
        ep.selected_direction = 'keep-current'
        ep.directions_revision = 5
        ep.analysis_notes = ['已有核对备注']
    first = trial.directions(repo, KEYS, grant, validation_only=True)
    assert first['state'] == 'succeeded' and first['direction_count'] == 1
    assert len(director_transport['calls']) == 1
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert ep.directions == [{'id': 'keep-current', 'title': '已有方案'}]
        assert ep.selected_direction == 'keep-current' and ep.directions_revision == 5
        assert ep.analysis_notes == ['已有核对备注']
        job = db.get(Job, first['job_id'])
        assert job.result['validation_only'] is True
        assert job.result['directions']['directions'][0]['id'] == 'd1'
    assert trial.directions(repo, KEYS, grant, validation_only=True) == first
    assert len(director_transport['calls']) == 1


def test_validation_direction_failure_is_not_automatically_retried(confirmed, director_transport):
    repo, grant = confirmed
    director_transport['mode'] = 'timeout'
    with pytest.raises(DomainError):
        trial.directions(repo, KEYS, grant, validation_only=True)
    with pytest.raises(DomainError):
        trial.directions(repo, KEYS, grant, validation_only=True)
    assert len(director_transport['calls']) == 1


def test_validation_analysis_preserves_review_and_paid_steps(confirmed, transport):
    repo, grant = confirmed
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        before = deepcopy(ep.evidence), ep.evidence_revision, ep.analysis_state, ep.evidence_confirmed_revision
    result = trial.analyze(repo, KEYS, grant, validation_only=True)
    assert result['state'] == 'succeeded' and result['evidence_count'] > 0
    assert len(transport['calls']) == 4
    assert trial.analyze(repo, KEYS, grant, validation_only=True) == result
    assert len(transport['calls']) == 4
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert (ep.evidence, ep.evidence_revision, ep.analysis_state, ep.evidence_confirmed_revision) == before
        job = db.get(Job, result['job_id'])
        assert job.result['validation_only'] is True
        assert len(job.payload['steps']) == 4


def test_validation_analysis_timeout_keeps_review_and_cannot_auto_retry(confirmed, transport):
    repo, grant = confirmed
    transport['timeout'] = True
    with pytest.raises(DomainError):trial.analyze(repo, KEYS, grant, validation_only=True)
    count = len(transport['calls'])
    with pytest.raises(DomainError):trial.analyze(repo, KEYS, grant, validation_only=True)
    assert len(transport['calls']) == count
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert ep.analysis_state == 'confirmed' and ep.evidence_confirmed_revision == 5


def test_same_brief_text_with_new_revision_cannot_reuse_succeeded_task(confirmed, director_transport):
    from app.services import director_guidance as guidance
    repo, grant = confirmed
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        guidance.record(repo, db, ep, revision=0, client_id='initial-choice', choice='warm')
    trial.directions(repo, KEYS, grant)
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        earlier = guidance.compiled_background(db, ep)
        guidance.record(repo, db, ep, revision=1, client_id='repeated-choice', choice='warm')
        assert guidance.compiled_background(db, ep) == earlier
        assert not guidance.directions_current(db, ep)
    with pytest.raises(DomainError) as error:
        trial.directions(repo, KEYS, grant)
    assert error.value.code == 'DIRECTOR_BRIEF_CHANGED'
    assert len(director_transport['calls']) == 1


@pytest.mark.parametrize('mode', ['unconfirmed', 'empty', 'revision', 'analysis_failed', 'analysis_scope',
                                  'source_ids', 'evidence_source', 'evidence_bounds', 'key'])
def test_direction_admission_blocks_before_network(confirmed, director_transport, mode):
    repo, grant = confirmed
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        if mode == 'unconfirmed': ep.analysis_state = 'review'
        if mode == 'empty': ep.evidence = []
        if mode == 'revision': ep.evidence_confirmed_revision = 4
        if mode == 'source_ids': ep.analysis_asset_ids = ep.analysis_asset_ids[:1]
        if mode in ('evidence_source', 'evidence_bounds'):
            rows = deepcopy(ep.evidence)
            if mode == 'evidence_source': rows[0]['asset_id'] = 'other'
            else: rows[0]['end'] = 9.
            ep.evidence = rows
        if mode in ('analysis_failed', 'analysis_scope'):
            job = db.scalars(select(Job)).one()
            if mode == 'analysis_failed': job.state = 'failed'
            else: job.payload = {'model_profile': {'grant_digest': 'other'}}
    with pytest.raises((DomainError, trial.DirectorError)):
        trial.directions(repo, {} if mode == 'key' else KEYS, grant)
    assert not director_transport['calls']


@pytest.mark.parametrize('mode', ['evidence', 'background', 'revision', 'prompt', 'bytes', 'deleted'])
def test_direction_frozen_request_rechecked_before_sending(confirmed, director_transport, monkeypatch, mode):
    repo, grant = confirmed
    services = trial.DirectionServices(repo, KEYS, grant)
    _, payload = trial.confirmed_input(repo, grant)
    if mode == 'prompt': change_prompt(monkeypatch)
    if mode == 'bytes': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'changed')
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        if mode == 'evidence': ep.evidence = [{**row, 'note': 'changed'} for row in ep.evidence]
        if mode == 'background': ep.background = 'changed'
        if mode == 'revision': ep.evidence_revision = ep.evidence_confirmed_revision = 6
        if mode == 'deleted': db.get(Asset, grant['assets'][0]['id']).deleted = True
    assert not services.ready()[0]
    with pytest.raises((DomainError, trial.DirectorError)):
        asyncio.run(services.directions(*payload, reservation='offline', permit=lambda _: True))
    assert not director_transport['calls']


@pytest.mark.parametrize('mode', ['evidence', 'assets', 'background'])
def test_direction_rejects_changed_call_arguments(confirmed, director_transport, mode):
    repo, grant = confirmed
    services = trial.DirectionServices(repo, KEYS, grant)
    _, payload = trial.confirmed_input(repo, grant)
    if mode == 'evidence': payload[0][0]['note'] = 'unapproved extra text'
    if mode == 'assets': payload[1][grant['assets'][0]['id']] = 9.
    if mode == 'background': payload[2] = 'unapproved background'
    with pytest.raises(trial.DirectorError, match='编导输入'):
        asyncio.run(services.directions(*payload, reservation='offline', permit=lambda _: True))
    assert not director_transport['calls']


@pytest.mark.parametrize('mode', ['evidence', 'background', 'revision', 'prompt'])
def test_paid_direction_request_cannot_be_replaced(confirmed, director_transport, monkeypatch, mode):
    repo, grant = confirmed
    trial.directions(repo, KEYS, grant)
    if mode == 'prompt': change_prompt(monkeypatch)
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        if mode == 'evidence': ep.evidence = [{**row, 'note': 'changed'} for row in ep.evidence]
        if mode == 'background': ep.background = 'changed'
        if mode == 'revision': ep.evidence_revision = ep.evidence_confirmed_revision = 6
    with pytest.raises(DomainError, match='不重复调用'):
        trial.directions(repo, KEYS, grant)
    assert len(director_transport['calls']) == 1


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_direction_blocks_other_active_job(confirmed, director_transport, state):
    repo, grant = confirmed
    with repo.transaction() as db:
        db.add(Job(owner_id='local-owner', episode_id=grant['episode_id'], kind='analysis', state=state,
                   idempotency_key='different-job'))
    with pytest.raises(DomainError, match='已有任务'):
        trial.directions(repo, KEYS, grant)
    assert not director_transport['calls']


def test_direction_operator_lock_prevents_parallel_invocations(confirmed, director_transport):
    repo, grant = confirmed
    with trial.locked(repo):
        with pytest.raises(DomainError, match='已有授权素材验证'):
            with trial.locked(repo):
                trial.directions(repo, KEYS, grant)
    assert not director_transport['calls']


@pytest.mark.parametrize('mode', ['episode', 'aggregate', 'pending'])
def test_direction_respects_existing_costs_and_budget(confirmed, director_transport, mode):
    repo, grant = confirmed
    with repo.transaction() as db:
        db.add(Budget(owner_id='local-owner', episode_id='other' if mode == 'aggregate' else grant['episode_id'],
                      amount=30_000_000 if mode == 'aggregate' else 2_900_000,
                      cost=30_000_000 if mode == 'aggregate' else 2_900_000,
                      state='unknown' if mode == 'pending' else 'settled'))
    with pytest.raises(DomainError) as error:
        trial.directions(repo, KEYS, grant)
    assert error.value.code == ('COST_UNKNOWN' if mode == 'pending' else 'BUDGET_EXCEEDED')
    assert not director_transport['calls']


@pytest.mark.parametrize('mode', ['timeout', 'invalid', 'ungrounded'])
def test_direction_failure_retains_cost_and_never_automatically_retries(confirmed, director_transport, mode):
    repo, grant = confirmed
    director_transport['mode'] = mode
    with pytest.raises(DomainError): trial.directions(repo, KEYS, grant)
    repo.recover()
    with pytest.raises(DomainError, match='不会自动重发'): trial.directions(repo, KEYS, grant)
    assert len(director_transport['calls']) == 1
    with repo.factory() as db:
        call = db.scalars(select(ModelCall)).one()
        ledger = db.get(Budget, call.reservation_id)
        assert call.state == 'failed'
        assert ledger.state == ('unknown' if mode == 'timeout' else 'settled')
        assert (ledger.cost is None) == (mode == 'timeout')
        assert direction_job(db, grant).state == 'failed'
        assert not db.get(Episode, grant['episode_id']).directions


def test_direction_recovers_paid_checkpoint_without_new_call(confirmed, director_transport, monkeypatch):
    repo, grant = confirmed
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise RuntimeError('simulated interruption before publication commit')
        patch.setattr(trial.Pipeline, 'finish', interrupted)
        with pytest.raises(RuntimeError): trial.directions(repo, KEYS, grant)
    with repo.factory() as db:
        assert 'directions' in direction_job(db, grant).payload['steps']
        assert not db.get(Episode, grant['episode_id']).directions
    restarted = Repository(repo.data_dir)
    try:
        restarted.recover()
        assert trial.directions(restarted, KEYS, grant)['direction_count'] == 1
    finally:
        restarted.engine.dispose()
    assert len(director_transport['calls']) == 1
    with repo.factory() as db:
        assert db.get(Episode, grant['episode_id']).selected_direction is None
        assert not list(db.scalars(select(Version)))


def test_direction_revision_changed_during_request_keeps_paid_result_unpublished(confirmed, director_transport):
    repo, grant = confirmed
    def changed():
        with repo.transaction() as db:
            ep = db.get(Episode, grant['episode_id'])
            ep.evidence_revision = ep.evidence_confirmed_revision = 6
    director_transport['during_request'] = changed
    with pytest.raises(DomainError): trial.directions(repo, KEYS, grant)
    with pytest.raises(DomainError): trial.directions(repo, KEYS, grant)
    assert len(director_transport['calls']) == 1
    with repo.factory() as db:
        assert not db.get(Episode, grant['episode_id']).directions
        assert 'directions' in direction_job(db, grant).payload['steps']
        call = db.scalars(select(ModelCall)).one()
        assert db.get(Budget, call.reservation_id).state == 'settled'
