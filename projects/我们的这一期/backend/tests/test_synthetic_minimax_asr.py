"""Fixed audio admission, actual resampling, durable shared budget; no network."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select
from app.models import Budget, Job
from app.repository import Repository, DomainError
from app.services import media
from app.services.speech import AudioChunk, SpeechError
from test_minimax_speech import wav_bytes
from test_minimax_asr import envelope

scripts = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('asr_probe', scripts / 'synthetic_minimax_asr.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
KEY = 'offline-fake-key'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a, **k): pytest.fail('Network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'data')
    source = repo.data_dir / probe.SOURCE
    source.write_bytes(wav_bytes())
    prepared = tmp_path / 'expected.wav'
    subprocess.run([media._binary('ffmpeg'), '-nostdin', '-v', 'error', '-n', '-i', str(source),
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-map_metadata', '-1', str(prepared)], check=True)
    monkeypatch.setattr(probe, 'SOURCE_SHA', hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(probe, 'PREPARED_SHA', hashlib.sha256(prepared.read_bytes()).hexdigest())
    yield repo
    repo.engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'timeout': False}
    original = httpx.AsyncClient
    def handler(req):
        state['calls'].append(req)
        assert b'fictional-narration' not in req.content and b'expected' not in req.content
        if state['timeout']: raise httpx.ReadTimeout(KEY)
        return httpx.Response(200, json=envelope())
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def test_real_resampling_and_once_only_with_shared_held_budget(fixture_repo, transport):
    repo = fixture_repo
    with repo.transaction() as db:
        row = Budget(owner_id='old-tts', episode_id='old-tts', amount=24500, cost=None, state='unknown')
        db.add(row); db.flush(); old_id = row.id
    result = probe.run_probe(repo, KEY, authorized=True)
    assert result['state'] == 'succeeded' and result['recognized_text'] == '你好！'
    assert result['budget']['total_reserved'] == .0245 and result['budget']['spent'] == .000695
    restarted = Repository(repo.data_dir)
    assert probe.run_probe(restarted, KEY, authorized=True)['words'] == result['words']
    with restarted.factory() as db:
        assert db.get(Budget, old_id).cost is None and db.get(Budget, old_id).state == 'unknown'
    restarted.engine.dispose()
    assert len(transport['calls']) == 1 and not list(repo.data_dir.rglob('冒烟记录.md'))
    assert not list((repo.data_dir/'incoming').iterdir())
    assert KEY not in json.dumps(result)
    assert result['model_profile']['retention_verified'] is False


def test_unknown_cost_never_automatically_retries(fixture_repo, transport):
    transport['timeout'] = True
    with pytest.raises(DomainError): probe.run_probe(fixture_repo, KEY, authorized=True)
    with pytest.raises(DomainError, match='费用'): probe.run_probe(fixture_repo, KEY, authorized=True)
    assert len(transport['calls']) == 1
    with fixture_repo.factory() as db:
        ledger = db.scalars(select(Budget)).one()
        assert ledger.state == 'unknown' and ledger.cost is None
    assert KEY not in (fixture_repo.data_dir/'技术测试/MiniMax固定转写/技术调用记录.json').read_text()


def test_shared_global_limit_includes_unknown(fixture_repo, transport):
    with fixture_repo.transaction() as db:
        db.add(Budget(owner_id='other', episode_id='other', amount=29_999_000, cost=None, state='unknown'))
    with pytest.raises(DomainError, match='预算'): probe.run_probe(fixture_repo, KEY, authorized=True)
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['unauthorized', 'key', 'source', 'prepared', 'offset'])
def test_reject_other_input_before_send(fixture_repo, transport, mode, tmp_path):
    repo = fixture_repo
    if mode in ('unauthorized', 'key'):
        with pytest.raises(DomainError):
            probe.run_probe(repo, '' if mode == 'key' else KEY, authorized=mode != 'unauthorized')
    elif mode == 'source':
        (repo.data_dir / probe.SOURCE).write_bytes(b'different-audio')
        with pytest.raises(DomainError, match='指纹'): probe.run_probe(repo, KEY, authorized=True)
    else:
        with pytest.raises(SpeechError):
            if mode == 'prepared':
                p = tmp_path/'different.wav'; p.write_bytes(wav_bytes(rate=16000))
                chunks = [AudioChunk(p, 'fictional-narration', 0, 1)]
            else:
                chunks = probe.prepare(repo, tmp_path)
                chunks = [AudioChunk(chunks[0].path, 'fictional-narration', 1, 1)]
            asyncio.run(probe.FixedASR(KEY, authorized=True).transcribe(chunks))
    assert not transport['calls']
    with repo.factory() as db:
        assert not list(db.scalars(select(Budget)))


def test_changed_profile_expiry_and_paid_checkpoint(fixture_repo, transport, monkeypatch):
    repo = fixture_repo
    value = probe.run_probe(repo, KEY, authorized=True)
    with repo.transaction() as db:
        db.get(Job, value['job_id']).state = 'failed'
    assert probe.run_probe(repo, KEY, authorized=True)['state'] == 'succeeded'
    assert len(transport['calls']) == 1
    with monkeypatch.context() as m:
        m.setattr(probe, 'PREPARED_SHA', 'changed')
        with pytest.raises(DomainError, match='变化'): probe.run_probe(repo, KEY, authorized=True)
    with repo.transaction() as db:
        db.get(probe.Retention, probe.EPISODE_ID).expires_at = 1
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    assert len(transport['calls']) == 1
