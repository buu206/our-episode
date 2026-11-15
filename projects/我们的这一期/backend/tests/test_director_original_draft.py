from sqlalchemy import select
from app.models import Budget, Job, ModelCall, Version
from app.worker import Worker
from test_pipeline import (client, settings, services, no_network, voiced_video_bytes,
    create_with_asset, analyze_and_review, request, advance, episode_state)


def proposal(client, content):
    auth, eid, _ = create_with_asset(client, content)
    analyze_and_review(client, auth, eid)
    job = request(client, f'/episodes/{eid}/directions', auth)
    advance(client, job['id'])
    state = episode_state(client, eid)
    body = {'direction_id': state['directions'][0]['id'], 'revision': state['directions_revision'],
            'packaging': 'variety_v2', 'auto_render_original': True, 'client_id': 'local-original-draft-001'}
    return auth, eid, body


def test_one_choice_runs_original_draft_and_review_without_tts_or_confirmation(client, voiced_video_bytes, services):
    auth, eid, body = proposal(client, voiced_video_bytes)
    before = dict(services.counts)
    version = request(client, f'/episodes/{eid}/direction', auth, body=body, expected=201)
    assert version['state'] == 'rendering' and version['narration_text'] == ''
    assert not version['narration_approved']
    repeated = request(client, f'/episodes/{eid}/direction', auth, body=body, expected=201)
    assert repeated['id'] == version['id'] and repeated['job_id'] == version['job_id']
    changed = {**body, 'packaging': 'original'}
    assert request(client, f'/episodes/{eid}/direction', auth, body=changed, expected=409)['error']['code'] == 'REQUEST_CONFLICT'
    advance(client, version['job_id'])
    state = episode_state(client, eid)
    check = next(j for j in state['jobs'] if j['kind'] == 'technical_review')
    advance(client, check['id'])
    assert dict(services.counts) == before
    with client.app.state.repo.factory() as db:
        saved = db.get(Version, version['id'])
        assert saved.state == 'ready' and saved.confirmed_at is None and not saved.narration_path
        assert db.get(Job, saved.job_id).payload['stop_at'] == 'preview'
        assert len(list(db.scalars(select(Version).where(Version.episode_id == eid)))) == 1
    client.app.state.repo.recover()
    assert request(client, f'/episodes/{eid}/direction', auth, body=body, expected=201)['id'] == version['id']
    assert client.get(f'/api/v1/versions/{version["id"]}/export').status_code == 409


def test_auto_draft_requires_stable_request_and_current_source_revision(client, voiced_video_bytes):
    auth, eid, body = proposal(client, voiced_video_bytes)
    assert request(client, f'/episodes/{eid}/direction', auth,
                   body={**body,'client_id':None}, expected=422)['error']['code'] == 'REQUEST_ID_REQUIRED'
    assert request(client, f'/episodes/{eid}/direction', auth,
                   body={**body,'revision':body['revision']+1}, expected=409)['error']['code'] == 'STALE_REVISION'
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(Version).where(Version.episode_id == eid)))
