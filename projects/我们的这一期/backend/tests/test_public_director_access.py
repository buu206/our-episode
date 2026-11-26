"""Public source review does not implicitly authorize dialogue or execution."""
import asyncio
from copy import deepcopy
import json
import pytest
from app.models import Budget, Episode
from app.repository import DomainError
from app.services import director_chat_access as access, public_director_access as public, review_grant
from app.services.director import DirectorError
from test_director_chat_access import sample, transport, offline, CONFIG


@pytest.fixture
def public_sample(sample):
    repo, legacy, context, reservation = sample
    source = {'scope': 'public-fixture-output-review-v1', 'episode_id': legacy['episode_id'],
        'owner_id': legacy['owner_id'], 'providers': ['deepseek'],
        'operations': ['visual_review', 'sequence_review'], 'task_limit_yuan': 3,
        'total_limit_yuan': 30, 'retention_verified': False, 'approval': 'synthetic fixture review',
        'approved_at': '2026-11-18', 'assets': [{**a, 'source_url': 'https://example.com/fixture'} for a in legacy['assets']]}
    review_grant.grant_path(repo.data_dir, legacy['episode_id']).write_text(json.dumps(source))
    grant = {'scope': 'public-fixture-director-text-v1', 'episode_id': legacy['episode_id'],
        'owner_id': 'local-owner', 'providers': ['deepseek'], 'operations': ['director_chat'],
        'task_limit_yuan': 3, 'total_limit_yuan': 30, 'retention_verified': False,
        'approval': 'synthetic fixture dialogue', 'approved_at': '2026-11-18',
        'review_grant_digest': access._digest(source)}
    public.grant_path(repo.data_dir, legacy['episode_id']).write_text(json.dumps(grant))
    (repo.data_dir / access.GRANT_FILE).unlink()
    return repo, grant, context, reservation, source


def adapter(case, **kw):
    repo, grant, context, reservation, source = case
    return access.chat_adapter(kw.get('config', CONFIG), repo, kw.get('context', context),
        kw.get('permit', lambda value: value == reservation))


def run(case, service):
    return asyncio.run(service.chat(case[2], reservation=case[3]))


def test_public_dialogue_uses_existing_transport_without_global_enable(public_sample, transport):
    service = adapter(public_sample)
    assert isinstance(service, public.PublicDirectorChat) and service.ready()[0]
    result = run(public_sample, service)
    assert result['result'].action is None and len(transport['calls']) == 1
    assert CONFIG['CLOUD_ENABLED'] is False and CONFIG['DEEPSEEK_RETENTION_VERIFIED'] is False
    assert transport['calls'][0].url.host == 'api.deepseek.com'


@pytest.mark.parametrize('mode', ['missing', 'symlink', 'owner', 'operation', 'approval', 'source_grant',
                                  'source_bytes', 'evidence', 'expired', 'changed_during_permit'])
def test_changed_scope_blocks_before_network(public_sample, transport, mode):
    repo, grant, context, reservation, source = public_sample
    service = adapter(public_sample)
    assert service.ready()[0]
    path = public.grant_path(repo.data_dir, grant['episode_id'])
    if mode == 'missing': path.unlink()
    if mode == 'symlink':
        backup = path.with_suffix('.backup'); path.rename(backup); path.symlink_to(backup)
    if mode in ('owner', 'operation', 'approval'):
        if mode == 'owner': grant['owner_id'] = 'other'
        if mode == 'operation': grant['operations'].append('publish')
        if mode == 'approval': grant['approval'] = ''
        path.write_text(json.dumps(grant))
    if mode == 'source_grant':
        source['approval'] += ' changed'
        review_grant.grant_path(repo.data_dir, grant['episode_id']).write_text(json.dumps(source))
    if mode == 'source_bytes':
        row = source['assets'][0]; (repo.data_dir / row['path']).write_bytes(b'x' * row['size'])
    with repo.transaction() as db:
        if mode == 'evidence': db.get(Episode, grant['episode_id']).evidence_revision += 1
        if mode == 'expired':
            from app.retention_models import Retention
            db.get(Retention, grant['episode_id']).expires_at = 1
    if mode == 'changed_during_permit':
        count = 0
        async def permit(_):
            nonlocal count
            count += 1
            if count == 2: path.unlink()
            return True
        service = adapter(public_sample, permit=permit)
    with pytest.raises((DirectorError, DomainError)): run(public_sample, service)
    assert not transport['calls']


