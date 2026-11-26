from copy import deepcopy
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import create_app
from app.models import Job, Version
from app.worker import Worker
from test_api import settings, client, headers, episode, asset, video_bytes, login, BASE


def prepare(client, headers, episode, asset):
    body = {'title': '三步试剪', 'packaging': 'variety_v2', 'segments': [{
        'asset_id': asset['id'], 'start': 0., 'end': .5, 'caption': '', 'visual_beats': [{
            'kind': 'headline', 'source_time': 0., 'duration': .5, 'text': '稳住',
            'target': {'x': .1, 'y': .1, 'width': .3, 'height': .3}, 'sound': 'none',
        }]}]}
    r = client.post(BASE + f"/episodes/{episode['id']}/versions", headers=headers, json=body)
    assert r.status_code == 201, r.text
    vid = r.json()['id']
    client.post(BASE + f'/versions/{vid}/render', headers=headers)
    Worker(client.app.state.repo).process_one()
    path = BASE + f'/versions/{vid}/finishing'
    r = client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    return vid, path, r.json()


def save(client, headers, path, state, action='next'):
    r = client.put(path, headers=headers, json={'revision': state['revision'], 'draft': state['draft'], 'action': action})
    assert r.status_code == 200, r.text
    return r.json()


def test_three_steps_restore_commit_render_without_acceptance(client, headers, episode, asset, settings):
    vid, path, state = prepare(client, headers, episode, asset)
    state['draft']['segments'][0]['visual_beats'][0]['text'] = '拿到了'
    state = save(client, headers, path, state)
    assert state['step'] == 2 and state['completed'] == 1
    # A newly constructed app/repository reads the saved checkpoint.
    with TestClient(create_app(settings)) as other:
        restored = other.get(path, headers=login(other)).json()
        assert restored == state
    state['draft']['segments'][0]['visual_beats'][0]['sound'] = 'wood'
    state = save(client, headers, path, state)
    state['draft']['segments'][0]['frame_accents'] = [{'source_time': 0., 'duration': .5, 'palette': 'coral'}]
    state = save(client, headers, path, state)
    assert state['completed'] == 3
    r = client.post(path + '/commit', headers=headers, json={'revision': state['revision']})
    assert r.status_code == 200, r.text
    made = r.json()
    again = client.post(path + '/commit', headers=headers, json={'revision': state['revision']}).json()
    assert again == made and made['id'] != vid and made['state'] == 'draft'
    client.post(BASE + f"/versions/{made['id']}/render", headers=headers)
    Worker(client.app.state.repo).process_one()
    with client.app.state.repo.factory() as db:
        v = db.get(Version, made['id'])
        assert v.state == 'ready' and v.confirmed_at is None
        assert v.segments[0]['visual_beats'][0]['text'] == '拿到了'
        assert db.get(Version, vid).segments[0]['visual_beats'][0]['text'] == '稳住'
        assert db.scalar(select(func.count()).select_from(Version)) == 2
        assert not db.scalar(select(Job).where(Job.kind == 'narration'))
    assert client.get(BASE + f"/versions/{made['id']}/export", headers=headers).status_code == 409


def test_conflict_cut_injection_phase_guard_and_no_phantom_versions(client, headers, episode, asset):
    vid, path, state = prepare(client, headers, episode, asset)
    assert client.post(path + '/commit', headers=headers, json={'revision': 0}).status_code == 409
    bad = deepcopy(state); bad['draft']['segments'][0]['end'] = .49
    r = client.put(path, headers=headers, json={'revision': 0, 'action': 'next', 'draft': bad['draft']})
    assert r.status_code == 422
    bad = deepcopy(state); bad['draft']['narration_text'] = '偷偷改旁白'
    assert client.put(path, headers=headers, json={'revision': 0, 'action': 'next', 'draft': bad['draft']}).status_code == 422
    new = save(client, headers, path, state)
    assert client.put(path, headers=headers, json={'revision': 0, 'action': 'next', 'draft': state['draft']}).status_code == 409
    new = save(client, headers, path, new, 'back')
    new['draft']['segments'][0]['visual_beats'][0]['text'] = '再来'
    new = save(client, headers, path, new, 'save')
    assert new['completed'] == 0
    with client.app.state.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1
    other = login(client, 'second-test')
    assert client.get(path, headers=other).status_code == 404
    assert client.put(path, headers=other, json={'revision': new['revision'], 'action': 'save', 'draft': new['draft']}).status_code == 404


def test_invalid_effect_rolls_back_session_and_base(client, headers, episode, asset):
    _, path, state = prepare(client, headers, episode, asset)
    state['draft']['segments'][0]['visual_beats'][0]['text'] = ''
    assert client.put(path, headers=headers, json={'revision': 0, 'action': 'next', 'draft': state['draft']}).status_code == 422
    assert client.get(path, headers=headers).json()['revision'] == 0


def test_finishing_reuses_exact_voice_but_changed_text_requires_approval(client, headers, episode, asset):
    import subprocess
    from app.services import media
    vid, path, state = prepare(client, headers, episode, asset)
    repo = client.app.state.repo
    voice = repo.data_dir / 'outputs' / ('e' * 32 + '.wav')
    subprocess.run([str(media._binary('ffmpeg')), '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=24000', '-t', '1', str(voice)], check=True)
    with repo.transaction() as db:
        base = db.get(Version, vid)
        base.narration_text = '测试旁白'
        base.narration_approved = True
        base.narration_path = 'outputs/' + 'e' * 32 + '.wav'
    state = client.get(path, headers=headers).json()
    state['draft']['segments'][0]['visual_beats'][0]['palette'] = 'coral'
    for _ in range(3):
        state = save(client, headers, path, state)
    first = client.post(path + '/commit', headers=headers, json={'revision': state['revision']})
    assert first.status_code == 200, first.text
    assert first.json()['narration_ready'] and first.json()['narration_approved']
    state['draft']['narration_text'] = '新的旁白'
    state = save(client, headers, path, state)
    second = client.post(path + '/commit', headers=headers, json={'revision': state['revision']}).json()
    assert second['id'] != first.json()['id'] and not second['narration_ready'] and not second['narration_approved']
    with repo.factory() as db:
        assert not db.scalar(select(Job).where(Job.kind == 'narration'))
