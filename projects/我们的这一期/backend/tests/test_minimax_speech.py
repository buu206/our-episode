import asyncio
import io
import json
import socket
import wave
import httpx
import pytest
from sqlalchemy import select
from app.models import Episode, Job, Budget, ModelCall
from app.repository import Repository, DomainError
from app.services import minimax_speech as mm
from app.services.pipeline import ModelServices, Pipeline
from app.services.provider_config import adapter, cloud_configuration
from app.services.speech import SpeechError

CONFIG = cloud_configuration({'CLOUD_ENABLED': True, 'MINIMAX_ENABLED': True,
    'MINIMAX_RETENTION_VERIFIED': True, 'MINIMAX_REGION_VERIFIED': True,
    'MINIMAX_API_KEY': 'test-only', 'TTS_PROVIDER': 'minimax'})

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a, **kw): pytest.fail('Tests may not access network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)

def wav_bytes(seconds=1, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b'\0\0' * int(seconds * rate))
    return buf.getvalue()

def envelope():
    audio = wav_bytes()
    return {'base_resp': {'status_code': 0}, 'trace_id': 'mock-trace-123',
        'data': {'status': 2, 'audio': audio.hex()}, 'extra_info': {
            'usage_characters': 4, 'audio_length': 1000, 'audio_size': len(audio),
            'audio_sample_rate': 24000, 'audio_channel': 1, 'audio_format': 'wav'}}

def service(handler, config=None, permit=None):
    return mm.MiniMaxSpeech(config or CONFIG,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        budget_check=permit or (lambda reservation, **kw: True))

def run(s, path, text='你好'):
    return asyncio.run(s.synthesize(text, path, reservation='held'))

@pytest.mark.parametrize('field', ['CLOUD_ENABLED', 'MINIMAX_ENABLED', 'MINIMAX_RETENTION_VERIFIED',
                                  'MINIMAX_REGION_VERIFIED', 'MINIMAX_API_KEY'])
def test_independent_guard_no_fallback(field, tmp_path):
    s = service(lambda _: pytest.fail('sent'), {**CONFIG, field: False, 'MODEL_API_KEY': 'other-vendor'})
    assert not s.ready()[0]
    with pytest.raises(SpeechError): run(s, tmp_path/'v.wav')

@pytest.mark.parametrize('field,value', [('MINIMAX_TTS_MODEL', 'other'),
    ('MINIMAX_TTS_VOICE', 'custom-clone'), ('MINIMAX_TTS_SPEED', 'nan'),
    ('MINIMAX_TTS_SPEED', '0.1'), ('MINIMAX_TTS_PITCH', '1.2'),
    ('MINIMAX_TTS_PITCH', 'inf'), ('MINIMAX_TTS_EMOTION', 'whisper')])
def test_invalid_settings(field, value, tmp_path):
    s = service(lambda _: pytest.fail('sent'), {**CONFIG, field: value})
    assert not s.ready()[0]
    with pytest.raises(SpeechError): run(s, tmp_path/'v.wav')

def test_contract_cost_and_audio(tmp_path):
    calls, permits = [], []
    def handler(request):
        calls.append(request); body = json.loads(request.content)
        assert str(request.url) == mm.ENDPOINT
        assert request.headers['authorization'] == 'Bearer test-only'
        assert body['stream'] is False and body['output_format'] == 'hex'
        assert body['voice_setting'] == {'voice_id': 'male-qn-daxuesheng', 'speed': 1.15,
                                         'vol': 1, 'pitch': 0, 'emotion': 'happy'}
        assert body['audio_setting'] == {'format': 'wav', 'sample_rate': 24000, 'channel': 1}
        return httpx.Response(200, json=envelope())
    def permit(reservation, required):
        permits.append((reservation, required)); return required <= .002
    result = run(service(handler, permit=permit), tmp_path/'v.wav')
    assert len(calls) == 1 and permits == [('held', .0014)] * 2
    assert result['usage']['characters'] == 4 and result['usage']['cost_yuan'] == .0014
    assert result['usage']['provider'] == 'minimax' and result['usage']['retries'] == 0
    assert result['result']['duration'] == 1 and (tmp_path/'v.wav').read_bytes() == wav_bytes()
    assert ModelServices(CONFIG).tts_estimate('a你') == .0014

@pytest.mark.parametrize('status,error', [(1004, 'TTS_AUTH_FAILED'), (1008, 'TTS_BALANCE_REQUIRED')])
def test_safe_provider_error_diagnosis(tmp_path, status, error):
    def handler(request):
        return httpx.Response(200, json={'base_resp': {'status_code': status,
            'status_msg': 'private text and key must not be persisted'}})
    with pytest.raises(SpeechError) as exc:
        run(service(handler), tmp_path/'v.wav')
    assert exc.value.code == error
    assert exc.value.usage['provider_status_code'] == status
    assert exc.value.usage['cost_yuan'] is None
    assert 'private text' not in json.dumps(exc.value.usage) + str(exc.value)
    assert not (tmp_path/'v.wav').exists()

def test_configurable_voice_and_auto_emotion(tmp_path):
    def handler(request):
        v = json.loads(request.content)['voice_setting']
        assert 'emotion' not in v and v['voice_id'] == 'female-yujie' and v['speed'] == .9 and v['pitch'] == -2
        return httpx.Response(200, json=envelope())
    run(service(handler, {**CONFIG, 'MINIMAX_TTS_EMOTION': 'auto', 'MINIMAX_TTS_VOICE': 'female-yujie',
        'MINIMAX_TTS_SPEED': '.9', 'MINIMAX_TTS_PITCH': '-2'}), tmp_path/'v.wav')

@pytest.mark.parametrize('text', ['', ' ', '字'*121, '\ud800', None])
def test_invalid_text(text, tmp_path):
    with pytest.raises(SpeechError): run(service(lambda _: pytest.fail('sent')), tmp_path/'v.wav', text)

def test_budget_revocation_and_client_init(tmp_path):
    permits = iter([True, False])
    with pytest.raises(SpeechError) as error:
        run(service(lambda _: pytest.fail('sent'), permit=lambda *a, **kw: next(permits)), tmp_path/'v.wav')
    assert error.value.usage['cost_yuan'] == 0
    def broken(): raise RuntimeError('private secret')
    s = mm.MiniMaxSpeech(CONFIG, client_factory=broken, budget_check=lambda *a, **kw: True)
    with pytest.raises(SpeechError) as error: run(s, tmp_path/'v.wav')
    assert error.value.code == 'CLIENT_INIT_FAILED' and error.value.usage['cost_yuan'] == 0
    assert 'private' not in str(error.value)

@pytest.mark.parametrize('status', [302, 401, 429, 500])
def test_http_failures_no_redirect_or_retry(status, tmp_path):
    calls = []
    def handler(request):
        calls.append(request); return httpx.Response(status, headers={'location': 'https://untrusted.invalid/audio'})
    with pytest.raises(SpeechError) as error: run(service(handler), tmp_path/'v.wav')
    assert len(calls) == 1 and error.value.usage['cost_yuan'] is None and not (tmp_path/'v.wav').exists()

def test_timeout_unknown_no_retry(tmp_path):
    calls = []
    def handler(request): calls.append(request); raise httpx.ReadTimeout('private response')
    with pytest.raises(SpeechError) as error: run(service(handler), tmp_path/'v.wav')
    assert len(calls) == 1 and error.value.usage['cost_status'] == 'unknown'
    assert 'private' not in str(error.value)

@pytest.mark.parametrize('field,value', [('usage_characters', None), ('usage_characters', True),
    ('usage_characters', 5), ('usage_characters', 0), ('usage_voice_count', 1), ('usage_voice_count', True)])
def test_billing_unknown_stops_audio(field, value, tmp_path):
    data = envelope(); data['extra_info'][field] = value
    with pytest.raises(SpeechError) as error: run(service(lambda _: httpx.Response(200, json=data)), tmp_path/'v.wav')
    assert error.value.code == 'COST_UNKNOWN' and error.value.usage['cost_yuan'] is None
    assert not (tmp_path/'v.wav').exists()

@pytest.mark.parametrize('change', ['trace', 'base', 'duplicate', 'nan', 'oversize',
                                  'audio', 'status', 'duration', 'size', 'format', 'truncated', 'long', 'rate'])
def test_response_validation(change, tmp_path):
    data = envelope()
    if change == 'trace': data['trace_id'] = 'unsafe\ntrace'
    if change == 'base': data['base_resp']['status_code'] = 1008
    if change == 'audio': data['data']['audio'] = 'https://untrusted.invalid/download'
    if change == 'status': data['data']['status'] = 1
    if change == 'duration': data['extra_info']['audio_length'] = 2000
    if change == 'size': data['extra_info']['audio_size'] = 1
    if change == 'format': data['extra_info']['audio_format'] = 'mp3'
    if change == 'truncated': data['data']['audio'] = wav_bytes()[:-2].hex()
    if change == 'long': data['data']['audio'] = wav_bytes(30.1).hex()
    if change == 'rate': data['data']['audio'] = wav_bytes(rate=16000).hex()
    raw = json.dumps(data)
    if change == 'duplicate': raw = '{"data":{},"data":{}}'
    if change == 'nan': raw = '{"x":NaN}'
    if change == 'oversize': raw = 'x' * (mm.MAX_RESPONSE_BYTES+1)
    with pytest.raises(SpeechError) as error: run(service(lambda _: httpx.Response(200, content=raw)), tmp_path/'v.wav')
    assert not (tmp_path/'v.wav').exists()
    if change in ('audio','status','duration','size','format','truncated','long','rate'):
        assert error.value.usage['cost_yuan'] == .0014

def test_no_overwrite_and_failed_writes_removed(tmp_path, monkeypatch):
    p = tmp_path/'exists.wav'; p.write_bytes(b'keep')
    with pytest.raises(SpeechError): run(service(lambda _: pytest.fail('sent')), p)
    alias = tmp_path/'alias.wav'; alias.symlink_to(p)
    with pytest.raises(SpeechError): run(service(lambda _: pytest.fail('sent')), alias)
    assert p.read_bytes() == b'keep'
    def broken(_): raise OSError('disk error')
    monkeypatch.setattr(mm.os, 'fsync', broken)
    with pytest.raises(SpeechError) as error:
        run(service(lambda _: httpx.Response(200, json=envelope())), tmp_path/'new.wav')
    assert not (tmp_path/'new.wav').exists() and error.value.usage['cost_yuan'] == .0014

def make_pipeline(tmp_path):
    repo = Repository(tmp_path/'data')
    with repo.transaction() as db:
        ep = Episode(owner_id='offline', title='test'); db.add(ep); db.flush()
        job = Job(owner_id='offline', episode_id=ep.id, kind='render', state='running',
                  idempotency_key='mm-test', payload={'asset_ids': [], 'revision': 0})
        db.add(job); db.flush(); jid = job.id
    models = ModelServices(CONFIG); models.is_real = False
    models.ready = lambda: (True, 'Other providers isolated in offline test')
    return repo, models, jid

def test_paid_checkpoint_restart_and_voice_change(tmp_path):
    repo, models, jid = make_pipeline(tmp_path); calls = []
    def handler(request): calls.append(request); return httpx.Response(200, json=envelope())
    async def operation(reservation, permit):
        return await service(handler, permit=permit).synthesize('你好', tmp_path/'v.wav', reservation=reservation)
    first = Pipeline(repo, models).invoke(jid, 'tts', .0014, operation)
    restored = Repository(tmp_path/'data')
    assert Pipeline(restored, models).invoke(jid, 'tts', .0014, operation) == first
    assert len(calls) == 1
    with restored.factory() as db:
        ledger = db.scalars(select(Budget)).one(); call = db.scalars(select(ModelCall)).one()
        assert ledger.state == 'settled' and ledger.cost == 1400 and call.usage['provider'] == 'minimax'
    models.config = {**CONFIG, 'MINIMAX_TTS_SPEED': '1.2'}
    with pytest.raises(DomainError) as error: Pipeline(restored, models).invoke(jid, 'tts', .0014, operation)
    assert error.value.code == 'MODEL_CONFIG_CHANGED'

def test_insufficient_reservation_and_unknown_bill_block(tmp_path):
    repo, models, jid = make_pipeline(tmp_path); calls = []
    def handler(request):
        calls.append(request); data = envelope(); data['extra_info'].pop('usage_characters')
        return httpx.Response(200, json=data)
    async def operation(reservation, permit):
        return await service(handler, permit=permit).synthesize('你好', tmp_path/'v.wav', reservation=reservation)
    pipe = Pipeline(repo, models)
    with pytest.raises(DomainError): pipe.invoke(jid, 'tts', .0001, operation)
    assert calls == []
    with pytest.raises(DomainError): pipe.invoke(jid, 'tts', .0014, operation)
    with pytest.raises(DomainError) as error: pipe.invoke(jid, 'tts', .0014, operation)
    assert error.value.code == 'COST_UNKNOWN' and len(calls) == 1

def test_selection_profile_and_inactive_compatibility():
    assert isinstance(adapter(CONFIG, 'tts'), mm.MiniMaxSpeech)
    from app.services.minimax_asr import MiniMaxASR
    assert isinstance(adapter({**CONFIG, 'ASR_PROVIDER': 'minimax'}, 'asr'), MiniMaxASR)
    with pytest.raises(Exception): adapter({**CONFIG, 'VISION_PROVIDER': 'minimax'}, 'vision')
    p = ModelServices(CONFIG).profile()
    assert p['MINIMAX_TTS_SPEED'] == '1.15' and 'MINIMAX_API_KEY' not in p
    assert not any(k.startswith('MINIMAX_') for k in ModelServices({}).profile())
    assert not mm.MiniMaxSpeech(cloud_configuration({})).ready()[0]


def test_no_asr_and_bad_typed_config(tmp_path):
    s = service(lambda _: pytest.fail('sent'))
    with pytest.raises(SpeechError) as error: asyncio.run(s.transcribe([], reservation='r'))
    assert error.value.code == 'MODEL_NOT_ALLOWED'
    for name in ('MINIMAX_TTS_VOICE', 'MINIMAX_TTS_EMOTION'):
        assert not service(lambda _: pytest.fail('sent'), {**CONFIG, name: []}).ready()[0]


def test_offline_preflight_checks_minimax_without_printing_key(tmp_path, capsys):
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]/'scripts'))
    import cloud_preflight
    checks = cloud_preflight.check_configuration(CONFIG, product=tmp_path)
    assert next(c for c in checks if c.name == 'TTS_ADAPTER').status == 'ready'
    checks = cloud_preflight.check_configuration({**CONFIG, 'MINIMAX_API_KEY': ''}, product=tmp_path)
    assert next(c for c in checks if c.name == 'TTS_ADAPTER').status == 'missing'
    cloud_preflight.main(['--profile', 'deepseek-volc-minimax'], env_file=tmp_path/'missing', environ=CONFIG, product=tmp_path)
    assert 'test-only' not in capsys.readouterr().out
