"""Public-trial permission is explicit, episode-bound and revocable."""
import json
from sqlalchemy import select
import pytest
from app.models import Job, ModelCall, Version
from app.services import review_grant
from app.services.material_trial import GRANT_FILE
from test_visual_review import rendered, transport, run
from test_workflow_access import client, post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline


def authorize(rendered):
    c, repo, legacy, vid, path = rendered
    grant = {'scope': 'public-fixture-output-review-v1', 'episode_id': legacy['episode_id'],
        'owner_id': legacy['owner_id'], 'providers': ['deepseek'],
        'operations': ['visual_review', 'sequence_review'], 'task_limit_yuan': 3,
        'total_limit_yuan': 30, 'retention_verified': False,
        'approval': 'synthetic authorized fixture', 'approved_at': '2026-11-18',
        'assets': [{**a, 'source_url': 'https://example.com/synthetic-fixture'} for a in legacy['assets']]}
    destination = review_grant.grant_path(repo.data_dir, legacy['episode_id'])
    destination.write_text(json.dumps(grant))
    # Demonstrate there is no dependency on the first private three-video grant.
    (repo.data_dir / GRANT_FILE).unlink()
    return grant, destination


def test_public_review_runs_once_and_preserves_confirmation(rendered, transport):
    c, repo, legacy, vid, path = rendered
    grant, destination = authorize(rendered)
    with repo.transaction() as db:
        v = db.get(Version, vid); v.state = 'confirmed'; v.confirmed_at = '2026-11-18T00:00:00+00:00'
    job = post(c, f'/versions/{vid}/visual-review')
    run(repo)
    with repo.factory() as db:
        assert db.get(Job, job['id']).state == 'succeeded'
        assert db.get(Version, vid).state == 'confirmed'
        assert len(list(db.scalars(select(ModelCall)))) == 1
    repo.recover()
    assert post(c, f'/versions/{vid}/visual-review')['id'] == job['id']
    assert len(transport['calls']) == 1


@pytest.mark.parametrize('change', ['owner', 'episode', 'provider', 'operation', 'approval', 'source', 'revoked', 'symlink'])
def test_changed_public_permission_blocks_before_provider(rendered, transport, change):
    c, repo, legacy, vid, path = rendered
    grant, destination = authorize(rendered)
    job = post(c, f'/versions/{vid}/visual-review')
    if change == 'owner': grant['owner_id'] = 'someone-else'
    elif change == 'episode': grant['episode_id'] = 'f'*32
    elif change == 'provider': grant['providers'] = ['other']
    elif change == 'operation': grant['operations'].append('publish')
    elif change == 'approval': grant['approval'] = ''
    elif change == 'source': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'changed')
    if change == 'revoked': destination.unlink()
    elif change == 'symlink':
        moved = destination.with_suffix('.backup'); destination.rename(moved); destination.symlink_to(moved)
    else: destination.write_text(json.dumps(grant))
    run(repo)
    assert not transport['calls']
    with repo.factory() as db:
        assert db.get(Job, job['id']).state == 'failed'
        assert not list(db.scalars(select(ModelCall)))


def test_public_url_alone_never_grants_permission(rendered):
    c, repo, legacy, vid, path = rendered
    grant, destination = authorize(rendered)
    destination.write_text(json.dumps({'assets': grant['assets']}))
    response = c.post(f'/api/v1/versions/{vid}/sequence-review', json={'start': 1, 'end': 3})
    assert response.status_code == 409 and response.json()['error']['code'] == 'GRANT_INVALID'


def test_frame_grant_cannot_authorize_audio_upload(rendered):
    from app.services import audio_review
    from app.repository import DomainError
    c, repo, legacy, vid, path = rendered
    authorize(rendered)
    with repo.factory() as db:
        with pytest.raises(DomainError) as error:
            audio_review.snapshot(repo, db, db.get(Version, vid))
        assert error.value.code == 'REVIEW_NOT_AUTHORIZED'