@pytest.mark.parametrize('mode', ['permit', 'foreign', 'settled', 'task', 'total'])
def test_public_dialogue_preserves_budget_limits(public_sample, transport, mode):
    repo, grant, _, reservation, _ = public_sample
    with repo.transaction() as db:
        if mode == 'foreign': db.get(Budget, reservation).episode_id = 'another'
        if mode == 'settled': db.get(Budget, reservation).state = 'settled'
        if mode in ('task', 'total'):
            db.add(Budget(owner_id='local-owner', episode_id=grant['episode_id'] if mode == 'task' else 'other',
                state='unknown', amount=3_000_000 if mode == 'task' else 30_000_000))
    service = adapter(public_sample, **({'permit': lambda _: False} if mode == 'permit' else {}))
    with pytest.raises(DirectorError): run(public_sample, service)
    assert not transport['calls']


@pytest.mark.parametrize('field', ['episode', 'assets', 'confirmed_evidence', 'current_version'])
def test_unsaved_or_foreign_content_not_sent(public_sample, transport, field):
    context = deepcopy(public_sample[2])
    if field == 'episode': context[field]['background'] = 'unsaved'
    if field == 'assets': context[field][0]['id'] = 'f'*32
    if field == 'confirmed_evidence': context[field][0]['note'] = 'invented'
    if field == 'current_version': context[field] = {'id': 'f'*32}
    service = adapter(public_sample, context=context)
    with pytest.raises((DirectorError, DomainError)):
        asyncio.run(service.chat(context, reservation=public_sample[3]))
    assert not transport['calls']


def test_frame_grant_alone_does_not_allow_chat_or_directions(public_sample):
    repo, grant, context, _, _ = public_sample
    public.grant_path(repo.data_dir, grant['episode_id']).unlink()
    assert not adapter(public_sample).ready()[0]
    assert not access.directions_adapter(CONFIG, repo, context).ready()[0]


def test_text_grant_does_not_grant_direction_generation(public_sample):
    repo, _, context, _, _ = public_sample
    assert adapter(public_sample).ready()[0]
    assert not access.directions_adapter(CONFIG, repo, context).ready()[0]


@pytest.fixture
def one_request(public_sample):
    import hashlib
    from app.models import Job, ModelCall
    from app.chat_models import DirectorChatTurn
    repo, grant, context, reservation, source = public_sample
    grant['one_request'] = {'client_id': 'one-authorized-question', 'version_id': 'a'*32,
        'question_sha256': hashlib.sha256(context['question'].encode()).hexdigest(), 'max_reservation_yuan': .09}
    # Use a saved ready version so ordinary scoped payload validation also runs.
    from app.models import Version
    with repo.transaction() as db:
        version = Version(id='a'*32,owner_id='local-owner',episode_id=grant['episode_id'],title='fixture',
            state='ready',segments=[],narration_text='',packaging='standard')
        db.add(version);db.flush()
        context['current_version']={k:getattr(version,k) for k in ('id','title','state','segments','narration_text','packaging')}
        job=Job(owner_id='local-owner',episode_id=grant['episode_id'],kind='director_chat',state='running',idempotency_key='one');db.add(job);db.flush()
        turn=DirectorChatTurn(owner_id='local-owner',episode_id=grant['episode_id'],client_id='one-authorized-question',request_hash='fixture',question=context['question'],source='model',context=deepcopy(context),snapshot={},job_id=job.id);db.add(turn);db.flush();job.payload={'turn_id':turn.id}
        db.get(Budget,reservation).amount=90_000
        db.add(ModelCall(owner_id='local-owner',episode_id=grant['episode_id'],job_id=job.id,reservation_id=reservation,kind='director_chat',state='sending'))
    public.grant_path(repo.data_dir,grant['episode_id']).write_text(json.dumps(grant))
    return public_sample


def test_single_authorized_question_sends_once(one_request,transport):
    run(one_request,adapter(one_request))
    assert len(transport['calls'])==1


