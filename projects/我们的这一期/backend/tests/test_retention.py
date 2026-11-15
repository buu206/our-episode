"""Use isolated temporary data only; exercise actual local deletion and recovery."""
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Asset, Budget, Episode, Job, Version, uid
from app.repository import Repository
from app.retention_models import Retention
from app.services import retention
from test_api import settings, client, headers, episode, asset, video_bytes, login, create_episode
from test_collaboration import h


def delete_episode(client, headers, eid):
    return client.delete(f'/api/v1/episodes/{eid}?confirm=true', headers=headers)


def test_new_episode_fixed_30_days_and_restart_does_not_renew(client, episode, settings):
    deadline = episode['retention']['expires_at']
    assert abs(deadline - time.time() - 30 * 86400) < 3
    other = Repository(settings.data_dir)
    try:
        with other.factory() as db:
            assert db.get(Retention, episode['id']).expires_at == deadline
    finally:
        other.engine.dispose()


def test_legacy_episode_gets_full_grace_once(tmp_path):
    repo = Repository(tmp_path)
    with repo.transaction() as db:
        old = Episode(owner_id='owner', title='old', created_at='2020-01-01T00:00:00+00:00')
        db.add(old); db.flush(); eid = old.id
    retention.enroll_existing(repo)
    with repo.factory() as db:
        deadline = db.get(Retention, eid).expires_at
    assert abs(deadline - time.time() - 30 * 86400) < 3
    retention.enroll_existing(repo)
    with repo.factory() as db:
        assert db.get(Retention, eid).expires_at == deadline


def test_delete_requires_confirmation_csrf_and_ownership(client, headers, episode):
    eid = episode['id']
    assert client.delete(f'/api/v1/episodes/{eid}', headers=headers).status_code == 409
    assert delete_episode(client, {}, eid).status_code == 403
    other_headers = login(client, 'second-test')
    assert delete_episode(client, other_headers, eid).status_code == 404
    assert client.get(f'/api/v1/episodes/{eid}/retention').status_code == 404


def test_delete_clears_all_content_and_preserves_budget(client, headers, episode, asset):
    repo, eid = client.app.state.repo, episode['id']
    with repo.transaction() as db:
        source = repo.path(db.get(Asset, asset['id']).path)
        version = Version(owner_id='alice', episode_id=eid, title='私密标题',
                          segments=[{'asset_id': asset['id'], 'start': 0, 'end': .3, 'caption': '私密花字'}],
                          narration_text='私密旁白', output_path='outputs/' + uid() + '.mp4')
        db.add(version); db.flush()
        output = repo.path(version.output_path); output.write_bytes(b'private video')
        db.add(Budget(owner_id='alice', episode_id=eid, amount=100, cost=None, state='unknown'))
        ep = db.get(Episode, eid); ep.evidence = [{'private': '私密转写'}]
    assert delete_episode(client, headers, eid).status_code == 200
    assert not source.exists() and not output.exists()
    assert client.get(f'/api/v1/episodes/{eid}').status_code == 410
    assert client.get(f'/api/v1/assets/{asset["id"]}/media').status_code == 404
    assert client.get('/api/v1/episodes').json()['items'] == []
    state = client.get(f'/api/v1/episodes/{eid}/retention').json()
    assert state['deletion_requested'] and not state['cleanup_pending']
    with repo.factory() as db:
        ep = db.get(Episode, eid)
        assert ep.title == '已删除节目' and ep.background == '' and ep.evidence == []
        assert not list(db.scalars(select(Version).where(Version.episode_id == eid)))
        assert repo.budget(db, eid)['total_reserved'] == .0001
    assert delete_episode(client, headers, eid).status_code == 200


def test_expiry_blocks_before_sweeper_and_automatically_purges(client, headers, episode, asset):
    repo, eid = client.app.state.repo, episode['id']
    with repo.transaction() as db:
        db.get(Retention, eid).expires_at = time.time() - 1
    assert client.get(f'/api/v1/episodes/{eid}').status_code == 410
    assert client.get(f'/api/v1/assets/{asset["id"]}/media').status_code == 410
    assert client.post(f'/api/v1/episodes/{eid}/analysis', headers=headers).status_code == 410
    retention.maintain(repo)
    with repo.factory() as db:
        row = db.get(Retention, eid)
        assert row.reason == 'expired' and row.completed_at is not None


def test_cleanup_failure_is_durable_and_retryable(client, headers, episode, asset, monkeypatch):
    repo, eid = client.app.state.repo, episode['id']
    with repo.factory() as db:
        source = repo.path(db.get(Asset, asset['id']).path)
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == source:
            raise PermissionError('test only')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        assert delete_episode(client, headers, eid).status_code == 200
        assert client.get(f'/api/v1/episodes/{eid}/retention').json()['cleanup_pending']
        assert source.exists()
    retention.maintain(repo)
    assert not source.exists()
    assert not client.get(f'/api/v1/episodes/{eid}/retention').json()['cleanup_pending']


