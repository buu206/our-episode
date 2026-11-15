"""Version-bound narration with real persistence and simulated provider HTTP only."""
import asyncio
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import socket

import httpx
import pytest
from sqlalchemy import select

from app.models import Asset, Budget, Episode, Job, ModelCall, Version
from app.repository import DomainError, Repository
from app.services.speech import SpeechError
from test_authorized_materials import setup, confirmed, movie
from test_minimax_speech import envelope, wav_bytes

n = importlib.import_module('authorized_narration')
KEYS = {'MINIMAX_API_KEY': 'offline-narration-test-only'}
TEXT = '镜头给到这一桌！在场，开拍！'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def ready(confirmed):
    repo, grant = confirmed
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id'])
        ep.selected_direction = 'direction_2'
        v = Version(id='a' * 32, owner_id=ep.owner_id, episode_id=ep.id, title='offline narration',
                    origin='ai', evidence_revision=5, direction_id='direction_2', packaging='standard',
                    narration_text=TEXT, segments=[{'asset_id': grant['assets'][0]['id'],
                                                   'start': 1., 'end': 3., 'caption': '测试'}])
        db.add(v); db.flush()
        approval = {'scope': grant['scope'], 'approval': '旁白已确认', 'grant_digest': n.digest(grant),
                    'version_id': v.id, 'revision': 5, 'text_sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
                    'version_digest': n.version_digest(v), 'voice': deepcopy(n.VOICE_CONFIG)}
    approval['evidence_digest'] = n.digest(n.confirmed_input(repo, grant)[1])
    return repo, grant, approval


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'mode': 'valid', 'during_request': None}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(request)
        assert str(request.url) == 'https://api.minimax.cn/v1/t2a_v2'
        body = json.loads(request.content)
        assert body['text'] == TEXT and body['model'] == 'speech-2.8-hd'
        assert body['voice_setting'] == {'voice_id': 'male-qn-daxuesheng', 'speed': 1.15,
                                          'pitch': 0, 'vol': 1, 'emotion': 'happy'}
        if state['during_request']:
            state['during_request']()
        if state['mode'] == 'timeout':
            raise httpx.ReadTimeout('private provider error')
        data = envelope()
        if state['mode'] == 'invalid': data['data']['audio'] = 'invalid'
        return httpx.Response(200, json=data)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def get_job(db, approval):
    return db.scalars(select(Job).where(Job.version_id == approval['version_id'])).one()


def test_frozen_audition_reuses_old_approval_without_mutating_version(ready, transport):
    repo, grant, approval = ready
    with repo.transaction() as db:
        v = db.get(Version, approval['version_id']);v.narration_approved = True;v.state = 'ready'
        ep = db.get(Episode, grant['episode_id']);ep.background = '新的导演要求，不是新旁白授权'
        before = v.state, v.job_id, v.narration_path, v.narration_approved, v.output_path
    assert not n.NarrationServices(repo, KEYS, grant, approval).ready()[0]
    result = n.narrate(repo, KEYS, grant, approval, validation_only=True)
    assert result['state'] == 'succeeded' and result['validation_only']
    assert (repo.data_dir / result['audio_path']).read_bytes() == wav_bytes()
    assert len(transport['calls']) == 1
    with repo.factory() as db:
        v = db.get(Version, approval['version_id'])
        assert (v.state, v.job_id, v.narration_path, v.narration_approved, v.output_path) == before
    repo.recover()
    from app.services.artifacts import recover_artifacts
    recover_artifacts(repo)
    assert n.narrate(repo, KEYS, grant, approval, validation_only=True)['job_id'] == result['job_id']
    assert len(transport['calls']) == 1
    assert (repo.data_dir / result['audio_path']).is_file()


@pytest.mark.parametrize('change', ['text', 'approval', 'source'])
def test_frozen_audition_still_rejects_unapproved_text_or_sources(ready, transport, change):
    repo, grant, approval = ready
    with repo.transaction() as db:
        v = db.get(Version, approval['version_id']);v.narration_approved = True
        if change == 'text':v.narration_text = '未经确认的新文案'
        if change == 'approval':v.narration_approved = False
        if change == 'source':db.get(Asset, grant['assets'][0]['id']).deleted = True
    with pytest.raises(DomainError):n.narrate(repo, KEYS, grant, approval, validation_only=True)
    assert transport['calls'] == []


