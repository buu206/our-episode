"""Durable requirement flow with offline services; no real model quality claim."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.director_guidance_models import DirectorGuidance
from app.main import create_app
from app.models import Audit, Budget, Episode, Job, ModelCall, Version
from app.repository import DomainError
from app.retention_models import Retention
from app.services import director_guidance as guidance, retention
from app.worker import source_segments
from test_pipeline import (advance, analyze_and_review, authenticate, client,
                           create_with_asset, episode_state, no_network, request,
                           services, settings, voiced_video_bytes)

BASE = '/api/v1'


def episode_only(client, background='今晚朋友聚会'):
    auth = authenticate(client)
    episode = request(client, '/episodes', auth, expected=201,
                      body={'title': '离线导演引导测试', 'background': background})
    return auth, episode


def send(client, auth, eid, revision, *, client_id=None, text=None, choice=None, expected=200):
    body = {'revision': revision, 'client_id': client_id or f'test-message-{revision}'}
    if text is not None:
        body['text'] = text
    if choice is not None:
        body['choice'] = choice
    return request(client, f'/episodes/{eid}/director/messages', auth, body=body, expected=expected)


def test_virtual_initial_state_does_not_create_session_or_call_model(client, services):
    _, episode = episode_only(client)
    current = episode['director']
    assert current['revision'] == 0 and current['focus'] is None and current['notes'] == ''
    assert not current['directions_current']
    assert current['messages'][0]['text'] == episode['background']
    assert current['messages'][1]['role'] == 'director'
    assert episode_state(client, episode['id'])['director'] == current
    repo = client.app.state.repo
    with repo.factory() as db:
        assert db.get(DirectorGuidance, episode['id']) is None
        assert guidance.compiled_background(db, db.get(Episode, episode['id'])) == episode['background']
        assert not list(db.scalars(select(ModelCall)))
    assert not services.counts


def test_append_focus_idempotency_and_restart_preserve_all_requirements(client, services, settings):
    auth, episode = episode_only(client)
    eid = episode['id']
    chosen = send(client, auth, eid, 0, choice='conversation')
    assert chosen['revision'] == 1 and chosen['focus'] == 'conversation'
    assert chosen['messages'][-2]['id'] == 'test-message-0'
    first = send(client, auth, eid, 1, text='保留完整对话')
    second = send(client, auth, eid, 2, text='结尾不要煽情')
    assert second['notes'] == '保留完整对话\n结尾不要煽情'
    assert second['messages'][:len(first['messages'])] == first['messages']
    assert send(client, auth, eid, 1, text='保留完整对话') == second
    conflict = send(client, auth, eid, 1, text='用同一编号换需求', expected=409)
    assert conflict['error']['code'] == 'DIRECTOR_REQUEST_CONFLICT'
    conflict = send(client, auth, eid, 1, client_id='other-client-1', text='过时标签页', expected=409)
    assert conflict['error']['code'] == 'DIRECTOR_REVISION_CONFLICT'
    assert episode_state(client, eid)['retention']['expires_at'] == episode['retention']['expires_at']
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as restarted:
        authenticate(restarted)
        assert episode_state(restarted, eid)['director'] == second
    repo = client.app.state.repo
    with repo.factory() as db:
        row = db.get(DirectorGuidance, eid)
        assert len(row.operations) == 3
        assert all('text' not in operation for operation in row.operations)
        assert not list(db.scalars(select(ModelCall)))
        assert all('保留完整对话' not in json.dumps(a.details, ensure_ascii=False)
                   for a in db.scalars(select(Audit)))


@pytest.mark.parametrize('payload', [
    {}, {'text': ' '}, {'text': 'x', 'choice': 'warm'}, {'choice': 'invented'},
    {'text': 'x' * 1001}, {'text': 'x', 'client_id': 'short'}, {'text': 'x', 'revision': -1},
])
def test_invalid_message_is_atomic(client, payload):
    auth, episode = episode_only(client)
    body = {'revision': 0, 'client_id': 'invalid-test', **payload}
    request(client, f'/episodes/{episode["id"]}/director/messages', auth, body=body, expected=422)
    assert episode_state(client, episode['id'])['director'] == episode['director']


def test_concurrent_requirement_updates_cannot_overwrite_each_other(client):
    _, episode = episode_only(client)
    repo, eid = client.app.state.repo, episode['id']
    barrier = threading.Barrier(2)

    def append(index):
        barrier.wait(timeout=5)
        try:
            with repo.transaction() as db:
                current = repo.owned(db, Episode, eid, 'owner')
                result = guidance.record(repo, db, current, revision=0,
                                         client_id=f'concurrent-{index}', text=f'需求{index}')
                return result['revision']
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, (1, 2)))
    assert sorted(map(str, results)) == ['1', 'DIRECTOR_REVISION_CONFLICT']
    current = episode_state(client, eid)['director']
    assert current['notes'] in ('需求1', '需求2') and len(current['messages']) == 4


def test_cumulative_notes_and_compiled_background_never_silently_truncate(client):
    auth, episode = episode_only(client)
    eid = episode['id']
    send(client, auth, eid, 0, text='甲' * 1000)
    full = send(client, auth, eid, 1, text='乙' * 499)
    assert len(full['notes']) == 1500
    error = send(client, auth, eid, 2, text='丙', expected=422)
    assert error['error']['code'] == 'DIRECTOR_NOTES_TOO_LONG'
    assert episode_state(client, eid)['director'] == full
    auth, long = episode_only(client, '原' * 4000)
    error = send(client, auth, long['id'], 0, choice='auto', expected=422)
    assert error['error']['code'] == 'DIRECTOR_CONTEXT_TOO_LONG'
    assert episode_state(client, long['id'])['director'] == long['director']
    with client.app.state.repo.factory() as db:
        assert guidance.compiled_background(db, db.get(Episode, long['id'])) == '原' * 4000


def test_bounded_history_rejects_new_requests_but_preserves_retry_idempotency(client):
    auth, episode = episode_only(client)
    for revision in range(49):
        full = send(client, auth, episode['id'], revision, choice='auto')
    assert len(full['messages']) == 100
    error = send(client, auth, episode['id'], 49, choice='warm', expected=409)
    assert error['error']['code'] == 'DIRECTOR_HISTORY_FULL'
    assert send(client, auth, episode['id'], 0, choice='auto') == full
    assert episode_state(client, episode['id'])['director'] == full


def test_director_message_reuses_owner_csrf_and_retention_guards(client):
    auth, episode = episode_only(client)
    eid = episode['id']
    assert send(client, {}, eid, 0, choice='auto', expected=403)['error']['code'] == 'CSRF_FAILED'
    with client.app.state.repo.transaction() as db:
        foreign = Episode(owner_id='other-owner', title='另一人的节目', background='私密')
        db.add(foreign); db.flush()
        foreign_id = foreign.id
    assert send(client, auth, foreign_id, 0, choice='auto', expected=404)['error']['code'] == 'NOT_FOUND'
    assert client.get(BASE + '/episodes/' + foreign_id).status_code == 404
    with client.app.state.repo.transaction() as db:
        db.get(Retention, eid).expires_at = time.time() - 1
    assert send(client, auth, eid, 0, choice='auto', expected=410)['error']['code'] == 'EPISODE_EXPIRED'


def test_requirements_reach_existing_director_call_and_only_stale_proposals(client, services, voiced_video_bytes, monkeypatch):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyzed = analyze_and_review(client, auth, eid)
    received = []
    original = services.directions

    async def capture(evidence, assets, background, reservation, permit):
        received.append(background)
        return await original(evidence, assets, background, reservation, permit)

    monkeypatch.setattr(services, 'directions', capture)
    legacy = request(client, f'/episodes/{eid}/directions', auth)
    advance(client, legacy['id'])
    current = episode_state(client, eid)
    assert received[-1] == current['background']
    assert current['director']['directions_current']
    version = request(client, f'/episodes/{eid}/direction', auth, expected=201,
                      body={'revision': current['evidence_revision'], 'direction_id': current['directions'][0]['id'],
                            'packaging': 'original'})
    send(client, auth, eid, 0, choice='warm')
    send(client, auth, eid, 1, text='保留现场笑声')
    stale = episode_state(client, eid)
    assert stale['evidence'] == analyzed['evidence'] and stale['evidence_revision'] == analyzed['evidence_revision']
    assert stale['versions'] == [version] and not stale['director']['directions_current']
    assert stale['retention']['expires_at'] == current['retention']['expires_at']
    denied = request(client, f'/episodes/{eid}/direction', auth, expected=409,
                     body={'revision': stale['evidence_revision'], 'direction_id': stale['directions'][0]['id']})
    assert denied['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    repo = client.app.state.repo
    with repo.factory() as db:
        assert source_segments(repo, db, db.get(Version, version['id']))
    newer = request(client, f'/episodes/{eid}/directions', auth)
    with repo.factory() as db:
        assert db.get(Job, newer['id']).payload['brief_revision'] == 2
    advance(client, newer['id'])
    compiled = json.loads(received[-1])
    assert compiled['original_background'] == current['background']
    assert compiled['director_brief']['focus'] == 'warm'
    assert compiled['director_brief']['notes'] == '保留现场笑声'
    latest = episode_state(client, eid)
    assert latest['director']['directions_current']
    # The model reused the direction ID and the evidence revision did not change.
    # An old tab must not accidentally select this freshly generated proposal.
    assert latest['directions'][0]['id'] == current['directions'][0]['id']
    selection = {'revision': latest['evidence_revision'],
                 'direction_id': latest['directions'][0]['id'], 'packaging': 'original'}
    for obsolete in (None, 0, 1, 99):
        body = {**selection, **({'brief_revision': obsolete} if obsolete is not None else {})}
        denied = request(client, f'/episodes/{eid}/direction', auth, expected=409, body=body)
        assert denied['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    assert episode_state(client, eid)['versions'] == [version]
    selected = request(client, f'/episodes/{eid}/direction', auth, expected=201,
                       body={**selection, 'brief_revision': 2})
    assert selected['id'] != version['id']


@pytest.mark.parametrize('legacy_payload', [False, True])
def test_queued_old_brief_is_not_called_or_retried(client, services, voiced_video_bytes, legacy_payload):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    job = request(client, f'/episodes/{eid}/directions', auth)
    if legacy_payload:
        with client.app.state.repo.transaction() as db:
            row = db.get(Job, job['id'])
            row.payload = {key: value for key, value in row.payload.items() if key != 'brief_revision'}
    send(client, auth, eid, 0, choice='conversation')
    duplicate = request(client, f'/episodes/{eid}/directions', auth, expected=409)
    assert duplicate['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    denied = request(client, f'/jobs/{job["id"]}/retry', auth, expected=409)
    assert denied['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    assert services.counts['directions'] == 0


def test_legacy_job_without_brief_revision_runs_when_no_guidance_exists(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    job = request(client, f'/episodes/{eid}/directions', auth)
    with client.app.state.repo.transaction() as db:
        row = db.get(Job, job['id'])
        row.payload = {key: value for key, value in row.payload.items() if key != 'brief_revision'}
    advance(client, job['id'])
    assert services.counts['directions'] == 1
    assert episode_state(client, eid)['director']['directions_current']


@pytest.mark.parametrize('when', ['before_permit', 'after_result'])
def test_inflight_requirement_change_never_publishes_old_result_and_retains_billing(client, services, voiced_video_bytes, monkeypatch, when):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    original = services.directions
    repo = client.app.state.repo

    def update_requirement():
        with repo.transaction() as db:
            guidance.record(repo, db, db.get(Episode, eid), revision=0,
                            client_id='inflight-test', text='改为保留完整对话')

    async def changing_adapter(evidence, assets, background, reservation, permit):
        if when == 'before_permit':
            update_requirement()
        result = await original(evidence, assets, background, reservation, permit)
        if when == 'after_result':
            update_requirement()
        return result

    monkeypatch.setattr(services, 'directions', changing_adapter)
    job = request(client, f'/episodes/{eid}/directions', auth)
    failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    assert episode_state(client, eid)['directions'] == []
    with repo.factory() as db:
        call = db.scalars(select(ModelCall).where(ModelCall.job_id == job['id'])).one()
        ledger = db.get(Budget, call.reservation_id)
        assert ledger.state == 'settled'
        assert ledger.cost == (0 if when == 'before_permit' else 20000)
        assert services.counts['directions'] == (0 if when == 'before_permit' else 1)
    denied = request(client, f'/jobs/{job["id"]}/retry', auth, expected=409)
    assert denied['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'


@pytest.mark.parametrize('mode', ['manual', 'expiry'])
def test_episode_deletion_removes_requirement_body_and_idempotency_history(client, mode):
    auth, episode = episode_only(client)
    eid, repo = episode['id'], client.app.state.repo
    send(client, auth, eid, 0, text='只存在本期需求表中的私密文字')
    if mode == 'manual':
        request(client, f'/episodes/{eid}?confirm=true', auth, method='DELETE')
    else:
        with repo.transaction() as db:
            db.get(Retention, eid).expires_at = time.time() - 1
        retention.maintain(repo)
    with repo.factory() as db:
        assert db.get(DirectorGuidance, eid) is None
        assert db.get(Episode, eid).background == ''
        assert not list(db.scalars(select(Audit).where(Audit.target_id == eid,
                                                       Audit.action == 'director_requirement_recorded')))
    assert send(client, auth, eid, 1, text='删除后不能写入', expected=410)['error']['code'] == 'EPISODE_EXPIRED'
