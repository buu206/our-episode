"""Supplier protocol fixtures only: no model calls, real media or credentials sent."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import socket
import struct
from types import SimpleNamespace
import wave
from zoneinfo import ZoneInfo

import httpx
from PIL import Image
import pytest

from app.services import deepseek as ds, volc_speech as vs
from app.services.director import DirectorError
from app.services.pipeline import ModelServices
from app.services.provider_config import adapter, cloud_configuration
from app.services.speech import AudioChunk, SpeechError
from app.services.vision import PreparedVideo, FrameSample, _coverage

CONFIG = cloud_configuration({
    'CLOUD_ENABLED': True, 'DEEPSEEK_ENABLED': True, 'DEEPSEEK_RETENTION_VERIFIED': True,
    'DEEPSEEK_REGION_VERIFIED': True, 'DEEPSEEK_API_KEY': 'test-only',
    'VOLC_ENABLED': True, 'VOLC_RETENTION_VERIFIED': True, 'VOLC_REGION_VERIFIED': True,
    'VOLC_API_KEY': 'test-only', 'VISION_PROVIDER': 'deepseek', 'DIRECTOR_PROVIDER': 'deepseek',
    'ASR_PROVIDER': 'volc', 'TTS_PROVIDER': 'volc'})
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ds.time, 'time', lambda: NOW)
    def no_network(*args, **kwargs):
        raise AssertionError('offline tests must not open network connections')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)


def packet():
    aid = 'a' * 32
    coverage = _coverage(aid, 1)
    out = io.BytesIO()
    Image.new('RGB', (512, 512), 'red').save(out, format='JPEG')
    return PreparedVideo((FrameSample(aid, .5, aid + '_f001', out.getvalue()),), (coverage,))


def envelope(content, **changes):
    return {'id': 'provider-response-1', 'model': 'deepseek-flash', 'created': int(NOW),
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                      'prompt_cache_hit_tokens': 20, 'prompt_cache_miss_tokens': 80}, **changes}


def client(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize('key', ['CLOUD_ENABLED', 'DEEPSEEK_ENABLED', 'DEEPSEEK_RETENTION_VERIFIED',
                                 'DEEPSEEK_REGION_VERIFIED', 'DEEPSEEK_API_KEY'])
def test_deepseek_admission_before_clients(key):
    cfg = {**CONFIG, key: False}
    service = ds.DeepSeekDirector(cfg, client_factory=lambda: pytest.fail('created'), budget_check=lambda _: True)
    assert not service.ready()[0]
    with pytest.raises(DirectorError):
        run(service.propose([{'asset_id': 'a', 'start': 0., 'end': 1., 'note': 'test'}], {'a': 1.}, reservation='r'))


@pytest.mark.parametrize('key', ['CLOUD_ENABLED', 'VOLC_ENABLED', 'VOLC_RETENTION_VERIFIED', 'VOLC_REGION_VERIFIED', 'VOLC_API_KEY'])
def test_volc_independent_admission(key):
    cfg = {**CONFIG, key: False, 'CLOUD_RETENTION_VERIFIED': True, 'CLOUD_REGION_VERIFIED': True}
    assert not vs.VolcSpeech(cfg).ready()[0]
    assert not ModelServices(cfg).ready()[0]


def test_selection_no_silent_fallback_or_key_sharing():
    assert ModelServices(CONFIG).ready()[0]
    assert isinstance(adapter(CONFIG, 'vision'), ds.DeepSeekVision)
    assert isinstance(adapter(CONFIG, 'tts'), vs.VolcSpeech)
    assert not ModelServices({**CONFIG, 'ASR_PROVIDER': 'unknown'}).ready()[0]
    assert not ModelServices({**CONFIG, 'DEEPSEEK_API_KEY': '', 'MODEL_API_KEY': 'legacy'}).ready()[0]
    assert not ModelServices({**CONFIG, 'DEEPSEEK_BASE_URL': 'https://localhost'}).ready()[0]
    assert not ModelServices({**CONFIG, 'VOLC_TTS_SPEAKER': 'cloned-voice'}).ready()[0]
    assert 'DEEPSEEK_API_KEY' not in ModelServices(CONFIG).profile()


def test_deepseek_contract_and_grounding():
    calls = []
    value = {'directions': [{'id': 'd1', 'title': '开场', 'summary': '真实来源',
        'evidence': [{'asset_id': 'a', 'start': 0., 'end': 1., 'note': '已核对'}],
        'edit_plan': {'segments': [{'asset_id': 'a', 'start': 0., 'end': 1., 'caption': '开场'}], 'narration_text': ''}}],
        'uncertainties': []}
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body['thinking'] == {'type': 'disabled'}
        assert body['response_format'] == {'type': 'json_object'}
        assert request.url.host == 'api.deepseek.com'
        return httpx.Response(200, json=envelope(value))
    service = ds.DeepSeekDirector(CONFIG, client_factory=client(handler), budget_check=lambda _: True)
    evidence = [{'asset_id': 'a', 'start': 0., 'end': 1., 'note': '已核对'}]
    result = run(service.propose(evidence, {'a': 1.}, reservation='r'))
    assert result['usage']['cost_yuan'] == pytest.approx((20*.04+80*2+10*8)/1e6)
    assert result['usage']['model_version'] == 'deepseek-flash'
    value['directions'][0]['evidence'][0]['end'] = 2.
    with pytest.raises(DirectorError) as error:
        run(service.propose(evidence, {'a': 2.}, reservation='r'))
    assert error.value.code == 'UNGROUNDED_EVIDENCE'
    assert error.value.usage['cost_status'] == 'known'
    assert len(calls) == 2


@pytest.mark.parametrize('response', [httpx.Response(302, headers={'Location': 'https://example.com'}),
                                    httpx.Response(429), httpx.Response(200, json={}),
                                    httpx.Response(200, content=b'x'*200001)])
def test_deepseek_failure_no_retry(response):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    service = ds.DeepSeekDirector(CONFIG, client_factory=client(handler), budget_check=lambda _: True)
    with pytest.raises(DirectorError) as error:
        run(service.request([{'role': 'user', 'content': 'json'}], 'r'))
    assert len(calls) == 1
    assert error.value.usage['cost_yuan'] is None


def test_cost_missing_cache_and_cross_band_are_unknown():
    base = envelope({})
    assert ds.response_usage(base, NOW, NOW+1, 1000, 100)['cost_status'] == 'known'
    del base['usage']['prompt_cache_hit_tokens']
    assert ds.response_usage(base, NOW, NOW+1, 1000, 100)['cost_yuan'] is None
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    assert ds.response_usage(envelope({}), noon-1, noon+1, 1000, 100)['cost_yuan'] is None
    assert ds.price_band(datetime(2027, 1, 1, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()) is None


def test_vision_manifest_and_partial_unknown_cost():
    prepared = packet()
    f = prepared.frames[0]
    value = {'observations': [{'asset_id': f.asset_id, 'frame_id': f.frame_id, 'time': f.time, 'note': '红色画面'}], 'uncertainties': []}
    permissions = []
    def permit(*args):
        permissions.append(args)
        return True
    def handler(request):
        assert len(json.loads(request.content)['messages'][0]['content']) == 3
        return httpx.Response(200, json=envelope(value))
    service = ds.DeepSeekVision(CONFIG, client_factory=client(handler), budget_check=permit)
    result = run(service.analyze(prepared, reservation='r'))
    assert ('r', .1, prepared.request_digest) in permissions
    assert result['result'].coverage[0].audio_analyzed is False
    assert result['usage']['requests'][0]['provider'] == 'deepseek'


@pytest.fixture
def chunk(tmp_path):
    path = tmp_path/'in.wav'
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(b'\0\0'*16000)
    return AudioChunk(path, 'a', 30., 1.)


def asr_result():
    return {'audio_info': {'duration': 1000}, 'result': {'text': '你好再见', 'utterances': [
        {'start_time': 0, 'end_time': 400, 'text': '你好', 'definite': True,
         'words': [{'start_time': 0, 'end_time': 200, 'text': '你'}, {'start_time': 200, 'end_time': 400, 'text': '好'}]},
        {'start_time': 500, 'end_time': 1000, 'text': '再见', 'definite': True,
         'words': [{'start_time': 500, 'end_time': 900, 'text': '再'}, {'start_time': 1000, 'end_time': 1000, 'text': '见'}]}]}}


def server_packet(value, final=True):
    payload = json.dumps(value).encode()
    return bytes((0x11, 0x92 if final else 0x90, 0x10, 0)) + struct.pack('>I', len(payload)) + payload


class FakeSocket:
    def __init__(self, final, intermediate=None):
        self.final, self.intermediate = final, intermediate
        self.response = SimpleNamespace(headers={'x-tt-logid': 'volc-response-1'})
        self.sent = []
        self.done = asyncio.Event()
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def send(self, value):
        self.sent.append(value)
        if value[1] & 2: self.done.set()
    async def recv(self):
        await self.done.wait()
        if self.intermediate:
            value, self.intermediate = self.intermediate, None
            return server_packet(value, final=False)
        return server_packet(self.final)


async def no_sleep(_):
    await asyncio.sleep(0)


def test_asr_stream_multisentence_offsets_and_zero_duration(chunk):
    sockets = []
    def factory(url, **kwargs):
        assert url == vs.ASR_URL and kwargs['proxy'] is None
        assert kwargs['additional_headers']['X-Api-Resource-Id'] == vs.ASR_MODEL
        sock = FakeSocket(asr_result()); sockets.append(sock); return sock
    service = vs.VolcSpeech(CONFIG, ws_factory=factory, budget_check=lambda _: True, sleep=no_sleep)
    result = run(service.transcribe([chunk], reservation='r'))
    assert ''.join(w['text'] for w in result['result']) == '你好再见'
    assert result['result'][0]['start'] == 30
    assert result['result'][-1]['text'] == '再见'
    assert result['usage']['cost_yuan'] == pytest.approx(1/3600, abs=1e-6)
    assert len(sockets[0].sent) == 6


@pytest.mark.parametrize('change', ['missing_sentence', 'missing_word', 'unfinal', 'bad_time'])
def test_asr_rejects_incomplete_text(chunk, change):
    value = asr_result()
    if change == 'missing_sentence': value['result']['utterances'].pop()
    elif change == 'missing_word': value['result']['utterances'][0]['words'].pop()
    elif change == 'unfinal': value['result']['utterances'][0]['definite'] = False
    else: value['result']['utterances'][0]['words'][0]['end_time'] = 3000
    with pytest.raises(SpeechError): vs.parse_final(value, chunk)


def test_asr_dropped_previously_final_sentence_is_rejected(chunk):
    early = asr_result()
    final = deepcopy(early)
    final['result']['utterances'] = final['result']['utterances'][1:]
    final['result']['text'] = '再见'
    service = vs.VolcSpeech(CONFIG, ws_factory=lambda *a, **kw: FakeSocket(final, early),
                            budget_check=lambda _: True, sleep=no_sleep)
    with pytest.raises(SpeechError) as error:
        run(service.transcribe([chunk], reservation='r'))
    assert error.value.code == 'INCOMPLETE_ASR'
    assert error.value.usage['cost_yuan'] is None


def test_asr_unknown_duration_stops_before_next_chunk(chunk):
    calls=[]
    value=asr_result(); value.pop('audio_info')
    def factory(*a, **kw): calls.append(1); return FakeSocket(value)
    service=vs.VolcSpeech(CONFIG, ws_factory=factory, budget_check=lambda _: True, sleep=no_sleep)
    from dataclasses import replace
    with pytest.raises(SpeechError) as error:
        run(service.transcribe([chunk,replace(chunk,offset=31.)],reservation='r'))
    assert len(calls)==1 and error.value.usage['cost_yuan'] is None


def test_protocol_and_redirect_rejection():
    for bad in (b'', b'\x11\x92\x10\0'+struct.pack('>I',300)+b'{}', server_packet({})+b'junk'):
        with pytest.raises(SpeechError): vs.decode_packet(bad)
    obj=object.__new__(vs.FixedEndpointConnect)
    error=RuntimeError('redirect')
    assert obj.process_redirect(error) is error


def tts_stream(final=True, units=2):
    event={'code':0,'data':base64.b64encode(b'\0\0'*24000).decode()}
    result='data: '+json.dumps(event)+'\n\n'
    if final: result+='event: 152\ndata: '+json.dumps({'code':20000000,'usage':{'text_words':units}})+'\n\n'
    return result


@pytest.mark.parametrize('mode',['success','missing_final','missing_usage','overcharge','http_error'])
def test_tts_terminal_usage_and_exclusive_wav(tmp_path,mode):
    requests=[]
    def handler(request):
        requests.append(request)
        body=json.loads(request.content)
        assert body['req_params']['speaker']==vs.SPEAKER
        assert request.headers['x-control-require-usage-tokens-return']=='text_words'
        return httpx.Response(500 if mode=='http_error' else 200,
            headers={'x-tt-logid':'tts-log-1'},text=tts_stream(mode!='missing_final',None if mode=='missing_usage' else 100 if mode=='overcharge' else 2))
    target=tmp_path/'out.wav'
    service=vs.VolcSpeech(CONFIG,capability='tts',client_factory=client(handler),budget_check=lambda _:True)
    if mode in ('missing_final','http_error'):
        with pytest.raises(SpeechError): run(service.synthesize('你好',target,reservation='r'))
        assert not target.exists()
    else:
        result=run(service.synthesize('你好',target,reservation='r'))
        assert result['result']['duration']==1
        assert result['usage']['cost_status']==('known' if mode=='success' else 'unknown')
        if mode=='success': assert result['usage']['cost_yuan']==.0006
        with pytest.raises(SpeechError): run(service.synthesize('你好',target,reservation='r'))
    assert len(requests)==1


def test_preflight_profile_is_offline_and_does_not_write(tmp_path, capsys):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    import cloud_preflight as preflight
    env=tmp_path/'.env'
    env.write_text('CLOUD_ENABLED=false\n')
    before=env.read_bytes()
    assert preflight.main(['--profile','deepseek-volc'],env_file=env,environ={},product=tmp_path)==2
    assert env.read_bytes()==before
    assert 'VISION_ADAPTER' in capsys.readouterr().out
    checks=preflight.check_configuration(CONFIG,product=tmp_path)
    assert all(c.status in ('ready','not_checked') for c in checks)


def test_pipeline_provider_profile_survives_restart_and_blocks_switch(tmp_path):
    from app.repository import Repository, DomainError
    from app.models import Episode, Job, Budget, ModelCall
    from app.services.pipeline import Pipeline
    from sqlalchemy import select
    repo=Repository(tmp_path/'data')
    with repo.transaction() as db:
        ep=Episode(owner_id='test',title='offline');db.add(ep);db.flush()
        job=Job(owner_id='test',episode_id=ep.id,kind='analysis',state='running',
                idempotency_key='test',payload={'asset_ids':[],'revision':0})
        db.add(job);db.flush();jid=job.id
    services=ModelServices(dict(CONFIG)); services.is_real=False
    pipeline=Pipeline(repo,services)
    sent=[]
    async def operation(reservation,permit):
        assert permit(reservation)
        sent.append(1)
        return {'result': {'safe':'checkpoint'},'usage': {**ds.empty_usage('known'),'cost_yuan':.001}}
    pipeline.invoke(jid,'vision',.1,operation)
    restored=Repository(tmp_path/'data')
    services.config['VISION_PROVIDER']='bailian'
    # Admission is separately covered; make it ready to isolate persisted profile protection.
    services.ready=lambda:(True,'offline')
    with pytest.raises(DomainError) as error:
        Pipeline(restored,services).invoke(jid,'vision',.1,operation)
    assert error.value.code=='MODEL_CONFIG_CHANGED' and len(sent)==1
    with restored.factory() as db:
        assert len(list(db.scalars(select(Budget))))==1
        assert len(list(db.scalars(select(ModelCall))))==1


def test_unknown_real_adapter_usage_holds_budget_and_blocks_next_step(tmp_path):
    from app.repository import Repository, DomainError
    from app.models import Episode, Job, Budget
    from app.services.pipeline import Pipeline
    from sqlalchemy import select
    repo=Repository(tmp_path/'data')
    with repo.transaction() as db:
        ep=Episode(owner_id='test',title='offline');db.add(ep);db.flush()
        job=Job(owner_id='test',episode_id=ep.id,kind='directions',state='running',
                idempotency_key='test',payload={'asset_ids':[],'revision':0})
        db.add(job);db.flush();jid=job.id
    def handler(request):
        value=envelope({'directions':[],'uncertainties':['缺少证据']})
        value['usage'].pop('prompt_cache_hit_tokens')
        return httpx.Response(200,json=value)
    services=ModelServices(CONFIG); services.is_real=False
    pipeline=Pipeline(repo,services)
    async def operation(reservation,permit):
        return await ds.DeepSeekDirector(CONFIG,client_factory=client(handler),budget_check=permit).propose(
            [{'asset_id':'a','start':0.,'end':1.,'note':'测试'}],{'a':1.},reservation=reservation)
    pipeline.invoke(jid,'directions',.28,operation)
    with repo.factory() as db:
        ledger=db.scalars(select(Budget)).one()
        assert ledger.state=='unknown' and ledger.cost is None
    with pytest.raises(DomainError) as error:
        pipeline.invoke(jid,'directions',.28,operation,checkpoint_key='second')
    assert error.value.code=='COST_UNKNOWN'