def test_frozen_audition_timeout_preserves_version_and_holds_cost(ready, transport):
    repo, grant, approval = ready
    with repo.transaction() as db:
        v = db.get(Version, approval['version_id']);v.narration_approved = True;v.state = 'ready'
    transport['mode'] = 'timeout'
    with pytest.raises(DomainError):n.narrate(repo, KEYS, grant, approval, validation_only=True)
    with pytest.raises(DomainError):n.narrate(repo, KEYS, grant, approval, validation_only=True)
    assert len(transport['calls']) == 1
    with repo.factory() as db:
        assert db.get(Version, approval['version_id']).state == 'ready'
        assert list(db.scalars(select(Budget)))[0].state == 'unknown'


def test_one_tts_only_and_restart_reuses_audio_without_confirming_film(ready, transport):
    repo, grant, approval = ready
    first = n.narrate(repo, KEYS, grant, approval)
    assert len(transport['calls']) == 1
    assert first['state'] == 'succeeded' and first['narration_ready'] and first['narration_approved']
    assert first['calls'][0]['ledger_state'] == 'settled'
    assert (repo.data_dir / first['audio_path']).read_bytes() == wav_bytes()
    restarted = Repository(repo.data_dir)
    try:
        restarted.recover()
        assert n.narrate(restarted, KEYS, grant, approval)['calls'] == first['calls']
    finally:
        restarted.engine.dispose()
    assert len(transport['calls']) == 1
    with repo.factory() as db:
        v = db.get(Version, approval['version_id'])
        assert v.state == 'draft' and v.confirmed_at is None and v.output_path is None
        assert get_job(db, approval).payload['model_profile']['retention_verified'] is False
        assert len(list(db.scalars(select(Version)))) == 1
    output = json.dumps(first, ensure_ascii=False)
    assert TEXT not in output and KEYS['MINIMAX_API_KEY'] not in output


@pytest.mark.parametrize('mode', ['approval', 'version_id', 'text_hash', 'voice', 'grant'])
def test_missing_or_changed_confirmation_rejected(ready, transport, mode):
    repo, grant, original = ready
    approval = deepcopy(original)
    if mode == 'approval': approval['approval'] = 'not confirmed'
    if mode == 'version_id': approval['version_id'] = 'b' * 32
    if mode == 'text_hash': approval['text_sha256'] = '0' * 64
    if mode == 'voice': approval['voice']['MINIMAX_TTS_VOICE'] = 'cloned-voice'
    if mode == 'grant': approval['grant_digest'] = '0' * 64
    with pytest.raises(DomainError): n.narrate(repo, KEYS, grant, approval)
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['text', 'segment', 'revision', 'evidence', 'deleted', 'bytes',
                                  'restricted', 'selected_direction', 'key'])
def test_current_sources_and_exact_version_rechecked_before_send(ready, transport, mode):
    repo, grant, approval = ready
    if mode == 'bytes': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'changed')
    with repo.transaction() as db:
        ep = db.get(Episode, grant['episode_id']); v = db.get(Version, approval['version_id'])
        if mode == 'text': v.narration_text = 'other text'
        if mode == 'segment': v.segments = [{**v.segments[0], 'end': 4.}]
        if mode == 'revision': ep.evidence_revision = ep.evidence_confirmed_revision = 6
        if mode == 'evidence': ep.evidence = [{**row, 'note': 'changed'} for row in ep.evidence]
        if mode == 'deleted': db.get(Asset, grant['assets'][0]['id']).deleted = True
        if mode == 'restricted': v.state = 'restricted'
        if mode == 'selected_direction': ep.selected_direction = 'other'
    with pytest.raises(DomainError): n.narrate(repo, {} if mode == 'key' else KEYS, grant, approval)
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['text', 'output', 'voice'])
def test_adapter_binds_actual_arguments_and_voice(ready, transport, mode):
    repo, grant, approval = ready
    service = n.NarrationServices(repo, KEYS, grant, approval)
    output = repo.data_dir / ('outputs/' + 'c' * 32 + '.wav')
    adapter = n.GrantedSpeech(KEYS['MINIMAX_API_KEY'], approval['text_sha256'], output,
                              service.recheck, budget_check=lambda *a, **kw: True)
    if mode == 'voice': adapter._config['MINIMAX_TTS_SPEED'] = '1.3'
    with pytest.raises(SpeechError):
        asyncio.run(adapter.synthesize('unapproved' if mode == 'text' else TEXT,
                    output.with_name('other.wav') if mode == 'output' else output, reservation='offline'))
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['timeout', 'invalid'])
def test_failed_tts_keeps_cost_and_never_resends(ready, transport, mode):
    repo, grant, approval = ready
    transport['mode'] = mode
    with pytest.raises(DomainError): n.narrate(repo, KEYS, grant, approval)
    repo.recover()
    with pytest.raises(DomainError, match='不会自动重发'): n.narrate(repo, KEYS, grant, approval)
    assert len(transport['calls']) == 1
    with repo.factory() as db:
        call = db.scalars(select(ModelCall)).one()
        ledger = db.get(Budget, call.reservation_id)
        assert ledger.state == ('unknown' if mode == 'timeout' else 'settled')
        assert (ledger.cost is None) == (mode == 'timeout')
        v = db.get(Version, approval['version_id'])
        assert v.state == 'failed' and not v.narration_path


