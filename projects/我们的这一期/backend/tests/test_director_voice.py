"""Offline voice input: authentic media, durable consent, bounded paid work.

Synthesised silence and labelled text doubles never assert model quality.
"""
import asyncio
import subprocess
import time
import wave

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from app.chat_models import DirectorChatTurn
from app.main import create_app
from app.models import Audit, Budget, Episode, Job, ModelCall, Version
from app.retention_models import Retention
from app.services import director_voice as voice, media, retention
from app.services.pipeline import ModelServices
from app.services.speech import AudioChunk, SpeechError
from test_pipeline import FakeServices, settings, no_network, authenticate, request, advance, voiced_video_bytes
from test_minimax_speech import wav_bytes


class VoiceServices(FakeServices):
    costs = {**FakeServices.costs, 'director_voice': .000695}

    def __init__(self):
        super().__init__()
        self.text = '离线测试占位，不是真实转写。'

    def voice_ready(self):
        return True, 'Explicit offline test voice.'

    async def director_voice(self, chunks, reservation, permit, context):
        self._permission('director_voice', reservation, permit)
        return {'result': ([{'asset_id': context['job_id'], 'start': 0, 'end': 1, 'text': self.text}]
                           if self.text else []), 'usage': self._usage('director_voice')}


@pytest.fixture
def services():
    return VoiceServices()


@pytest.fixture
def client(settings, services):
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as connection:
        yield connection


def episode(client):
    auth = authenticate(client)
    value = request(client, '/episodes', auth, expected=201, body={'title': '合成录音技术测试'})
    return auth, value['id']


def path(eid, suffix=''):
    return f'/api/v1/episodes/{eid}/director/voice' + suffix


def upload(client, auth, eid, *, audio=None, name='voice.wav', key='voice-client-0001', expected=201, **extra):
    data = {'client_id': key, 'confirm_send': 'true', 'accept_unknown_retention': 'true', **extra}
    response = client.post(path(eid), headers=auth, data=data,
        files={'file': (name, wav_bytes() if audio is None else audio, 'application/octet-stream')})
    assert response.status_code == expected, response.text
    return response.json()


def stored_source(client, job_id):
    with client.app.state.repo.factory() as db:
        return client.app.state.repo.data_dir / db.get(Job, job_id).payload['voice_path']


def test_voice_upload_job_text_recovery_idempotency_and_no_actions(client, services, settings):
    auth, eid = episode(client)
    status = client.get(path(eid, '/status')).json()
    assert status == {'can_record': True, 'reason': 'Explicit offline test voice.', 'provider': 'MiniMax',
        'model': 'asr-1.0', 'mode': 'demo', 'max_seconds': 60, 'max_bytes': 12 * 1024 * 1024}
    queued = upload(client, auth, eid, name='../../private-original.wav')
    assert queued['state'] == 'queued' and queued['text'] is None and queued['mode'] == 'demo'
    assert queued['job_id'] == queued['id'] and not services.counts
    assert upload(client, auth, eid)['id'] == queued['id']
    assert upload(client, auth, eid, audio=wav_bytes(seconds=.5), expected=409)['error']['code'] == 'VOICE_REQUEST_CONFLICT'
    assert upload(client, auth, eid, key='voice-client-0002', expected=409)['error']['code'] == 'VOICE_BUSY'
    source = stored_source(client, queued['id'])
    with wave.open(str(source), 'rb') as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert not list((client.app.state.repo.data_dir / 'incoming').iterdir())
    services.ready = lambda: (False, 'Unrelated cloud permission remains closed.')
    advance(client, queued['id'])
    result = client.get(path(eid, '/' + queued['id'])).json()
    assert result['state'] == 'succeeded' and result['text'] == services.text
    assert client.get(path(eid, '/by-client/voice-client-0001')).json() == result
    assert client.get(path(eid)).json() == {'items': [result]}
    assert upload(client, auth, eid) == result
    assert services.counts['director_voice'] == 1
    with client.app.state.repo.factory() as db:
        job = db.get(Job, queued['id'])
        assert job.payload['voice_consent']['owner_id'] == 'owner'
        assert job.payload['voice_consent']['voice_sha256'] == job.payload['voice_sha256']
        assert not list(db.scalars(select(DirectorChatTurn))) and not list(db.scalars(select(Version)))
        assert db.scalars(select(ModelCall)).one().kind == 'director_voice'
        assert db.scalars(select(Budget)).one().cost == 695
        assert all(services.text not in str(a.details) for a in db.scalars(select(Audit)))
    assert 'outputs/' not in str(result) and 'voice_consent' not in str(result)
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as restarted:
        authenticate(restarted)
        assert restarted.get(path(eid, '/by-client/voice-client-0001')).json() == result


