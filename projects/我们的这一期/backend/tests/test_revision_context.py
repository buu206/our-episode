"""Replanning handoff over synthetic media, with no real model calls."""
import asyncio
from copy import deepcopy
import json
import httpx
import pytest
from app.models import Episode, Version
from app.repository import DomainError
from app.services import director_chat as chat, sequence_review as sequence
from app.services.director_chat_access import ScopedDirector
from app.services.director import DirectorError
from app.services import revision_context as revision
from test_visual_review import rendered
from test_workflow_access import client, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_new_providers import envelope


@pytest.fixture
def revision_case(rendered, monkeypatch):
    _, repo, grant, vid, path = rendered
    observations = {'scope': 'sampled_sequence', 'verification': 'unverified',
                    'windows': [{'window': {'start': 1., 'end': 3.}, 'audio_analyzed': False,
                                 'summary': '合成物体位置可能变化', 'not_checked': ['连续动作']} ]}
    monkeypatch.setattr(sequence, 'context', lambda *args: deepcopy(observations))
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        _, context = chat._context(repo, db, ep, [], '先保留完整话轮，调整开场信息。', vid, 'preview')
    with repo.transaction() as db:
        reservation = repo.reserve(db, grant['owner_id'], grant['episode_id'], .28)
    return repo, grant, vid, path, context, reservation, observations


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'after': None}
    original = httpx.AsyncClient
    def handler(request):
        state['calls'].append(json.loads(request.content))
        if state['after']: state['after']()
        return httpx.Response(200, json=envelope({'directions': [], 'uncertainties': ['暂不足以提出有内容差异的改版。']}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return state


def invoke(case, permit=None):
    repo, grant, vid, path, context, reservation, _ = case
    adapter = ScopedDirector(KEYS, repo, context, permit or (lambda _: True))
    evidence, assets, background = adapter._current_payload()
    return asyncio.run(adapter.propose(evidence, assets, background, reservation=reservation))


def test_handoff_keeps_current_cut_and_unverified_observations_separate(revision_case, transport):
    repo, _, vid, path, context, _, observations = revision_case
    before = path.read_bytes()
    result = invoke(revision_case)
    assert not result['result'].directions
    body = transport['calls'][0]
    data = json.loads(body['messages'][1]['content'])
    ref = data['untrusted_revision_context']
    assert ref['current_version'] == context['current_version']
    assert ref['unverified_sequence_observations'] == observations
    assert ref['observation_times'] == 'current_output_seconds'
    assert ref['segment_times'] == 'source_seconds' and not ref['audio_reheard']
    assert ref['current_output_timeline'] == [{'shot': 1, 'start': 0., 'end': 8.}]
    assert data['verified_evidence'] == context['confirmed_evidence']
    assert '合成物体位置可能变化' not in json.dumps(data['verified_evidence'], ensure_ascii=False)
    assert all(isinstance(row['content'], str) for row in body['messages'])
    assert '基于已存在的候选' in body['messages'][0]['content']
    assert '不是所有改版永久禁止剪辑' in body['messages'][0]['content']
    with repo.factory() as db: assert db.get(Version, vid).confirmed_at is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['version', 'observations', 'speech', 'owner', 'source'])
def test_stale_handoff_stops_before_request(revision_case, transport, change):
    repo, grant, vid, path, context, _, observations = revision_case
    if change == 'observations': observations['new_report'] = True
    elif change == 'speech': context['story_evidence'] = {'changed': True}
    elif change == 'source': (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'changed')
    else:
        with repo.transaction() as db:
            version = db.get(Version, vid)
            if change == 'version': version.title = 'updated'
            else: version.owner_id = 'other'
    with pytest.raises((DomainError, DirectorError)): invoke(revision_case)
    assert not transport['calls']


def test_changed_after_response_is_billed_but_not_returned_as_valid_plan(revision_case, transport):
    transport['after'] = lambda: revision_case[-1].update(new_report=True)
    with pytest.raises(DirectorError) as error: invoke(revision_case)
    assert error.value.code == 'SEQUENCE_REVIEW_STALE'
    assert error.value.usage['cost_status'] == 'known' and len(transport['calls']) == 1


def test_budget_recheck_detects_output_file_change_before_send(revision_case, transport):
    count = 0
    def permit(_):
        nonlocal count
        count += 1
        if count == 2:
            p = revision_case[3]
            p.write_bytes(p.read_bytes() + b'changed')
        return True
    with pytest.raises((DomainError, DirectorError)): invoke(revision_case, permit)
    assert not transport['calls']


def test_oversize_context_is_not_sent(revision_case, transport):
    revision_case[4]['question'] = '很长的要求' * 120000
    with pytest.raises(DirectorError, match='输入超出'): invoke(revision_case)
    assert not transport['calls']


def test_initial_generation_keeps_original_input_without_revision():
    from app.services.director import prepare_input
    evidence = [{'asset_id': 'a', 'start': 0., 'end': 1., 'note': '合成画面'}]
    assert revision.prepare(evidence, {'a': 1.}, '', None) == prepare_input(evidence, {'a': 1.}, '')


def test_rehearsal_is_persisted_idempotent_and_does_not_publish(revision_case, transport):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import select
    from app.models import Budget, Job, ModelCall
    repo, grant, vid, path, _, reservation, _ = revision_case
    spec = importlib.util.spec_from_file_location('rehearsal_test', Path(__file__).resolve().parents[2] / 'scripts/preview_story_revision.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with repo.transaction() as db:
        ledger = db.get(Budget, reservation)
        ledger.state, ledger.cost = 'settled', 0
        ep = db.get(Episode, grant['episode_id'])
        original = (deepcopy(ep.directions), ep.selected_direction, ep.directions_revision, ep.analysis_notes)
        jobs = len(list(db.scalars(select(Job))))
    assert module.preview(repo, KEYS, vid, '调整开场信息')['new_calls'] == 0
    with repo.factory() as db: assert len(list(db.scalars(select(Job)))) == jobs
    result = module.preview(repo, KEYS, vid, '调整开场信息', True)
    assert result['state'] == 'succeeded' and result['published'] is False
    repo.recover()
    assert module.preview(repo, KEYS, vid, '调整开场信息', True)['new_calls'] == 0
    with repo.factory() as db:
        ep = db.get(Episode, grant['episode_id'])
        assert (ep.directions, ep.selected_direction, ep.directions_revision, ep.analysis_notes) == original
        assert len(list(db.scalars(select(Version)))) == 1
        assert db.get(Version, vid).confirmed_at is None
        assert len(list(db.scalars(select(ModelCall)))) == 1
        assert db.get(Job, result['job']).result['validation_only'] is True
        assert db.get(Job, result['job']).result['revision_review'] == []
    assert len(transport['calls']) == 1
