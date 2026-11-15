"""Local delegation: real FFmpeg, fake planning, no paid calls from the runner."""
from sqlalchemy import select
import pytest
from app.models import Asset, Job, Version
from app.services import variety_v2, director_run
from app.services.media import MediaError
from app.repository import DomainError
from app.worker import Worker
from test_director_original_draft import proposal
from test_pipeline import client, settings, services, no_network, voiced_video_bytes, request, episode_state


def create(client, content):
    auth, eid, body = proposal(client, content)
    version = request(client, f'/episodes/{eid}/direction', auth, body=body, expected=201)
    return auth, eid, version


def state(client, eid):
    return next(j for j in episode_state(client, eid)['jobs'] if j['kind'] == 'director_run')


def control(client, auth, vid, action, expected=200):
    return request(client, f'/versions/{vid}/director-run/{action}', auth, expected=expected)


def worker(client):
    return Worker(client.app.state.repo, client.app.state.pipeline)


def test_pause_queue_resume_complete_and_never_confirm(client, voiced_video_bytes, services):
    auth, eid, v = create(client, voiced_video_bytes)
    before = dict(services.counts)
    assert control(client, auth, v['id'], 'pause')['state'] == 'paused'
    assert control(client, auth, v['id'], 'pause')['state'] == 'paused'
    worker(client).process_one()
    assert state(client, eid)['state'] == 'paused'
    assert request(client, f"/versions/{v['id']}/render", auth, expected=409)['error']['code'] == 'DIRECTOR_RUN_ACTIVE'
    client.app.state.repo.recover()
    assert control(client, auth, v['id'], 'resume')['state'] == 'active'
    worker(client).process_one()
    assert state(client, eid)['result']['phase'] == 'technical_review'
    worker(client).process_one()
    assert state(client, eid)['state'] == 'awaiting_user'
    assert dict(services.counts) == before
    with client.app.state.repo.factory() as db:
        saved = db.get(Version, v['id'])
        assert saved.state == 'ready' and saved.confirmed_at is None
    assert client.get(f"/api/v1/versions/{v['id']}/export").status_code == 409


