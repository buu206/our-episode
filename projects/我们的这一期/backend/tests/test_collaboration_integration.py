from app.models import Version
from test_collaboration import h, BASE


def test_owner_can_save_safe_replacement_from_disputed_version(h):
    invitation = h.invitation('review')
    auth = h.participant(invitation)
    payload = {'title': '原版本', 'segments': [
        {'asset_id': h.source, 'start': 0, 'end': 5, 'caption': ''}]}
    original = h.client.post(BASE + f'/episodes/{h.episode}/versions',
                             headers=h.owner, json=payload).json()
    assert h.client.post(BASE + '/participant/objections', headers=auth,
                         json={'start': 1, 'end': 3, 'reason': '需要移除这段'}).status_code == 201
    payload['base_version_id'] = original['id']
    rejected = h.client.post(BASE + f'/episodes/{h.episode}/versions', headers=h.owner, json=payload)
    assert rejected.status_code == 409
    assert rejected.json()['error']['code'] == 'COLLAB_RESTRICTED'
    payload['segments'][0].update(start=4, end=5)
    replacement = h.client.post(BASE + f'/episodes/{h.episode}/versions', headers=h.owner, json=payload)
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()['state'] == 'draft'
    with h.repo.factory() as db:
        assert db.get(Version, original['id']).state == 'restricted'


def test_disputed_clone_exception_does_not_allow_stale_evidence(h):
    payload = {'title': '原版本', 'segments': [
        {'asset_id': h.source, 'start': 0, 'end': 5, 'caption': ''}]}
    original = h.client.post(BASE + f'/episodes/{h.episode}/versions', headers=h.owner, json=payload).json()
    with h.repo.transaction() as db:
        version = db.get(Version, original['id'])
        version.state = 'restricted'
        version.error = {'code': 'EVIDENCE_CHANGED', 'message': '依据失效'}
    payload['base_version_id'] = original['id']
    response = h.client.post(BASE + f'/episodes/{h.episode}/versions', headers=h.owner, json=payload)
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'VERSION_UNAVAILABLE'