@pytest.mark.parametrize('field', ['confirm_send', 'accept_unknown_retention'])
def test_each_recording_requires_both_permissions(client, field):
    auth, eid = episode(client)
    error = upload(client, auth, eid, expected=409, **{field: 'false'})
    assert error['error']['code'] == 'VOICE_CONSENT_REQUIRED'
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(Job)))


def test_login_csrf_owner_and_expiry(client):
    assert client.get(path('a' * 32, '/status')).status_code == 401
    auth, eid = episode(client)
    assert upload(client, {}, eid, expected=403)['error']['code'] == 'CSRF_FAILED'
    with client.app.state.repo.transaction() as db:
        other = Episode(owner_id='other', title='他人节目')
        db.add(other); db.flush(); other_id = other.id
    assert upload(client, auth, other_id, expected=404)['error']['code'] == 'NOT_FOUND'
    for suffix in ('', '/status', '/by-client/voice-client-0001', '/' + 'f' * 32):
        assert client.get(path(other_id, suffix)).status_code == 404
    record = upload(client, auth, eid)
    assert client.get(path(other_id, '/' + record['id'])).status_code == 404
    with client.app.state.repo.transaction() as db:
        db.get(Retention, eid).expires_at = time.time() - 1
    assert client.get(path(eid, '/' + record['id'])).status_code == 410


@pytest.mark.parametrize('audio,name,error', [(b'', 'voice.wav', 'VOICE_INVALID_FILE'),
    (b'not an audio file', 'voice.wav', 'VOICE_INVALID_FILE'),
    (b'<html>attack</html>', 'voice.html', 'VOICE_INVALID_FILE'),
    (wav_bytes(seconds=61), 'voice.wav', 'VOICE_DURATION_EXCEEDED'),
    (b'x' * (12 * 1024 * 1024 + 1), 'voice.wav', 'VOICE_TOO_LARGE')])
def test_reject_bad_and_oversized_inputs_without_model(client, services, audio, name, error):
    auth, eid = episode(client)
    response = upload(client, auth, eid, audio=audio, name=name, expected=413 if error == 'VOICE_TOO_LARGE' else 422)
    assert response['error']['code'] == error
    assert not services.counts
    assert not list((client.app.state.repo.data_dir / 'outputs').iterdir())
    assert not list((client.app.state.repo.data_dir / 'incoming').iterdir())
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(ModelCall)))


def test_video_in_mp4_container_is_rejected(client, voiced_video_bytes):
    auth, eid = episode(client)
    assert upload(client, auth, eid, audio=voiced_video_bytes, name='voice.mp4', expected=422)['error']['code'] == 'VOICE_INVALID_FILE'


