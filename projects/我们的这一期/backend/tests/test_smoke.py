"""Offline smoke-tool contracts. All records/configuration live under tmp_path.

No real service, provider, existing .env or product evidence directory is read.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import time

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Base, Budget, Episode, Job, ModelCall, Session as LoginSession, Version

SPEC = importlib.util.spec_from_file_location('smoke_tool', Path(__file__).resolve().parents[2] / 'scripts/smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
REAL_CLIENT = httpx.Client


@pytest.fixture
def h(tmp_path, monkeypatch):
    product = tmp_path / 'product'
    data = product / 'custom-data'
    data.mkdir(parents=True)
    monkeypatch.setattr(smoke, 'PRODUCT', product)
    engine = create_engine('sqlite:///' + str(data / 'app.sqlite3'))
    Base.metadata.create_all(engine)
    eid, jid, cid, bid = (str(i) * 32 for i in range(1, 5))
    token, csrf = 'offline-cookie', 'offline-csrf'
    with Session(engine) as db:
        db.add(Episode(id=eid, owner_id='alice', title='private-title', background='private-content',
                       evidence_revision=1, evidence_confirmed_revision=1))
        db.add(Job(id=jid, owner_id='alice', episode_id=eid, kind='analysis', state='succeeded',
                   stage='finished', idempotency_key='offline-job'))
        db.add(Budget(id=bid, owner_id='alice', episode_id=eid, amount=850000, cost=1000, state='settled'))
        db.add(ModelCall(id=cid, owner_id='alice', episode_id=eid, job_id=jid,
                         reservation_id=bid, kind='vision', state='succeeded', elapsed=1.5,
                         usage={'real_call': True, 'model': 'qwen3-vl-plus', 'request_id': 'provider-1',
                                'cost_status': 'known', 'cost_yuan': .001, 'input_tokens': 10,
                                'output_tokens': 5, 'retries': 0}))
        db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), owner_id='alice',
                            csrf=csrf, expires=time.time() + 3600))
        db.commit()
    config = {'APP_DATA_DIR': str(data), 'APP_INVITE_CODE': 'offline-invite', 'MODEL_API_KEY': 'offline-key',
              'CLOUD_ENABLED': 'true', 'CLOUD_RETENTION_VERIFIED': 'true', 'CLOUD_REGION_VERIFIED': 'true'}
    value = SimpleNamespace(product=product, engine=engine, eid=eid, jid=jid, cid=cid, bid=bid,
                            token=token, csrf=csrf, config=config, requests=[], status_mode='cloud',
                            job_status='succeeded', owner='alice', fail_poll=False, mismatch_cookie=False,
                            cloud_enabled=True)

    def handler(request):
        path = request.url.path.removeprefix('/api/v1')
        value.requests.append((request.method, path))
        if path == '/status':
            return httpx.Response(200, json={'cloud_enabled': value.cloud_enabled, 'model_mode': value.status_mode})
        if path == '/auth/login':
            cookie = 'wrong-cookie' if value.mismatch_cookie else token
            return httpx.Response(200, json={'user_id': value.owner, 'csrf_token': csrf},
                                  headers={'set-cookie': 'episode_session=' + cookie + '; Path=/; HttpOnly'})
        if path == '/auth/logout':
            assert request.headers.get('X-CSRF-Token') == csrf
            return httpx.Response(200, json={'ok': True})
        if path == '/jobs/' + jid:
            if value.fail_poll:
                raise httpx.ReadTimeout('private-provider-response', request=request)
            return httpx.Response(200, json=remote(value))
        if path == '/episodes/' + eid:
            return httpx.Response(200, json={'id': eid, 'evidence_revision': 1, 'evidence_confirmed_revision': 1})
        if path == '/episodes':
            return httpx.Response(200, json={'items': [{'id': eid, 'versions': getattr(value, 'versions', [])}]})
        if request.method == 'POST' and path in ('/episodes/' + eid + '/analysis', '/episodes/' + eid + '/directions'):
            return httpx.Response(200, json=remote(value))
        raise AssertionError('unexpected offline HTTP request: ' + path)

    monkeypatch.setattr(smoke, 'configuration', lambda: dict(config))
    monkeypatch.setattr(smoke.httpx, 'Client', lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    yield value
    engine.dispose()


def remote(h):
    with Session(h.engine) as db:
        job = db.get(Job, h.jid)
        return {'id': job.id, 'kind': job.kind, 'version_id': job.version_id, 'state': h.job_status}


def change(h, cls, identifier, **values):
    with Session(h.engine) as db:
        item = db.get(cls, identifier)
        for key, value in values.items():
            setattr(item, key, value)
        db.commit()


def output(h, stage='阶段2-1'):
    return h.product / 'docs/evidence' / stage / '冒烟记录.md'


def diagnostic(h, stage='阶段2-1'):
    return Path(h.config['APP_DATA_DIR']) / '真实AI诊断' / stage / '调用记录.md'


def record(h, stage='阶段2-1'):
    return smoke.record_real_calls(h.engine, h.jid, 'alice', remote(h), stage, h.product / 'docs/evidence')


@pytest.mark.parametrize('kind', ['director_chat', 'visual_review'])
def test_director_chat_observation_requires_real_provider_receipt_and_settlement(h, kind):
    with Session(h.engine) as db:
        job = db.get(Job, h.jid); job.kind = kind
        call = db.get(ModelCall, h.cid); call.kind = kind
        call.usage = {**call.usage, 'model': 'deepseek-flash'}
        db.commit()
    observed = {**remote(h), 'kind': kind}
    assert smoke.record_real_calls(h.engine, h.jid, 'alice', observed, '阶段3-2', h.product/'docs/evidence')[0] == 1
    content = output(h, '阶段3-2').read_text()
    assert kind + '/deepseek-flash' in content and 'provider-1' in content
    assert 'private-content' not in content


def test_observe_only_reads_existing_job_records_selected_stage_and_logs_out(h, capsys):
    assert smoke.main(['observe', h.jid, '--stage', '阶段3-2']) == 0
    assert output(h, '阶段3-2').is_file()
    assert not output(h).exists()
    assert [path for method, path in h.requests if method == 'POST'] == ['/auth/login', '/auth/logout']
    content = output(h, '阶段3-2').read_text()
    assert h.jid in content and h.cid in content and 'qwen3-vl-plus' in content
    assert 'private-content' not in content and 'private-title' not in content
    stdout = capsys.readouterr().out
    assert 'offline-key' not in stdout and 'offline-invite' not in stdout and 'offline-csrf' not in stdout


def test_observe_after_cloud_disabled_needs_no_key_or_admission_and_never_submits(h):
    for key in ('MODEL_API_KEY', 'CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED'):
        h.config.pop(key)
    h.cloud_enabled = False
    assert smoke.main(['observe', h.jid]) == 0
    assert output(h).is_file()
    assert [path for method, path in h.requests if method == 'POST'] == ['/auth/login', '/auth/logout']


@pytest.mark.parametrize('key', ['CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED', 'MODEL_API_KEY'])
def test_missing_admission_never_connects_or_writes(h, key):
    h.config[key] = ''
    assert smoke.main(['analysis', h.eid]) == 2
    assert h.requests == [] and not output(h).exists()


def test_demo_service_and_demo_directory_are_rejected(h):
    h.status_mode = 'demo'
    assert smoke.main(['observe', h.jid]) == 2
    assert h.requests == [('GET', '/status')]
    h.config['APP_DATA_DIR'] = str(h.product / 'data/demo')
    h.requests.clear()
    assert smoke.main(['observe', h.jid]) == 2
    assert h.requests == [] and not output(h).exists()


def test_database_is_existing_configured_database_and_read_only(h, tmp_path):
    engine = smoke.open_database(h.config)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM jobs').scalar() == 1
            with pytest.raises(OperationalError):
                connection.exec_driver_sql('DELETE FROM jobs')
    finally:
        engine.dispose()
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(smoke.SmokeError):
        smoke.open_database({'APP_DATA_DIR': str(empty)})
    assert not (empty / 'app.sqlite3').exists()


def test_database_session_mismatch_blocks_paid_submit_and_still_logs_out(h):
    h.mismatch_cookie = True
    assert smoke.main(['analysis', h.eid]) == 2
    assert ('POST', '/auth/logout') in h.requests
    assert not any(path.endswith('/analysis') for _, path in h.requests)
    assert not output(h).exists()


@pytest.mark.parametrize('cls,key', [(Job, 'jid'), (Episode, 'eid'), (Budget, 'bid'), (ModelCall, 'cid')])
def test_cross_owner_records_cannot_be_exported(h, cls, key):
    change(h, cls, getattr(h, key), owner_id='bob')
    with pytest.raises(smoke.SmokeError):
        record(h)
    assert not output(h).exists()


@pytest.mark.parametrize('state', ['prepared', 'sending', 'interrupted'])
def test_unfinished_call_gets_no_record_or_dedup_marker_then_terminal_can_be_recorded(h, state):
    change(h, ModelCall, h.cid, state=state)
    assert record(h) == (0, 0)
    assert not output(h).exists()
    change(h, ModelCall, h.cid, state='succeeded')
    assert record(h) == (1, 1)
    assert output(h).read_text().count('<!-- call:') == 1


@pytest.mark.parametrize('update', [
    {'real_call': False}, {'cost_status': 'not_sent'}, {'request_id': None},
    {'request_id': 'bad|private\ntext'}, {'model': 'DEMO-FIXTURE'}, {'model': 'unknown-provider'},
])
def test_real_adapter_flag_is_insufficient_proof_of_sent_request(h, update):
    with Session(h.engine) as db:
        call = db.get(ModelCall, h.cid)
        call.usage = {**call.usage, **update}
        db.commit()
    assert record(h) == (0, 0)
    assert not output(h).exists()


def test_real_failed_response_is_recorded_as_failure_not_pass(h):
    change(h, ModelCall, h.cid, state='failed')
    change(h, Job, h.jid, state='failed')
    h.job_status = 'failed'
    assert smoke.main(['observe', h.jid]) == 1
    assert not output(h).exists()
    content = diagnostic(h).read_text()
    assert '失败（真实请求，非通过）' in content and '未通过或未完成' in content
    assert '结构合规' not in content


@pytest.mark.parametrize('state,cost', [('unknown', None), ('reserved', None), ('settled', None)])
def test_unsettled_ledger_overrules_known_provider_usage_and_cannot_pass(h, state, cost):
    change(h, Budget, h.bid, state=state, cost=cost)
    assert smoke.main(['observe', h.jid, '--stage', '阶段3-2']) == 1
    assert not output(h, '阶段3-2').exists()
    content = diagnostic(h, '阶段3-2').read_text()
    assert '待核对，保留预留' in content and '账本已结算' not in content
    assert '| 0.001 | - | 待核对' in content
    with Session(h.engine) as db:
        ledger = db.get(Budget, h.bid)
        assert (ledger.state, ledger.cost) == (state, cost)


@pytest.mark.parametrize('job_state', ['succeeded', 'failed'])
def test_partial_success_never_creates_standard_smoke_even_if_job_claims_success(h, job_state):
    bid, cid = '6' * 32, '7' * 32
    with Session(h.engine) as db:
        db.add(Budget(id=bid, owner_id='alice', episode_id=h.eid, amount=1000, cost=220, state='settled'))
        db.add(ModelCall(id=cid, owner_id='alice', episode_id=h.eid, job_id=h.jid,
                         reservation_id=bid, kind='asr', state='failed', elapsed=.5,
                         usage={'real_call': True, 'model': smoke.MODELS['asr'], 'request_id': 'provider-failed',
                                'cost_status': 'known', 'cost_yuan': .00022, 'seconds': 1, 'retries': 0}))
        db.commit()
    change(h, Job, h.jid, state=job_state)
    h.job_status = job_state
    assert smoke.main(['observe', h.jid]) == 1
    assert not output(h).exists()
    content = diagnostic(h).read_text()
    assert h.cid in content and cid in content and '失败（真实请求，非通过）' in content


def test_diagnostic_dedup_and_cross_stage_guard_preserve_file_and_exit_failure(h):
    change(h, ModelCall, h.cid, state='failed')
    change(h, Job, h.jid, state='failed')
    h.job_status = 'failed'
    assert record(h) == (0, 1)
    os.utime(diagnostic(h), ns=(1_000_000_000, 1_000_000_000))
    before = diagnostic(h).stat().st_mtime_ns
    assert smoke.main(['observe', h.jid]) == 1
    assert diagnostic(h).stat().st_mtime_ns == before
    assert diagnostic(h).read_text().count('<!-- call:') == 1
    with pytest.raises(smoke.SmokeError, match='另一阶段'):
        record(h, '阶段3-2')
    assert not diagnostic(h, '阶段3-2').exists() and not output(h).exists()


def test_repeated_observe_does_not_refresh_mtime_or_duplicate_and_cross_stage_refused(h):
    assert record(h) == (1, 1)
    original = output(h).read_bytes()
    os.utime(output(h), ns=(1_000_000_000, 1_000_000_000))
    before = output(h).stat().st_mtime_ns
    assert record(h) == (1, 0)
    assert output(h).stat().st_mtime_ns == before
    with pytest.raises(smoke.SmokeError, match='另一阶段'):
        record(h, '阶段3-2')
    assert output(h).read_bytes() == original
    assert output(h).stat().st_mtime_ns == before and not output(h, '阶段3-2').exists()


def test_old_success_cannot_be_fresh_evidence_but_failure_stays_diagnostic(h, monkeypatch):
    monkeypatch.setattr(smoke, 'latest_source_timestamp', lambda product: time.time() + 100)
    with pytest.raises(smoke.SmokeError, match='代码已有更新'):
        record(h)
    assert not output(h).exists()
    change(h, ModelCall, h.cid, state='failed')
    change(h, Job, h.jid, state='failed')
    h.job_status = 'failed'
    assert record(h) == (0, 1)
    assert diagnostic(h).is_file() and not output(h).exists()


def test_source_freshness_scans_all_three_roots_but_excludes_build_caches(tmp_path):
    for root in ('backend', 'frontend', 'scripts'):
        (tmp_path / root).mkdir()
        path = tmp_path / root / 'source.txt'
        path.write_text('offline')
        os.utime(path, (100, 100))
    for directory in ('.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache', 'dist'):
        target = tmp_path / 'frontend' / directory / 'generated.txt'
        target.parent.mkdir()
        target.write_text('offline')
        os.utime(target, (500, 500))
    assert smoke.latest_source_timestamp(tmp_path) == 100
    os.utime(tmp_path / 'scripts/source.txt', (200, 200))
    assert smoke.latest_source_timestamp(tmp_path) == 200


def test_wait_timeout_writes_nothing_and_observe_later_records_without_resubmit(h, monkeypatch):
    change(h, Job, h.jid, state='running')
    change(h, ModelCall, h.cid, state='sending')
    h.job_status = 'running'
    monkeypatch.setattr(smoke, 'WAIT_SECONDS', 0)
    assert smoke.main(['observe', h.jid]) == 1
    assert not output(h).exists() and h.requests[-1] == ('POST', '/auth/logout')
    change(h, Job, h.jid, state='succeeded')
    change(h, ModelCall, h.cid, state='succeeded')
    h.job_status = 'succeeded'
    assert smoke.main(['observe', h.jid]) == 0
    assert output(h).read_text().count('<!-- call:') == 1
    assert all(path in ('/auth/login', '/auth/logout') for method, path in h.requests if method == 'POST')


def test_poll_exception_logs_out_without_leaking_response_or_exporting(h, capsys):
    h.fail_poll = True
    assert smoke.main(['observe', h.jid]) == 1
    assert h.requests[-1] == ('POST', '/auth/logout') and not output(h).exists()
    assert 'private-provider-response' not in capsys.readouterr().out


def test_analysis_keeps_compatible_default_stage_and_submits_once(h):
    assert smoke.main(['analysis', h.eid]) == 0
    assert h.requests.count(('POST', '/episodes/' + h.eid + '/analysis')) == 1
    assert output(h).is_file()


def test_unconfirmed_evidence_blocks_directions_before_post(h):
    change(h, Episode, h.eid, evidence_confirmed_revision=None)
    assert smoke.main(['directions', h.eid]) == 2
    assert not any(path.endswith('/directions') for _, path in h.requests)
    assert h.requests[-1] == ('POST', '/auth/logout')


def test_narration_does_not_turn_command_execution_into_human_approval(h):
    vid = '5' * 32
    with Session(h.engine) as db:
        db.add(Version(id=vid, owner_id='alice', episode_id=h.eid, title='version', segments=[],
                       narration_text='private narration', narration_approved=False))
        db.commit()
    h.versions = [{'id': vid, 'narration_approved': False, 'narration_text': 'private narration'}]
    assert smoke.main(['narration', vid]) == 2
    assert not any(path.endswith('/narration') for _, path in h.requests)
    assert h.requests[-1] == ('POST', '/auth/logout')


def test_unknown_job_and_wrong_remote_kind_do_not_export(h):
    bad = remote(h)
    bad['kind'] = 'narration'
    with Session(h.engine) as db, pytest.raises(smoke.SmokeError):
        smoke.owned_job(db, h.jid, 'alice', bad)
    with Session(h.engine) as db, pytest.raises(smoke.SmokeError):
        smoke.owned_job(db, 'f' * 32, 'alice', bad)
    assert not output(h).exists()


def test_configuration_matches_dotenv_interpolation_and_environment_without_mutation(tmp_path, monkeypatch):
    product = tmp_path / 'config-product'
    (product / 'backend').mkdir(parents=True)
    (product / 'backend/.env').write_text('SMOKE_TEST_VALUE=file\nAPP_DATA_DIR=${SMOKE_TEST_VALUE}/data\nSMOKE_EMPTY\n')
    monkeypatch.setattr(smoke, 'PRODUCT', product)
    monkeypatch.setenv('SMOKE_TEST_VALUE', 'environment')
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED', raising=False)
    monkeypatch.delenv('APP_DATA_DIR', raising=False)
    before = dict(os.environ)
    result = smoke.configuration()
    assert result['APP_DATA_DIR'] == 'environment/data'
    assert 'SMOKE_EMPTY' not in result and dict(os.environ) == before
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', 'TRUE')
    assert 'APP_DATA_DIR' not in smoke.configuration()


def test_invalid_stage_and_target_cannot_touch_configuration(h, monkeypatch):
    monkeypatch.setattr(smoke, 'configuration', lambda: pytest.fail('configuration should not be read'))
    with pytest.raises(SystemExit):
        smoke.main(['observe', h.jid, '--stage', '../阶段4'])
    assert smoke.main(['observe', 'not-an-id']) == 2
    assert h.requests == []


def test_new_provider_record_preserves_actual_model_without_body(h):
    change(h, ModelCall, h.cid, usage={'real_call': True, 'model': 'deepseek-flash',
        'model_version': 'DeepSeek-V4.1-Flash-1017', 'request_id': 'provider-new',
        'cost_status': 'known', 'cost_yuan': .001, 'input_tokens': 10,
        'output_tokens': 5, 'retries': 0})
    record(h)
    content = output(h).read_text()
    assert 'deepseek-flash' in content and 'DeepSeek-V4.1-Flash-1017' in content
    assert 'private-content' not in content