@pytest.mark.parametrize('change',['client','question','cost','extra_attempt','completed'])
def test_single_question_limits_block_other_requests(one_request,transport,change):
    from app.models import ModelCall
    from app.chat_models import DirectorChatTurn
    from sqlalchemy import select
    repo,grant,context,reservation,_=one_request
    with repo.transaction() as db:
        turn=db.scalar(select(DirectorChatTurn));call=db.scalar(select(ModelCall))
        if change=='client':turn.client_id='another-question'
        if change=='question':context['question']='another question'
        if change=='cost':db.get(Budget,reservation).amount=100_000
        if change=='completed':call.state='succeeded'
        if change=='extra_attempt':db.add(ModelCall(owner_id='local-owner',episode_id=grant['episode_id'],job_id=call.job_id,reservation_id='extra',kind='director_chat'))
    with pytest.raises((DirectorError,DomainError)):run(one_request,adapter(one_request))
    assert not transport['calls']


@pytest.fixture
def demo_window(one_request, monkeypatch):
    from datetime import datetime, timezone
    from app.models import ModelCall
    from sqlalchemy import select
    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 21, 15, 30, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(public, 'datetime', FixedDate)
    repo, grant, context, reservation, source = one_request
    grant.pop('one_request')
    grant.pop('review_grant_digest')
    grant['approved_at'] = '2026-11-21T15:00:00+00:00'
    grant['assets'] = source['assets']
    grant['window'] = {'expires_at': '2026-11-23T00:00:00+08:00',
        'max_requests': 10, 'max_reservation_yuan': .09, 'total_reservation_yuan': .90,
        'daily_limits': {'2026-11-21': 1, '2026-11-22': 9}}
    with repo.transaction() as db:
        db.scalar(select(ModelCall)).created_at = '2026-11-21T15:15:00+00:00'
    review_grant.grant_path(repo.data_dir, grant['episode_id']).unlink()
    public.grant_path(repo.data_dir, grant['episode_id']).write_text(json.dumps(grant))
    return one_request


def test_demo_window_text_only_and_survives_adapter_recreation(demo_window, transport):
    repo, grant, context, reservation, _ = demo_window
    run(demo_window, adapter(demo_window))
    assert len(transport['calls']) == 1
    assert adapter(demo_window).ready()[0]  # same in-flight context reuses its hold
    assert not adapter(demo_window, context={'episode': {'id': grant['episode_id']}}).ready()[0]
    assert not access.directions_adapter(CONFIG, repo, context).ready()[0]
    assert not review_grant.grant_path(repo.data_dir, grant['episode_id']).exists()


@pytest.mark.parametrize('change', ['expired', 'extra_attempt', 'cost', 'foreign', 'source', 'missing_assets'])
def test_demo_window_blocks_before_network(demo_window, transport, change):
    from app.models import ModelCall
    from sqlalchemy import select
    repo, grant, context, reservation, _ = demo_window
    if change == 'expired': grant['approved_at'] = '2026-11-22T00:00:00+00:00'
    if change == 'missing_assets': grant.pop('assets')
    if change == 'source':
        item = grant['assets'][0]
        (repo.data_dir / item['path']).write_bytes(b'x' * item['size'])
    with repo.transaction() as db:
        call = db.scalar(select(ModelCall))
        if change == 'cost': db.get(Budget, reservation).amount = 100_000
        if change == 'foreign': call.owner_id = 'other'
        if change == 'extra_attempt':
            db.add(ModelCall(owner_id='local-owner', episode_id=grant['episode_id'],
                job_id=call.job_id, reservation_id='extra', kind='director_chat',
                created_at='2026-11-21T15:20:00+00:00'))
    public.grant_path(repo.data_dir, grant['episode_id']).write_text(json.dumps(grant))
    with pytest.raises((DirectorError, DomainError)):
        run(demo_window, adapter(demo_window))
    assert not transport['calls']


def test_demo_window_nine_tomorrow_and_expiry(demo_window, monkeypatch):
    from datetime import datetime, timezone
    from app.models import ModelCall
    from sqlalchemy import select
    repo, grant, _, reservation, _ = demo_window
    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 22, 4, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(public, 'datetime', Tomorrow)
    assert adapter(demo_window).ready()[0]
    with repo.transaction() as db:
        for i in range(9):
            hold=Budget(owner_id='local-owner', episode_id=grant['episode_id'], amount=90_000)
            db.add(hold); db.flush()
            db.add(ModelCall(owner_id='local-owner', episode_id=grant['episode_id'], job_id='tomorrow',
                reservation_id=hold.id, kind='director_chat', state='succeeded',
                created_at='2026-11-22T03:00:00+00:00'))
    assert not adapter(demo_window).ready()[0]
    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 22, 16, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(public, 'datetime', Expired)
    assert '结束' in adapter(demo_window).ready()[1]