@pytest.mark.parametrize('suffix,codec', [('.webm', 'libopus'), ('.m4a', 'aac')])
def test_browser_audio_formats_are_decoded_without_crop(client, tmp_path, suffix, codec):
    auth, eid = episode(client)
    source, target = tmp_path / 'source.wav', tmp_path / ('browser' + suffix)
    source.write_bytes(wav_bytes())
    subprocess.run([media._binary('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(source),
        '-c:a', codec, str(target)], check=True, capture_output=True, timeout=20)
    record = upload(client, auth, eid, audio=target.read_bytes(), name='recording' + suffix)
    with wave.open(str(stored_source(client, record['id'])), 'rb') as w:
        assert .95 < w.getnframes() / w.getframerate() < 1.05


@pytest.mark.parametrize('mutation', ['file', 'consent', 'owner', 'episode', 'client', 'digest'])
def test_recording_binding_rechecked_before_send(client, services, mutation):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    if mutation == 'file':
        stored_source(client, record['id']).write_bytes(wav_bytes(seconds=.5, rate=16000))
    else:
        with client.app.state.repo.transaction() as db:
            job = db.get(Job, record['id'])
            consent = dict(job.payload['voice_consent'])
            consent[{'consent': 'confirm_send', 'owner': 'owner_id', 'episode': 'episode_id',
                     'client': 'client_id', 'digest': 'voice_sha256'}[mutation]] = False
            job.payload = {**job.payload, 'voice_consent': consent}
    advance(client, record['id'], 'failed')
    assert not services.counts
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(ModelCall)))


def test_no_speech_is_explicit_and_paid_result_not_repeated(client, services):
    auth, eid = episode(client)
    services.text = ''
    record = upload(client, auth, eid)
    assert advance(client, record['id'], 'failed')['error']['code'] == 'VOICE_NO_SPEECH'
    assert client.get(path(eid, '/' + record['id'])).json()['text'] is None
    request(client, '/jobs/' + record['id'] + '/retry', auth)
    advance(client, record['id'], 'failed')
    assert services.counts['director_voice'] == 1


def test_timeout_unknown_cost_blocks_retry_and_later_spending(client, services):
    auth, eid = episode(client)
    services.failures['director_voice'] = ['unknown']
    record = upload(client, auth, eid)
    assert advance(client, record['id'], 'failed')['error']['code'] == 'MODEL_TIMEOUT'
    assert request(client, '/jobs/' + record['id'] + '/retry', auth, expected=409)['error']['code'] == 'COST_UNKNOWN'
    next_record = upload(client, auth, eid, key='voice-client-0002')
    assert advance(client, next_record['id'], 'failed')['error']['code'] == 'COST_UNKNOWN'
    assert services.counts['director_voice'] == 1
    with client.app.state.repo.factory() as db:
        assert db.scalars(select(Budget)).one().state == 'unknown'


@pytest.mark.parametrize('scope', ['episode', 'total'])
def test_voice_respects_three_and_thirty_yuan_caps(client, services, scope):
    auth, eid = episode(client)
    with client.app.state.repo.transaction() as db:
        db.add(Budget(owner_id='owner', episode_id=eid if scope == 'episode' else 'other-episode',
            amount=3_000_000 if scope == 'episode' else 30_000_000,
            cost=3_000_000 if scope == 'episode' else 30_000_000, state='settled'))
    record = upload(client, auth, eid)
    assert advance(client, record['id'], 'failed')['error']['code'] == 'BUDGET_EXCEEDED'
    assert not services.counts
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(ModelCall)))