@pytest.mark.parametrize('action,expected', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_inflight_action_finishes_current_render_without_starting_review(client, voiced_video_bytes, monkeypatch, action, expected):
    auth, eid, v = create(client, voiced_video_bytes)
    original = variety_v2.render_variety
    def rendering(*args, **kwargs):
        response = control(client, auth, v['id'], action)
        assert response['state'] == ('pausing' if action == 'pause' else 'cancelling')
        return original(*args, **kwargs)
    monkeypatch.setattr(variety_v2, 'render_variety', rendering)
    worker(client).process_one()
    assert state(client, eid)['state'] == expected
    assert not any(j['kind'] == 'technical_review' for j in episode_state(client, eid)['jobs'])
    with client.app.state.repo.factory() as db:
        saved = db.get(Version, v['id'])
        assert saved.state == 'ready' and client.app.state.repo.path(saved.output_path).exists()
    if action == 'pause':
        control(client, auth, v['id'], 'resume')
        worker(client).process_one()
        assert state(client, eid)['state'] == 'awaiting_user'


def test_pause_between_render_and_review_holds_review(client, voiced_video_bytes):
    auth, eid, v = create(client, voiced_video_bytes)
    worker(client).process_one()
    control(client, auth, v['id'], 'pause')
    worker(client).process_one()
    review = next(j for j in episode_state(client, eid)['jobs'] if j['kind'] == 'technical_review')
    assert review['state'] == 'paused'
    control(client, auth, v['id'], 'resume'); worker(client).process_one()
    assert state(client, eid)['state'] == 'awaiting_user'


def test_queued_cancellation_preserves_draft_and_requires_new_intent_to_restart(client, voiced_video_bytes):
    auth, eid, v = create(client, voiced_video_bytes)
    assert control(client, auth, v['id'], 'cancel')['state'] == 'cancelled'
    worker(client).process_one()
    assert control(client, auth, v['id'], 'cancel')['state'] == 'cancelled'
    control(client, auth, v['id'], 'resume', 409)
    with client.app.state.repo.factory() as db:
        saved = db.get(Version, v['id'])
        assert saved.state == 'draft' and saved.output_path is None
    # Explicit manual rendering remains available after ending delegation.
    request(client, f"/versions/{v['id']}/render", auth)
    worker(client).process_one()
    assert state(client, eid)['state'] == 'cancelled'


def test_transient_retry_is_bounded_and_pause_does_not_reset_limit(client, voiced_video_bytes, monkeypatch):
    auth, eid, v = create(client, voiced_video_bytes)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise MediaError('MEDIA_TIMEOUT', '合成超时')
    monkeypatch.setattr(variety_v2, 'render_variety', fail)
    worker(client).process_one()
    assert state(client, eid)['result']['retries_used'] == 1
    control(client, auth, v['id'], 'pause'); control(client, auth, v['id'], 'resume')
    worker(client).process_one(); worker(client).process_one()
    assert calls == [1, 1] and state(client, eid)['state'] == 'blocked'
    control(client, auth, v['id'], 'resume', 409)


def test_interrupted_render_reuses_same_job_and_cannot_repeat_paid_work(client, voiced_video_bytes, services):
    auth, eid, v = create(client, voiced_video_bytes)
    repo = client.app.state.repo; before = dict(services.counts)
    with repo.transaction() as db: db.get(Job, v['job_id']).state = 'running'
    repo.recover(); worker(client).process_one(); worker(client).process_one()
    assert state(client, eid)['state'] == 'awaiting_user'
    assert state(client, eid)['result']['retries_used'] == 1
    assert dict(services.counts) == before
    assert len([j for j in episode_state(client, eid)['jobs'] if j['kind'] == 'render']) == 1


@pytest.mark.parametrize('change', ['source', 'intent', 'expired'])
def test_source_intent_and_deadline_changes_stop_before_dispatch(client, voiced_video_bytes, change):
    auth, eid, v = create(client, voiced_video_bytes)
    repo = client.app.state.repo
    with repo.transaction() as db:
        version = db.get(Version, v['id']); run = director_run.find(db, version)
        if change == 'expired': run.payload = {**run.payload, 'expires_at': 1}
        elif change == 'intent': version.packaging = 'original'
        else: db.get(Asset, version.segments[0]['asset_id']).deleted = True
    worker(client).process_one()
    assert state(client, eid)['state'] == 'blocked'
    with repo.factory() as db:
        assert db.get(Job, v['job_id']).state == 'paused'
        assert db.get(Version, v['id']).output_path is None
    control(client, auth, v['id'], 'resume', 409)


def test_controls_enforce_owner_csrf_and_action_allowlist(client, voiced_video_bytes):
    auth, eid, v = create(client, voiced_video_bytes)
    control(client, {}, v['id'], 'pause', 403)
    control(client, auth, v['id'], 'approve', 422)
    with client.app.state.repo.transaction() as db:
        other = Version(owner_id='other', episode_id=eid, title='other', segments=[])
        db.add(other); db.flush(); other_id = other.id
    control(client, auth, other_id, 'pause', 404)


def test_check_enqueue_failure_does_not_kill_worker_or_destroy_output(client, voiced_video_bytes, monkeypatch):
    from app.services import render_review
    auth, eid, v = create(client, voiced_video_bytes)
    def fail(*args, **kwargs):
        raise DomainError('OUTPUT_MISSING', '输出暂不可读', 409)
    monkeypatch.setattr(render_review, 'enqueue', fail)
    worker(client).process_one()
    assert state(client, eid)['state'] == 'blocked'
    with client.app.state.repo.factory() as db:
        assert db.get(Version, v['id']).state == 'ready'


def test_paused_delegation_is_scrubbed_with_episode_retention(client, voiced_video_bytes):
    from app.services import retention
    auth, eid, v = create(client, voiced_video_bytes)
    control(client, auth, v['id'], 'pause')
    run_id = state(client, eid)['id']
    repo = client.app.state.repo
    retention.request_deletion(repo, eid, 'owner')
    worker(client).process_one()
    with repo.factory() as db:
        run = db.get(Job, run_id)
        assert run.state == 'failed' and run.payload == {} and run.result is None
