"""Network-free ASR contract, source bounds and charge/recovery failures."""
import asyncio
import json
import socket

import httpx
import pytest
from app.services import minimax_asr as mm
from app.services.speech import AudioChunk, SpeechError
from app.services.pipeline import ModelServices
from app.services.provider_config import adapter, cloud_configuration
from test_minimax_speech import wav_bytes

CONFIG = {'CLOUD_ENABLED': True, 'MINIMAX_ENABLED': True,
          'MINIMAX_RETENTION_VERIFIED': True, 'MINIMAX_REGION_VERIFIED': True,
          'MINIMAX_API_KEY': 'offline-test', 'ASR_PROVIDER': 'minimax'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a, **k): pytest.fail('Real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def chunks(tmp_path):
    p = tmp_path / 'private-filename.wav'
    p.write_bytes(wav_bytes(rate=16000))
    return [AudioChunk(p, 'asset-1', 2, 1)]


def envelope():
    return {'text': '你好！', 'duration': 1, 'trace_id': 'mock-trace', 'n_speakers': 1,
            'segments': [{'id': 0, 'start': .1, 'end': .4, 'text': '你', 'speaker': 'S1'},
                         {'id': 1, 'start': .4, 'end': .9, 'text': '好！', 'speaker': 'S1'}]}


def service(handler, config=None, permit=None):
    return mm.MiniMaxASR(config or CONFIG,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        budget_check=permit or (lambda *a, **kw: True))


def run(s, chunks):
    return asyncio.run(s.transcribe(chunks, reservation='held'))


def test_contract_timing_usage_route(chunks):
    calls, permits = [], []
    def handler(req):
        calls.append(req)
        assert str(req.url) == mm.ENDPOINT
        assert req.headers['authorization'] == 'Bearer offline-test'
        body = req.content
        assert b'private-filename' not in body and b'asset-1' not in body
        for item in (b'verbose_json', b'word', b'asr-1.0', b'false', b'RIFF'):
            assert item in body
        assert b'prompt' not in body and b'expected_text' not in body
        return httpx.Response(200, json=envelope())
    def permit(reservation, required):
        permits.append(required); return required <= .002
    result = run(service(handler, permit=permit), chunks)
    assert len(calls) == 1 and permits == [.00139, .00139]
    assert result['usage']['cost_yuan'] == .000695 and result['usage']['seconds'] == 1
    assert result['usage']['retries'] == 0
    assert result['result'][0]['start'] == 2.1 and result['result'][1]['end'] == 2.9
    assert isinstance(adapter(CONFIG, 'asr'), mm.MiniMaxASR)
    assert ModelServices(CONFIG).asr_estimate(chunks) == .00139
    assert 'MINIMAX_ASR_MODEL' in ModelServices(CONFIG).profile()
    assert 'MINIMAX_ASR_MODEL' not in ModelServices({'TTS_PROVIDER': 'minimax'}).profile()
    assert 'MINIMAX_TTS_MODEL' not in ModelServices(CONFIG).profile()
    assert 'MINIMAX_API_KEY' not in ModelServices(CONFIG).profile()
    assert cloud_configuration({})['MINIMAX_ASR_MODEL'] == mm.MODEL


@pytest.mark.parametrize('field,value', [('CLOUD_ENABLED', False), ('MINIMAX_ENABLED', False),
    ('MINIMAX_RETENTION_VERIFIED', False), ('MINIMAX_REGION_VERIFIED', False),
    ('MINIMAX_API_KEY', ''), ('MINIMAX_API_KEY', 'invalid key'), ('MINIMAX_ASR_MODEL', 'other')])
def test_guard_before_send(chunks, field, value):
    s = service(lambda _: pytest.fail('sent'), {**CONFIG, field: value})
    assert not s.ready()[0]
    with pytest.raises(SpeechError): run(s, chunks)


@pytest.mark.parametrize('mode', ['denied', 'revoked', 'client'])
def test_unsent_failure_cost_zero(chunks, mode):
    permits = iter([True, False] if mode == 'revoked' else [False])
    s = service(lambda _: pytest.fail('sent'), permit=lambda *a, **kw: next(permits))
    if mode == 'client':
        s._budget_check = lambda *a, **kw: True
        def broken(): raise RuntimeError('secret')
        s._client_factory = broken
    with pytest.raises(SpeechError) as e: run(s, chunks)
    assert e.value.usage['cost_yuan'] == 0 and 'secret' not in str(e.value)


@pytest.mark.parametrize('mode', [302, 401, 402, 429, 500, 'timeout', 'duplicate', 'nan', 'oversize'])
def test_failed_request_unknown_never_retries(chunks, mode):
    calls = []
    def handler(req):
        calls.append(req)
        if mode == 'timeout': raise httpx.ReadTimeout('secret')
        if type(mode) is int: return httpx.Response(mode, headers={'location': 'https://untrusted.invalid'})
        content = {'duplicate': '{"text":"a","text":"b"}', 'nan': '{"duration":NaN}',
                   'oversize': 'a' * (mm.MAX_RESPONSE_BYTES + 1)}[mode]
        return httpx.Response(200, content=content)
    with pytest.raises(SpeechError) as e: run(service(handler), chunks)
    assert len(calls) == 1 and e.value.usage['cost_yuan'] is None
    assert 'secret' not in str(e.value)


@pytest.mark.parametrize('field,value', [('duration', None), ('duration', True), ('duration', -1),
    ('duration', float('inf')), ('duration', 2), ('trace_id', 'unsafe\ntrace')])
def test_unknown_billing_stops_result(chunks, field, value):
    data = envelope(); data[field] = value
    with pytest.raises(SpeechError) as e:
        run(service(lambda _: httpx.Response(200, content=json.dumps(data))), chunks)
    assert e.value.usage['cost_yuan'] is None


@pytest.mark.parametrize('mode', ['missing', 'coverage', 'bool', 'negative', 'out-of-range',
    'backwards', 'zero', 'id', 'speaker', 'count', 'text'])
def test_bad_timestamps_charge_known_but_no_result(chunks, mode):
    data = envelope(); seg = data['segments'][1]
    if mode == 'missing': data.pop('segments')
    if mode == 'coverage': data['text'] += '未经对齐'
    if mode == 'bool': seg['start'] = True
    if mode == 'negative': seg['start'] = -1
    if mode == 'out-of-range': seg['end'] = 1.01
    if mode == 'backwards': seg['start'] = .05
    if mode == 'zero': seg['end'] = seg['start']
    if mode == 'id': seg['id'] = 99
    if mode == 'speaker': seg['speaker'] = 'Alice'
    if mode == 'count': data['n_speakers'] = 2
    if mode == 'text': seg['text'] = ''
    with pytest.raises(SpeechError) as e: run(service(lambda _: httpx.Response(200, json=data)), chunks)
    assert e.value.usage['cost_yuan'] == .000695


def test_silent_input_and_overlapping_speakers(chunks):
    silent = {'text': '', 'segments': [], 'n_speakers': 0}
    assert mm.parse_transcription(silent, chunks[0]) == []
    data = envelope(); data['segments'][1].update(speaker='S2', start=.2)
    data['n_speakers'] = 2
    rows = mm.parse_transcription(data, chunks[0])
    assert rows[0]['speaker_scope'] == rows[1]['speaker_scope'] == 'asset-1:2'
    assert rows[0]['end'] > rows[1]['start']  # Real overlaps are not invented away.


@pytest.mark.parametrize('missing', [False, True])
def test_actual_no_speech_null_segments_is_empty_not_a_failure(chunks, missing):
    data = {**envelope(), 'text': '', 'segments': None, 'n_speakers': 0}
    if missing:
        data.pop('segments')
    result = run(service(lambda _: httpx.Response(200, json=data)), chunks)
    assert result['result'] == []
    assert result['usage']['cost_yuan'] == .000695


@pytest.mark.parametrize('text,speakers', [('有原话', 0), ('', 1), ('', False), ('', None)])
def test_null_segments_never_hides_unaligned_text_or_invalid_speakers(chunks, text, speakers):
    with pytest.raises(SpeechError):
        mm.parse_transcription({'text': text, 'segments': None, 'n_speakers': speakers}, chunks[0])


@pytest.mark.parametrize('second', ['success', 'failure', 'revoked'])
def test_multichunk_costs_and_partial_failure(chunks, second):
    chunks.append(AudioChunk(chunks[0].path, 'asset-1', 3, 1))
    calls, permissions = [], []
    def handler(req):
        calls.append(req)
        if len(calls) == 2 and second == 'failure': raise httpx.ReadTimeout('private')
        return httpx.Response(200, json=envelope())
    def permit(*a, **kw):
        permissions.append(1)
        return not (len(permissions) == 3 and second == 'revoked')
    if second == 'success':
        result = run(service(handler, permit=permit), chunks)
        assert result['usage']['cost_yuan'] == .00139
        assert result['result'][2]['start'] == 3.1
        assert result['result'][2]['speaker_scope'] != result['result'][0]['speaker_scope']
    else:
        with pytest.raises(SpeechError) as e: run(service(handler, permit=permit), chunks)
        assert len(calls) == (1 if second == 'revoked' else 2)
        assert e.value.usage['cost_yuan'] == (.000695 if second == 'revoked' else None)
        assert e.value.usage['requests'][0]['cost_yuan'] == .000695