@pytest.mark.parametrize('seconds,expected', [(59, 201), (60, 201), (60.05, 422)])
def test_streaming_webm_without_duration_has_full_decoded_length_checked(client, tmp_path, seconds, expected):
    auth, eid = episode(client)
    source, target = tmp_path / 'source.wav', tmp_path / 'stream.webm'
    source.write_bytes(wav_bytes(seconds=seconds, rate=16000))
    subprocess.run([media._binary('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(source),
        '-c:a', 'libopus', '-live', '1', str(target)], check=True, capture_output=True, timeout=20)
    record = upload(client, auth, eid, audio=target.read_bytes(), name='recording.webm', expected=expected)
    if expected == 201:
        with wave.open(str(stored_source(client, record['id'])), 'rb') as w:
            assert w.getnframes() / w.getframerate() == seconds
    else:
        assert record['error']['code'] == 'VOICE_DURATION_EXCEEDED'


def test_deletion_removes_application_recording_and_private_text(client, services):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    source = stored_source(client, record['id'])
    advance(client, record['id'])
    request(client, f'/episodes/{eid}?confirm=true', auth, method='DELETE')
    assert not source.exists()
    with client.app.state.repo.factory() as db:
        job = db.get(Job, record['id'])
        assert job.payload == {} and job.result is None
        assert db.scalars(select(Budget)).one().cost == 695


def test_expiry_revokes_queued_audio_before_sending_and_cleans_it(client, services):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    source = stored_source(client, record['id'])
    with client.app.state.repo.transaction() as db:
        db.get(Retention, eid).expires_at = time.time() - 1
    retention.maintain(client.app.state.repo)
    assert not source.exists() and not services.counts
    assert client.get(path(eid)).status_code == 410


def test_interrupted_running_voice_is_persisted_not_automatically_resent(client, services):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    with client.app.state.repo.transaction() as db:
        db.get(Job, record['id']).state = 'running'
    client.app.state.repo.recover()
    failed = client.get(path(eid, '/' + record['id'])).json()
    assert failed['state'] == 'failed' and failed['error']['code'] == 'INTERRUPTED'
    assert stored_source(client, record['id']).exists() and not services.counts


def test_real_adapter_scoped_consent_uses_existing_asr_contract_without_global_flags(client, monkeypatch, tmp_path):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    chunks = voice.chunks_for(stored_source(client, record['id']), tmp_path, record['id'])
    seen = []
    async def send(self, request, *args, **kwargs):
        seen.append(request)
        return httpx.Response(200, request=request, json={'text': '测试', 'duration': 1,
            'trace_id': 'offline-trace', 'n_speakers': 1, 'segments': [
                {'id': 0, 'start': 0, 'end': .8, 'text': '测试', 'speaker': 'S1'}]})
    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    config = {'MINIMAX_API_KEY': 'offline-only', 'CLOUD_ENABLED': False,
        'MINIMAX_ENABLED': False, 'MINIMAX_RETENTION_VERIFIED': False}
    adapter = voice.ConsentedMiniMaxASR(config, client.app.state.repo, record['id'], lambda *a, **kw: True)
    result = asyncio.run(adapter.transcribe(chunks, reservation='test-held'))
    assert result['result'][0]['text'] == '测试' and len(seen) == 1
    assert seen[0].url.host == 'api.minimax.cn' and seen[0].url.path == '/v1/speech_to_text'
    assert config['MINIMAX_RETENTION_VERIFIED'] is False
    assert ModelServices(config).voice_ready()[0]
    assert not ModelServices({}).voice_ready()[0]


@pytest.mark.parametrize('mutation', ['different_pcm', 'other_job', 'offset', 'shorter'])
def test_scoped_adapter_cannot_borrow_consent_for_other_audio(client, tmp_path, mutation):
    auth, eid = episode(client)
    record = upload(client, auth, eid)
    chunks = voice.chunks_for(stored_source(client, record['id']), tmp_path, record['id'])
    if mutation == 'different_pcm':
        with wave.open(str(chunks[0].path), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x01\x00' * 16000)
    elif mutation == 'shorter':
        chunks[0].path.write_bytes(wav_bytes(seconds=.5, rate=16000))
        chunks = [AudioChunk(chunks[0].path, record['id'], 0, .5)]
    else:
        chunks = [AudioChunk(chunks[0].path, 'other' if mutation == 'other_job' else record['id'],
                             1 if mutation == 'offset' else 0, 1)]
    adapter = voice.ConsentedMiniMaxASR({'MINIMAX_API_KEY': 'offline-only'}, client.app.state.repo,
                                      record['id'], lambda *a, **kw: pytest.fail('budget used for unbound audio'))
    with pytest.raises(SpeechError) as error:
        asyncio.run(adapter.transcribe(chunks, reservation='test-held'))
    assert error.value.code == 'VOICE_SOURCE_CHANGED'


def test_total_multipart_body_limit_precedes_parsing(client):
    auth, eid = episode(client)
    response = client.post(path(eid), headers={**auth, 'Content-Length': str(13 * 1024 * 1024 + 1)}, content=b'')
    assert response.status_code == 413 and response.json()['error']['code'] == 'REQUEST_TOO_LARGE'
