"""Fixed-sample admission, accounting and recovery; no real network or keys."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select
from app.models import Episode, Job, Budget
from app.repository import Repository, DomainError
from app.services.speech import SpeechError
from test_minimax_speech import envelope

spec = importlib.util.spec_from_file_location('minimax_probe', Path(__file__).resolve().parents[2] / 'scripts/synthetic_minimax.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
KEY = 'offline-test-only'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a, **k): pytest.fail('Network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(calls=[], missing_usage=False, timeout=False, provider_error=0,
                            auth_calls=0, auth_ok=True)
    original = httpx.AsyncClient
    sync_original = httpx.Client
    def handler(request):
        state.calls.append(request)
        body = json.loads(request.content)
        assert body['text'] == probe.TEXT
        if state.timeout:
            raise httpx.ReadTimeout('should not appear: ' + KEY)
        if state.provider_error:
            return httpx.Response(200, json={'base_resp': {'status_code': state.provider_error}})
        response = envelope()
        if state.missing_usage:
            response['extra_info'].pop('usage_characters')
        return httpx.Response(200, json=response)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    def auth_handler(request):
        state.auth_calls += 1
        assert str(request.url) == 'https://api.minimax.cn/v1/get_voice'
        assert json.loads(request.content) == {'voice_type': 'system'}
        return httpx.Response(200, json={'base_resp': {'status_code': 0 if state.auth_ok else 1004},
            'system_voice': [{'voice_id': probe.VOICE_CONFIG['MINIMAX_TTS_VOICE']}]})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: sync_original(transport=httpx.MockTransport(auth_handler), **kwargs))
    return state


def test_reject_unapproved_key_text_and_config_before_request(tmp_path, transport):
    repo = Repository(tmp_path)
    for key, approval in ((KEY, False), ('', True)):
        with pytest.raises(DomainError): probe.run_probe(repo, key, authorized=approval)
    service = probe.FixedSpeech(KEY, authorized=True)
    with pytest.raises(SpeechError, match='固定'):
        asyncio.run(service.synthesize('私人文字', tmp_path / 'v.wav'))
    service._config['MINIMAX_TTS_VOICE'] = 'custom-clone'
    assert not service.ready()[0]
    with repo.factory() as db:
        assert not list(db.scalars(select(Episode)))
        assert not list(db.scalars(select(Budget)))
    assert transport.calls == []


def test_one_call_budget_audio_restart_and_secret_free_report(tmp_path, transport):
    repo = Repository(tmp_path)
    value = probe.run_probe(repo, KEY, authorized=True)
    assert value['state'] == 'succeeded'
    assert value['budget']['spent'] == .0014
    assert value['budget']['reserved'] == 0
    assert Path(value['audio']['path']).is_file()
    assert KEY not in json.dumps(value)
    assert value['model_profile']['retention_verified'] is False
    assert probe.run_probe(Repository(tmp_path), KEY, authorized=True)['audio'] == value['audio']
    assert len(transport.calls) == 1
    assert not list(tmp_path.rglob('冒烟记录.md'))


@pytest.mark.parametrize('failure', ['missing_usage', 'timeout'])
def test_unknown_cost_kept_and_repeat_never_resends(tmp_path, transport, failure):
    setattr(transport, failure, True)
    repo = Repository(tmp_path)
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    with pytest.raises(DomainError, match='费用'): probe.run_probe(Repository(tmp_path), KEY, authorized=True)
    assert len(transport.calls) == 1
    with repo.factory() as db:
        ledger = db.scalars(select(Budget)).one()
        assert ledger.state == 'unknown' and ledger.cost is None
    text = (tmp_path/'技术测试/MiniMax固定旁白/技术调用记录.json').read_text()
    assert KEY not in text


def test_prior_spend_shared_global_limit_stops_send(tmp_path, transport):
    repo = Repository(tmp_path)
    with repo.transaction() as db:
        db.add(Budget(owner_id='other-provider', episode_id='other', amount=30_000_000,
                      cost=30_000_000, state='settled'))
    with pytest.raises(DomainError, match='预算'): probe.run_probe(repo, KEY, authorized=True)
    assert transport.calls == []


def test_changed_profile_and_expired_episode_cannot_create_fresh_budget(tmp_path, transport, monkeypatch):
    repo = Repository(tmp_path)
    value = probe.run_probe(repo, KEY, authorized=True)
    monkeypatch.setattr(probe, 'TEXT', '新文案')
    with pytest.raises(DomainError, match='变化'): probe.run_probe(repo, KEY, authorized=True)
    assert len(transport.calls) == 1
    with repo.transaction() as db:
        db.get(probe.Retention, probe.EPISODE_ID).expires_at = 1
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    assert len(transport.calls) == 1


def test_checkpoint_before_job_completion_recovers_without_resend(tmp_path, transport):
    repo = Repository(tmp_path)
    value = probe.run_probe(repo, KEY, authorized=True)
    with repo.transaction() as db:
        db.get(Job, value['job_id']).state = 'failed'
    assert probe.run_probe(repo, KEY, authorized=True)['state'] == 'succeeded'
    assert len(transport.calls) == 1


def test_artifact_registered_for_30day_cleanup(tmp_path, transport):
    from app.services.retention import request_deletion, maintain
    repo = Repository(tmp_path)
    result = probe.run_probe(repo, KEY, authorized=True)
    audio = Path(result['audio']['path'])
    request_deletion(repo, probe.EPISODE_ID, probe.OWNER)
    maintain(repo)
    assert not audio.exists()
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    assert len(transport.calls) == 1


def test_lock_rejects_second_process_and_releases(tmp_path):
    with probe.probe_lock(tmp_path):
        with pytest.raises(DomainError, match='运行'):
            with probe.probe_lock(tmp_path): pass
    with probe.probe_lock(tmp_path): pass


def test_reviewed_key_fix_keeps_unknown_cost_and_all_limits(tmp_path, transport):
    repo = Repository(tmp_path)
    transport.provider_error = 1004
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    transport.provider_error = 0
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    value = probe.run_probe(repo, KEY, authorized=True, retry_after_key_fix=True)
    assert value['state'] == 'succeeded' and len(transport.calls) == 2
    assert value['budget']['spent'] == .0014
    assert value['budget']['reserved'] == probe.tts_estimate(probe.TEXT)
    assert value['calls'][0]['settled_yuan'] is None
    assert value['calls'][0]['ledger_state'] == 'unknown'
    assert probe.run_probe(repo, KEY, authorized=True, retry_after_key_fix=True)['audio'] == value['audio']
    assert len(transport.calls) == 2 and transport.auth_calls == 1


def test_reviewed_retry_still_obeys_shared_budget(tmp_path, transport):
    repo = Repository(tmp_path); transport.provider_error = 1004
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    with repo.transaction() as db:
        db.add(Budget(owner_id='other', episode_id='other', amount=29_970_000,
                      cost=29_970_000, state='settled'))
    transport.provider_error = 0
    with pytest.raises(DomainError, match='预算'):
        probe.run_probe(repo, KEY, authorized=True, retry_after_key_fix=True)
    assert len(transport.calls) == 1


@pytest.mark.parametrize('blocked', ['new_key_invalid', 'second_attempt_failed', 'non_auth_failure'])
def test_retry_does_not_become_a_loop_or_general_bypass(tmp_path, transport, blocked):
    repo = Repository(tmp_path); transport.provider_error = 1004
    if blocked == 'non_auth_failure':
        transport.provider_error = 0; transport.timeout = True
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    if blocked == 'new_key_invalid': transport.auth_ok = False
    with pytest.raises(DomainError):
        probe.run_probe(repo, KEY, authorized=True, retry_after_key_fix=True)
    if blocked == 'second_attempt_failed':
        with pytest.raises(DomainError):
            probe.run_probe(repo, KEY, authorized=True, retry_after_key_fix=True)
        assert len(transport.calls) == 2
    else:
        assert len(transport.calls) == 1


def test_normal_pipeline_still_blocks_unknown_cost(tmp_path, transport):
    from app.services.pipeline import Pipeline
    repo = Repository(tmp_path); transport.provider_error = 1004
    with pytest.raises(DomainError): probe.run_probe(repo, KEY, authorized=True)
    jid = probe.ensure_job(repo, probe.Services(KEY, True).profile())
    with pytest.raises(DomainError, match='费用'):
        Pipeline(repo, probe.Services(KEY, True)).invoke(jid, 'tts', .0245,
            lambda *args: pytest.fail('Production must not bypass unknown costs'))
    assert len(transport.calls) == 1