@pytest.mark.parametrize('mode', ['budget', 'pending', 'active'])
def test_budget_pending_cost_and_other_job_stop_before_send(ready, transport, mode):
    repo, grant, approval = ready
    with repo.transaction() as db:
        if mode == 'active':
            db.add(Job(owner_id=grant['owner_id'], episode_id=grant['episode_id'],
                       kind='render', state='running', idempotency_key='other'))
        else:
            db.add(Budget(owner_id=grant['owner_id'], episode_id=grant['episode_id'], amount=3_000_000,
                          cost=3_000_000 if mode == 'budget' else None,
                          state='settled' if mode == 'budget' else 'unknown'))
    with pytest.raises(DomainError): n.narrate(repo, KEYS, grant, approval)
    assert not transport['calls']


def test_paid_audio_checkpoint_recovers_without_resynthesis(ready, transport, monkeypatch):
    repo, grant, approval = ready
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise RuntimeError('simulated interruption before publication')
        patch.setattr(n.NarrationPipeline, 'finish', interrupted)
        with pytest.raises(RuntimeError): n.narrate(repo, KEYS, grant, approval)
    with repo.factory() as db:
        assert 'tts' in get_job(db, approval).payload['steps']
        assert not db.get(Version, approval['version_id']).narration_path
    restarted = Repository(repo.data_dir)
    try:
        restarted.recover()
        assert n.narrate(restarted, KEYS, grant, approval)['narration_ready']
    finally:
        restarted.engine.dispose()
    assert len(transport['calls']) == 1


@pytest.mark.parametrize('mode', ['missing', 'changed', 'symlink'])
def test_paid_audio_missing_or_changed_never_recharges(ready, transport, mode):
    repo, grant, approval = ready
    result = n.narrate(repo, KEYS, grant, approval)
    path = repo.data_dir / result['audio_path']
    if mode == 'missing': path.unlink()
    if mode == 'changed': path.write_bytes(b'changed')
    if mode == 'symlink':
        backup = path.with_suffix('.backup'); path.rename(backup); path.symlink_to(backup)
    with pytest.raises(DomainError, match='停止重发'): n.narrate(repo, KEYS, grant, approval)
    assert len(transport['calls']) == 1


def test_version_changed_during_tts_keeps_paid_audio_unpublished(ready, transport):
    repo, grant, approval = ready
    def change_version():
        with repo.transaction() as db:
            db.get(Version, approval['version_id']).narration_text = 'changed during request'
    transport['during_request'] = change_version
    with pytest.raises(DomainError): n.narrate(repo, KEYS, grant, approval)
    assert len(transport['calls']) == 1
    with repo.factory() as db:
        job = get_job(db, approval)
        assert 'tts' in job.payload['steps'] and job.state == 'failed'
        assert not db.get(Version, approval['version_id']).narration_path
        call = db.scalars(select(ModelCall)).one()
        assert db.get(Budget, call.reservation_id).state == 'settled'
