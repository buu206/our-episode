"""Synthetic local files + simulated provider HTTP, never real-model evidence."""
import json
from copy import deepcopy
import httpx
import pytest
from sqlalchemy import select
from app.models import Asset, Budget, Job, ModelCall, Version
from app.services import visual_review as vr
from app.services.director import DirectorError
from app.services.pipeline import ModelServices, Pipeline
from app.services.material_trial import GRANT_FILE
from app.worker import Worker
from test_workflow_access import client, post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_new_providers import envelope


@pytest.fixture
def rendered(client):
    c, repo, grant = client
    vid = 'b' * 32
    path = repo.data_dir / 'outputs' / (vid + '.mp4')
    path.write_bytes((repo.data_dir / grant['assets'][0]['path']).read_bytes())
    with repo.transaction() as db:
        db.add(Version(id=vid, owner_id=grant['owner_id'], episode_id=grant['episode_id'],
            title='合成测试片', state='ready', packaging='original', output_path='outputs/' + path.name,
            segments=[{'asset_id': grant['assets'][0]['id'], 'start': 0., 'end': 8., 'caption': ''}]))
    return c, repo, grant, vid, path


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'timeout': False, 'invalid': False, 'revoke': None}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(request)
        assert request.url.host == 'api.deepseek.com'
        content = json.loads(request.content)['messages'][0]['content']
        positions = [json.loads(c['text']) for c in content[1:] if c['type'] == 'text']
        assert 1 <= len(positions) <= 12
        assert len([c for c in content if c['type'] == 'image_url']) == len(positions)
        if state['timeout']:
            raise httpx.ReadTimeout('secret upstream details')
        result = {'summary': '抽样画面中有一处需要核对。', 'findings': [
            {'frame_id': 'missing' if state['invalid'] else positions[0]['frame_id'],
             'category': 'text', 'observation': '这行字靠近画面边缘。', 'suggestion': '可以向画面内移一点。'}]}
        if state['revoke']:
            state['revoke']()
        return httpx.Response(200, json=envelope(result))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def run(repo):
    Worker(repo, Pipeline(repo, ModelServices(KEYS))).process_one()


def test_actual_frames_bound_budget_idempotency_and_restore(rendered, transport):
    c, repo, grant, vid, path = rendered
    job = post(c, f'/versions/{vid}/visual-review')
    assert post(c, f'/versions/{vid}/visual-review')['id'] == job['id']
    run(repo)
    with repo.factory() as db:
        j = db.get(Job, job['id']); v = db.get(Version, vid)
        assert j.state == 'succeeded' and v.state == 'ready' and v.confirmed_at is None
        report = j.result
        assert report['scope'] == 'sampled_visual' and not report['audio_analyzed']
        assert report['findings'][0]['time'] in report['sample_times']
        assert report['not_checked'] and report['verification'] == 'unverified'
        assert 'jpeg' not in json.dumps(j.payload) and 'data:image' not in json.dumps(j.payload)
        call = db.scalars(select(ModelCall)).one()
        assert call.kind == 'visual_review' and call.state == 'succeeded'
        assert db.get(Budget, call.reservation_id).state == 'settled'
        assert vr.context(repo, db, v)['summary'] == report['summary']
    repo.recover()
    assert post(c, f'/versions/{vid}/visual-review')['id'] == job['id']
    assert c.get(f'/api/v1/versions/{vid}/export').status_code == 409
    assert len(transport['calls']) == 1
    path.write_bytes(path.read_bytes() + b'changed')
    with repo.factory() as db:
        assert vr.context(repo, db, db.get(Version, vid)) is None


@pytest.mark.parametrize('change', ['output', 'source', 'grant', 'owner', 'prompt'])
def test_changes_before_send_block_and_keep_candidate(rendered, transport, monkeypatch, tmp_path, change):
    c, repo, grant, vid, path = rendered
    job = post(c, f'/versions/{vid}/visual-review')
    if change == 'output': path.write_bytes(path.read_bytes() + b'changed')
    elif change == 'source': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'changed')
    elif change == 'grant': (repo.data_dir / GRANT_FILE).unlink()
    elif change == 'prompt':
        prompt = tmp_path / 'prompt.txt'; prompt.write_text('changed'); monkeypatch.setattr(vr, 'PROMPT', prompt)
    else:
        with repo.transaction() as db: db.get(Version, vid).owner_id = 'other'
    run(repo)
    assert not transport['calls']
    with repo.factory() as db:
        assert db.get(Job, job['id']).state == 'failed'
        assert db.get(Version, vid).state == 'ready'
        assert not list(db.scalars(select(ModelCall)))


@pytest.mark.parametrize('mode', ['invalid', 'timeout', 'revoked'])
def test_failed_paid_review_never_destroys_or_repeats_candidate(rendered, transport, mode):
    c, repo, grant, vid, path = rendered
    job = post(c, f'/versions/{vid}/visual-review')
    if mode == 'revoked':
        def revoke():
            with repo.transaction() as db: db.get(Asset, grant['assets'][0]['id']).deleted = True
        transport['revoke'] = revoke
    else: transport[mode] = True
    run(repo)
    with repo.factory() as db:
        assert db.get(Job, job['id']).state == 'failed'
        assert db.get(Job, job['id']).result is None
        assert db.get(Version, vid).state == 'ready'
        call = db.scalars(select(ModelCall)).one()
        assert db.get(Budget, call.reservation_id).state == ('unknown' if mode == 'timeout' else 'settled')
    if mode != 'revoked':
        assert post(c, f'/versions/{vid}/visual-review')['id'] == job['id']
    assert len(transport['calls']) == 1


def test_ownership_csrf_and_budget_guard(rendered, transport):
    c, repo, grant, vid, path = rendered
    csrf = c.headers.pop('X-CSRF-Token')
    assert c.post(f'/api/v1/versions/{vid}/visual-review').status_code == 403
    c.headers['X-CSRF-Token'] = csrf
    with repo.transaction() as db:
        db.add(Budget(owner_id=grant['owner_id'], episode_id=grant['episode_id'], amount=3000000, cost=3000000, state='settled'))
    job = post(c, f'/versions/{vid}/visual-review'); run(repo)
    assert not transport['calls']
    with repo.factory() as db: assert db.get(Job, job['id']).state == 'failed'
    login = c.post('/api/v1/auth/login', json={'invite_code': 'other'})
    c.headers['X-CSRF-Token'] = login.json()['csrf_token']
    assert c.post(f'/api/v1/versions/{vid}/visual-review').status_code == 404


@pytest.mark.parametrize('patch', [{'findings': [{'frame_id': 'f99', 'category': 'effect', 'observation': 'x', 'suggestion': 'y'}]},
                                  {'summary': ' '}, {'approved': True}])
def test_schema_rejects_invented_position_blank_or_approval(patch):
    result = {'summary': '抽样观察', 'findings': [], **deepcopy(patch)}
    with pytest.raises(DirectorError): vr.parse(json.dumps(result), [{'frame_id': 'f1'}])