def test_inflight_work_is_revoked_then_purged_after_settlement(client, headers, episode, asset):
    repo, eid = client.app.state.repo, episode['id']
    with repo.transaction() as db:
        job = Job(owner_id='alice', episode_id=eid, kind='directions', state='running',
                  idempotency_key=uid(), payload={'steps': {'private': 'private output'}})
        db.add(job); db.flush(); jid = job.id
    assert delete_episode(client, headers, eid).status_code == 200
    assert client.get(f'/api/v1/episodes/{eid}/retention').json()['cleanup_pending']
    assert client.get(f'/api/v1/jobs/{jid}').status_code == 410
    with repo.transaction() as db:
        db.get(Job, jid).state = 'failed'
    retention.maintain(repo)
    with repo.factory() as db:
        assert db.get(Job, jid).payload == {}
        assert db.get(Retention, eid).completed_at is not None


def test_symlink_deletes_link_not_external_source(client, headers, episode, asset, tmp_path):
    repo = client.app.state.repo
    outside = tmp_path / 'source-original.mp4'; outside.write_bytes(b'keep original')
    with repo.factory() as db:
        path = repo.path(db.get(Asset, asset['id']).path)
    path.unlink(); path.symlink_to(outside)
    assert delete_episode(client, headers, episode['id']).status_code == 200
    assert not path.is_symlink() and outside.read_bytes() == b'keep original'


def test_duplicate_reference_never_deletes_other_episode_file(client, headers, episode, asset):
    other = create_episode(client, headers, '另一期')
    repo = client.app.state.repo
    with repo.transaction() as db:
        original = db.get(Asset, asset['id'])
        path = repo.path(original.path)
        db.add(Asset(owner_id='alice', episode_id=other['id'], filename='keep', path=original.path,
                     size=1, duration=1, width=1, height=1, has_audio=False))
    assert delete_episode(client, headers, episode['id']).status_code == 200
    assert path.exists()
    assert client.get(f'/api/v1/episodes/{episode["id"]}/retention').json()['cleanup_pending']
    assert client.get(f'/api/v1/episodes/{other["id"]}').status_code == 200


def test_collaboration_private_text_media_revisions_and_sessions_are_removed(h):
    from app.collaboration_models import Submission, ExpressionRevision, Participant, ParticipantSession
    invite, person_auth, submission = h.submitted()
    h.adopt(submission)
    with h.repo.factory() as db:
        paths = retention._references(h.repo, db, db.get(Episode, h.episode))
    retention.request_deletion(h.repo, h.episode, 'alice')
    assert h.client.get('/api/v1/participant/submissions').status_code == 410
    assert h.client.post('/api/v1/participant/auth/login', json={
        'invite_code': invite['invite_code'], 'display_name': '本人'}).status_code == 410
    retention.purge(h.repo, h.episode)
    with h.repo.factory() as db:
        for model in (Submission, ExpressionRevision, Participant, ParticipantSession):
            assert list(db.scalars(select(model))) == []
    assert all(not (h.repo.data_dir / path).exists() for path, _ in paths)


def test_expiry_closes_existing_viewer_and_blocks_idempotent_share_replay(h):
    from test_sharing import saved_version, create, viewer
    from app.services.sharing import stream_is_active
    version, _ = saved_version(h)
    share, body = create(h, version)
    viewer(h, share)
    with h.repo.transaction() as db:
        db.get(Retention, h.episode).expires_at = time.time() - 1
    assert not stream_is_active(h.repo, share['id'], share['revision'])
    assert h.client.get(f'/api/v1/viewer/shares/{share["id"]}').status_code == 410
    assert h.client.post(f'/api/v1/versions/{version}/shares', headers=h.owner, json=body).status_code == 410
    retention.maintain(h.repo)
    assert h.client.get(f'/api/v1/viewer/shares/{share["id"]}').status_code == 401


def test_delete_preserves_provider_usage_but_erases_nested_content(client, headers, episode):
    from app.models import ModelCall
    repo, eid = client.app.state.repo, episode['id']
    with repo.transaction() as db:
        job = Job(owner_id='alice', episode_id=eid, kind='analysis', state='succeeded', idempotency_key=uid())
        budget = Budget(owner_id='alice', episode_id=eid, amount=1000, state='unknown')
        db.add_all([job, budget]); db.flush()
        call = ModelCall(owner_id='alice', episode_id=eid, job_id=job.id, reservation_id=budget.id,
            kind='vision', state='failed', request_digest='content-hash', usage={
                'provider': 'deepseek', 'content': 'private text', 'requests': [{
                    'request_id': 'provider-1', 'model_version': 'DeepSeek-V4.1-Flash-1017',
                    'cache_hit_tokens': 12, 'content': 'private nested text'}]})
        db.add(call); db.flush(); cid = call.id
    assert delete_episode(client, headers, eid).status_code == 200
    with repo.factory() as db:
        call = db.get(ModelCall, cid)
        assert call.request_digest is None
        assert call.usage == {'provider': 'deepseek', 'requests': [{
            'request_id': 'provider-1', 'model_version': 'DeepSeek-V4.1-Flash-1017', 'cache_hit_tokens': 12}]}
