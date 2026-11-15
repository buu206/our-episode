import subprocess
from sqlalchemy import select
import pytest
from app.models import Asset, Job, ModelCall, Version
from app.repository import DomainError
from app.services import media, render_review
from app.worker import Worker
from test_pipeline import (client, settings, services, no_network, voiced_video_bytes,
    create_with_asset, request, advance, episode_state)


def rendered(client, content):
    auth, eid, asset = create_with_asset(client, content)
    version = request(client, f'/episodes/{eid}/versions', auth, expected=201,
        body={'title': '原声测试', 'packaging': 'original',
              'segments': [{'asset_id': asset['id'], 'start': 0, 'end': 2, 'caption': ''}]})
    job = request(client, f'/versions/{version["id"]}/render', auth)
    advance(client, job['id'])
    return auth, eid, version['id'], asset['id']


def test_automatic_review_persists_actual_file_and_never_confirms(client, voiced_video_bytes):
    auth, eid, vid, _ = rendered(client, voiced_video_bytes)
    queued = next(j for j in episode_state(client, eid)['jobs'] if j['kind'] == 'technical_review')
    assert request(client, f'/versions/{vid}/review', auth)['id'] == queued['id']
    report = advance(client, queued['id'])['result']
    assert report['decoded_full_output'] and report['status'] == 'checked'
    assert report['audio']['measured_seconds'] >= 1.9
    assert report['audio']['rms_dbfs'] > -55
    assert report['not_checked'] and report['version_id'] == vid
    assert report['source_review']['scope'] == 'source_metadata'
    assert not report['source_review']['evidence_confirmed']
    repo = client.app.state.repo
    repo.recover()
    with repo.factory() as db:
        version = db.get(Version, vid)
        assert version.state == 'ready' and version.confirmed_at is None
        assert render_review.context(repo, db, version)['decoded_full_output']
        assert not list(db.scalars(select(ModelCall)))
        assert db.get(Job, queued['id']).result == report
        # Same path with different bytes must never pass a stale result to AI.
        with repo.path(version.output_path).open('ab') as stream:
            stream.write(b'changed')
        assert render_review.context(repo, db, version) is None
    refreshed = request(client, f'/versions/{vid}/review', auth)
    assert refreshed['state'] == 'queued' and refreshed['result'] is None


def test_review_failure_does_not_destroy_ready_output_and_can_retry(client, voiced_video_bytes, monkeypatch):
    auth, eid, vid, _ = rendered(client, voiced_video_bytes)
    queued = request(client, f'/versions/{vid}/review', auth)
    original = render_review.inspect_output
    def fail(*args, **kwargs):
        raise media.MediaError('MEDIA_TIMEOUT', '媒体处理超时。')
    monkeypatch.setattr(render_review, 'inspect_output', fail)
    advance(client, queued['id'], expected='failed')
    with client.app.state.repo.factory() as db:
        v = db.get(Version, vid)
        assert v.state == 'ready' and v.error is None and client.app.state.repo.path(v.output_path).exists()
    monkeypatch.setattr(render_review, 'inspect_output', original)
    assert request(client, f'/versions/{vid}/review', auth)['id'] == queued['id']
    advance(client, queued['id'])


def test_review_permissions_and_source_revocation(client, voiced_video_bytes):
    auth, eid, vid, aid = rendered(client, voiced_video_bytes)
    assert request(client, f'/versions/{vid}/review', {}, expected=403)['error']['code'] == 'CSRF_FAILED'
    with client.app.state.repo.transaction() as db:
        foreign = Version(owner_id='other', episode_id=eid, title='other', state='ready', segments=[])
        db.add(foreign); db.flush(); other = foreign.id
    assert request(client, f'/versions/{other}/review', auth, expected=404)['error']
    queued = request(client, f'/versions/{vid}/review', auth)
    with client.app.state.repo.transaction() as db:
        db.get(Asset, aid).deleted = True
    advance(client, queued['id'], expected='failed')
    assert request(client, f'/versions/{vid}/review', auth, expected=409)['error']


def test_revocation_during_inspection_cannot_publish_report(client, voiced_video_bytes, monkeypatch):
    auth, eid, vid, aid = rendered(client, voiced_video_bytes)
    job = request(client, f'/versions/{vid}/review', auth)
    original = render_review.inspect_output
    def revoke(*args, **kwargs):
        report = original(*args, **kwargs)
        with client.app.state.repo.transaction() as db:
            db.get(Asset, aid).deleted = True
        return report
    monkeypatch.setattr(render_review, 'inspect_output', revoke)
    result = advance(client, job['id'], expected='failed')
    assert result['result'] is None
    with client.app.state.repo.factory() as db:
        assert db.get(Version, vid).state == 'ready'


@pytest.mark.parametrize('audio', ['silent', 'absent'])
def test_real_silence_and_missing_track_are_flagged_without_quality_verdict(tmp_path, audio):
    path = tmp_path/'test.mp4'
    args = [media._binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=96x64:d=2']
    if audio == 'silent':
        args += ['-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-shortest']
    subprocess.run(args+['-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True, timeout=30)
    report = render_review.inspect_output(path, expect_audio=True)
    assert report['status'] == 'needs_attention' and report['issues']
    assert report['issues'][0]['code'] == ('QUIET_INTERVAL' if audio == 'silent' else 'AUDIO_MISSING')
    assert render_review.inspect_output(path, expect_audio=False)['issues'] == []
