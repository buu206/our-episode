"""Offline durable-chat acceptance. Fixtures are never production AI claims."""
from copy import deepcopy
import time
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.chat_models import DirectorChatTurn
from app.main import create_app
from app.models import Audit, Budget, Episode, Job, ModelCall, Version
from app.retention_models import Retention
from app.services import director_guidance, retention
from app.services.director import DirectorError
from test_pipeline import (FakeServices, settings, no_network, voiced_video_bytes,
    authenticate, request, advance, create_with_asset, analyze_and_review, episode_state)


class ChatServices(FakeServices):
    costs = {**FakeServices.costs, 'director_chat': .003}

    def __init__(self):
        super().__init__()
        self.contexts = []
        self.reply = {'reply': '先保留完整原声，再选能交代地点的镜头。你更想保留哪段聊天？'}
        self.after_send = None

    def chat_ready(self, context=None):
        return True, 'Explicit offline test chat.'

    def chat_estimate(self, context=None):
        return .07

    def directions_ready(self, context=None):
        return True, 'Explicit offline scoped directions.'

    def scoped_director_estimate(self, context=None):
        return self.director_estimate()

    async def scoped_directions(self, evidence, assets, background, reservation, permit, context):
        return await self.directions(evidence, assets, background, reservation, permit)

    async def director_chat(self, context, reservation, permit):
        self._permission('director_chat', reservation, permit, .07)
        self.contexts.append(deepcopy(context))
        if self.after_send:
            self.after_send()
        return {'result': deepcopy(self.reply), 'usage': self._usage('director_chat')}


@pytest.fixture
def services():
    return ChatServices()


@pytest.fixture
def client(settings, services):
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as result:
        yield result


def episode_only(client):
    auth = authenticate(client)
    episode = request(client, '/episodes', auth, expected=201,
        body={'title': '纯本机导演对话测试', 'background': '仅虚构文字与合成图形'})
    return auth, episode['id']


def test_director_receives_current_output_checks_without_claiming_creative_review(client, services, voiced_video_bytes):
    from test_render_review import rendered
    auth, eid, vid, _ = rendered(client, voiced_video_bytes)
    review = request(client, f'/versions/{vid}/review', auth)
    advance(client, review['id'])
    job = send(client, auth, eid, key='inspect-actual-output', text='这版声音怎么样？先说检查结果。', version_id=vid)
    advance(client, job['id'])
    check = services.contexts[-1]['output_check']
    assert check['version_id'] == vid and check['scope'] == 'local_technical'
    assert check['decoded_full_output'] and check['not_checked']
    assert 'output_sha256' not in check and 'path' not in check


def chat_state(client, eid):
    r = client.get(f'/api/v1/episodes/{eid}/director/chat')
    assert r.status_code == 200, r.text
    return r.json()


def send(client, auth, eid, *, key='chat-request-1', text='怎么让故事连贯？', expected=200, **extra):
    return request(client, f'/episodes/{eid}/director/chat', auth, expected=expected,
        body={'client_id': key, 'text': text, 'confirm_send': True, **extra})


def apply(client, auth, eid, proposal, *, confirm=True, expected=200):
    return request(client, f'/episodes/{eid}/director/actions/{proposal}', auth,
                   body={'confirm': confirm}, expected=expected)


def test_chat_is_model_work_and_not_requirements_recording(client, services, settings):
    auth, eid = episode_only(client)
    initial = chat_state(client, eid)
    assert initial['messages'] == [] and initial['can_chat'] and initial['mode'] == 'demo'
    job = send(client, auth, eid)
    queued = chat_state(client, eid)
    assert queued['pending_job_id'] == job['id']
    assert queued['messages'][0]['client_id'] == 'chat-request-1'
    assert len(queued['messages']) == 1 and queued['messages'][0]['role'] == 'user'
    assert not services.counts
    assert send(client, auth, eid)['id'] == job['id']
    assert send(client, auth, eid, text='换句话', expected=409)['error']['code'] == 'DIRECTOR_REQUEST_CONFLICT'
    assert send(client, auth, eid, key='chat-request-2', expected=409)['error']['code'] == 'DIRECTOR_CHAT_BUSY'
    advance(client, job['id'])
    result = chat_state(client, eid)
    assert result['pending_job_id'] is None and len(result['messages']) == 2
    assert result['messages'][-1]['text'] == services.reply['reply']
    assert result['messages'][-1]['source'] == 'demo'
    assert episode_state(client, eid)['director']['revision'] == 0
    assert services.contexts[0]['available_actions'] == ['navigate']
    assert services.contexts[0]['confirmed_evidence'] == []
    next_job = send(client, auth, eid, key='chat-request-2', text='那先保留原声')
    advance(client, next_job['id'])
    assert services.contexts[-1]['history'] == [
        {'role': 'user', 'text': '怎么让故事连贯？'}, {'role': 'assistant', 'text': services.reply['reply']}]
    before = chat_state(client, eid)
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as restarted:
        authenticate(restarted)
        assert chat_state(restarted, eid) == before
    with client.app.state.repo.factory() as db:
        assert len(list(db.scalars(select(ModelCall)))) == 2
        assert all('怎么让' not in str(a.details) for a in db.scalars(select(Audit)))
        assert all('question' not in j.payload for j in db.scalars(select(Job)))


def test_auth_confirm_shape_and_episode_scope(client):
    auth, eid = episode_only(client)
    assert send(client, {}, eid, expected=403)['error']['code'] == 'CSRF_FAILED'
    assert send(client, auth, eid, confirm_send=False, expected=409)['error']['code'] == 'CONFIRM_REQUIRED'
    assert send(client, auth, eid, text=' ', expected=422)['error']['code'] == 'INVALID_CHAT_MESSAGE'
    assert send(client, auth, eid, text='字' * 2001, expected=422)['error']['code'] == 'INVALID_REQUEST'
    with client.app.state.repo.transaction() as db:
        other = Episode(owner_id='other', title='另一人的私密内容', background='不应发送')
        db.add(other); db.flush(); other_id = other.id
    assert client.get(f'/api/v1/episodes/{other_id}/director/chat').status_code == 404
    assert send(client, auth, other_id, expected=404)['error']['code'] == 'NOT_FOUND'
    assert chat_state(client, eid)['messages'] == []
    with client.app.state.repo.transaction() as db:
        db.get(Retention, eid).expires_at = time.time() - 1
    assert client.get(f'/api/v1/episodes/{eid}/director/chat').status_code == 410


def test_navigation_proposal_needs_confirmation_and_is_idempotent(client, services):
    auth, eid = episode_only(client)
    services.reply = {'reply': '先把原片放进来，我才能判断内容。', 'action': {
        'kind': 'navigate', 'title': '带来素材', 'explanation': '打开本期上传入口。', 'target_step': 'materials'}}
    job = send(client, auth, eid)
    advance(client, job['id'])
    proposal = chat_state(client, eid)['proposals'][0]
    assert proposal['target_step'] == 'materials' and proposal['status'] == 'pending'
    assert apply(client, auth, eid, proposal['id'], confirm=False, expected=409)['error']['code'] == 'CONFIRM_REQUIRED'
    result = apply(client, auth, eid, proposal['id'])
    assert result['next_step'] == 'materials'
    assert apply(client, auth, eid, proposal['id']) == result
    assert chat_state(client, eid)['proposals'][0]['status'] == 'applied'


def test_proposal_becomes_stale_after_requirements_change(client, services):
    auth, eid = episode_only(client)
    services.reply = {'reply': '从素材开始。', 'action': {
        'kind': 'navigate', 'title': '看素材', 'explanation': '打开素材。', 'target_step': 'materials'}}
    job = send(client, auth, eid); advance(client, job['id'])
    proposal = chat_state(client, eid)['proposals'][0]
    request(client, f'/episodes/{eid}/director/messages', auth,
            body={'revision': 0, 'client_id': 'new-requirement', 'text': '尽量热闹'})
    assert chat_state(client, eid)['proposals'][0]['status'] == 'stale'
    assert apply(client, auth, eid, proposal['id'], expected=409)['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'


def test_narration_action_creates_unapproved_new_version_and_keeps_old(client, services, voiced_video_bytes):
    auth, eid, asset = create_with_asset(client, voiced_video_bytes)
    base = request(client, f'/episodes/{eid}/versions', auth, expected=201, body={
        'title': '已确认旧版', 'segments': [{'asset_id': asset['id'], 'start': 0, 'end': 1.5}],
        'packaging': 'variety_v2', 'narration_text': '原来的开场'})
    with client.app.state.repo.transaction() as db:
        version = db.get(Version, base['id']); version.state = 'confirmed'; version.confirmed_at = 'fixed'
        version.narration_approved = True
    services.reply = {'reply': '开场可以缩短成一句，让原声早点进来。', 'action': {
        'kind': 'revise_narration', 'title': '换成利落开场', 'explanation': '只建立新候选，稍后确认声音。',
        'narration_text': '镜头给到这一桌！'}}
    job = send(client, auth, eid, version_id=base['id'], step='preview')
    assert job['version_id'] is None
    advance(client, job['id'])
    proposal = chat_state(client, eid)['proposals'][0]
    assert proposal['narration_text'] == '镜头给到这一桌！'
    assert len(episode_state(client, eid)['versions']) == 1
    result = apply(client, auth, eid, proposal['id'])
    assert result['version_id'] != base['id'] and result['next_step'] == 'preview'
    assert apply(client, auth, eid, proposal['id']) == result
    with client.app.state.repo.factory() as db:
        old, new = db.get(Version, base['id']), db.get(Version, result['version_id'])
        assert old.state == 'confirmed' and old.narration_text == '原来的开场' and old.narration_approved
        assert new.state == 'draft' and not new.narration_approved and not new.narration_path
        assert new.segments == old.segments and new.base_version_id == old.id
    assert services.counts['tts'] == 0


def test_revision_action_records_only_accepted_requirement_and_queues_once(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    services.reply = {'reply': '可以先保留完整原声，再用场景镜头接起来。', 'action': {
        'kind': 'revise_directions', 'title': '按原声重排方案', 'explanation': '保留旧版，重新生成候选方向。',
        'requirement': '保留完整原声，先交代场景再进入对话。'}}
    job = send(client, auth, eid); advance(client, job['id'])
    # A scoped director admission must not require unrelated global ASR/TTS
    # approval, or silently open those other providers.
    services.ready = lambda: (False, 'Global cloud admission stays disabled.')
    assert episode_state(client, eid)['director']['notes'] == ''
    proposal = chat_state(client, eid)['proposals'][0]
    result = apply(client, auth, eid, proposal['id'])
    assert result['job_id'] and result['next_step'] == 'direction'
    assert apply(client, auth, eid, proposal['id']) == result
    current = episode_state(client, eid)
    assert current['director']['notes'] == services.reply['action']['requirement']
    assert sum(j['kind'] == 'directions' for j in current['jobs']) == 1
    advance(client, result['job_id'])


def test_scoped_direction_binds_accepted_proposal_and_brief(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    services.reply = {'reply': '可以再试一版。', 'action': {
        'kind': 'revise_directions', 'title': '保留对话', 'explanation': '确认后重做方向。',
        'requirement': '先保留完整原声对话。'}}
    job = send(client, auth, eid); advance(client, job['id'])
    result = apply(client, auth, eid, chat_state(client, eid)['proposals'][0]['id'])
    request(client, f'/episodes/{eid}/director/messages', auth,
        body={'revision': 1, 'client_id': 'requirements-changed', 'text': '改为温暖回忆'})
    failed = advance(client, result['job_id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_BRIEF_CHANGED'
    assert services.counts['directions'] == 0


def test_unaccepted_proposal_cannot_authorize_scoped_direction_job(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    services.reply = {'reply': '先去看方案。', 'action': {
        'kind': 'navigate', 'title': '看方案', 'explanation': '仅导航。', 'target_step': 'direction'}}
    chat = send(client, auth, eid); advance(client, chat['id'])
    proposal = chat_state(client, eid)['proposals'][0]
    direction = request(client, f'/episodes/{eid}/directions', auth)
    with client.app.state.repo.transaction() as db:
        job = db.get(Job, direction['id'])
        job.payload = {**job.payload, 'chat_proposal_id': proposal['id']}
    failed = advance(client, direction['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'
    assert services.counts['directions'] == 0


def test_budget_limit_prevents_chat_network(client, services):
    auth, eid = episode_only(client)
    with client.app.state.repo.transaction() as db:
        db.add(Budget(owner_id='owner', episode_id=eid, amount=2_990_000,
                      cost=2_990_000, state='settled'))
    job = send(client, auth, eid)
    failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'BUDGET_EXCEEDED'
    assert services.counts['director_chat'] == 0


def test_older_failed_turn_cannot_restart_after_next_turn(client, services):
    auth, eid = episode_only(client)
    services.failures['director_chat'] = ['known']
    old = send(client, auth, eid); advance(client, old['id'], 'failed')
    current = send(client, auth, eid, key='newer-chat-message')
    advance(client, current['id'])
    assert request(client, f'/jobs/{old["id"]}/retry', auth, expected=409)['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'
    assert services.counts['director_chat'] == 2


def test_known_failure_can_retry_without_duplicate_user_message(client, services):
    auth, eid = episode_only(client)
    services.failures['director_chat'] = ['known']
    job = send(client, auth, eid); advance(client, job['id'], 'failed')
    assert chat_state(client, eid)['last_error']['code'] == 'TEST_KNOWN_FAILURE'
    retry = request(client, f'/jobs/{job["id"]}/retry', auth)
    advance(client, retry['id'])
    assert len(chat_state(client, eid)['messages']) == 2
    assert services.counts['director_chat'] == 2


def test_unknown_cost_stops_retry_and_new_message(client, services):
    auth, eid = episode_only(client)
    services.failures['director_chat'] = ['unknown']
    job = send(client, auth, eid); advance(client, job['id'], 'failed')
    assert request(client, f'/jobs/{job["id"]}/retry', auth, expected=409)['error']['code'] == 'COST_UNKNOWN'
    assert send(client, auth, eid, key='new-after-failure', expected=409)['error']['code'] == 'COST_UNKNOWN'
    assert services.counts['director_chat'] == 1


def test_context_changed_before_network_stops_without_charge(client, services):
    auth, eid = episode_only(client)
    job = send(client, auth, eid)
    request(client, f'/episodes/{eid}/director/messages', auth,
        body={'revision': 0, 'client_id': 'updated-brief', 'choice': 'warm'})
    failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'
    assert not services.counts
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(ModelCall)))


def test_confirmation_changed_before_reply_requires_fresh_context(client, services):
    auth, eid = episode_only(client)
    job = send(client, auth, eid)
    with client.app.state.repo.transaction() as db:
        episode = db.get(Episode, eid)
        episode.analysis_state = 'confirmed'
        episode.evidence_confirmed_revision = episode.evidence_revision
    failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'
    assert services.counts['director_chat'] == 0


def test_restart_releases_unsent_chat_but_holds_uncertain_sent_cost(client, services):
    auth, eid = episode_only(client)
    job = send(client, auth, eid)
    repo = client.app.state.repo
    with repo.transaction() as db:
        stored = db.get(Job, job['id']); stored.state = 'running'
        reservation = repo.reserve(db, 'owner', eid, .07)
        stored.payload = {**stored.payload, 'reservation': reservation}
        db.add(ModelCall(owner_id='owner', episode_id=eid, job_id=stored.id,
            reservation_id=reservation, kind='director_chat', state='prepared', usage={'real_call': False}))
    repo.recover()
    with repo.factory() as db:
        assert db.get(Job, job['id']).error['code'] == 'INTERRUPTED'
        cost = db.get(Budget, reservation)
        assert cost.state == 'settled' and cost.cost == 0
    request(client, f'/jobs/{job["id"]}/retry', auth)
    advance(client, job['id'])
    assert len(chat_state(client, eid)['messages']) == 2
    next_job = send(client, auth, eid, key='chat-after-restart')
    with repo.transaction() as db:
        stored = db.get(Job, next_job['id']); stored.state = 'running'
        reservation = repo.reserve(db, 'owner', eid, .07)
        stored.payload = {**stored.payload, 'reservation': reservation}
        db.add(ModelCall(owner_id='owner', episode_id=eid, job_id=stored.id,
            reservation_id=reservation, kind='director_chat', state='sending', usage={'real_call': True}))
    repo.recover()
    with repo.factory() as db:
        assert db.get(Budget, reservation).state == 'unknown'
    assert request(client, f'/jobs/{next_job["id"]}/retry', auth, expected=409)['error']['code'] == 'COST_UNKNOWN'
    assert len(chat_state(client, eid)['messages']) == 3


def test_context_changed_in_flight_settles_cost_but_does_not_publish(client, services):
    auth, eid = episode_only(client)
    def change():
        repo = client.app.state.repo
        with repo.transaction() as db:
            director_guidance.record(repo, db, db.get(Episode, eid), revision=0,
                client_id='while-model-running', text='新的侧重点')
    services.after_send = change
    job = send(client, auth, eid); failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'
    assert len(chat_state(client, eid)['messages']) == 1
    with client.app.state.repo.factory() as db:
        cost = db.scalars(select(Budget)).one()
        assert cost.state == 'settled' and cost.cost == 3000
    assert request(client, f'/jobs/{job["id"]}/retry', auth, expected=409)['error']['code'] == 'DIRECTOR_CONTEXT_CHANGED'


@pytest.mark.parametrize('reply', [
    {'reply': ''}, {'reply': '伪造任意工具', 'action': {'kind': 'delete_episode'}},
    {'reply': '没有确认依据也想改方向', 'action': {'kind': 'revise_directions', 'title': '不允许', 'explanation': '未确认', 'requirement': '重剪'}},
])
def test_invalid_or_unavailable_model_action_is_not_published(client, services, reply):
    auth, eid = episode_only(client)
    services.reply = reply
    job = send(client, auth, eid); failed = advance(client, job['id'], 'failed')
    assert failed['error']['code'] == 'INVALID_CHAT_REPLY'
    assert chat_state(client, eid)['proposals'] == []


def test_deletion_clears_private_chat_and_checkpoint_but_keeps_minimal_cost(client, services):
    auth, eid = episode_only(client)
    job = send(client, auth, eid); advance(client, job['id'])
    expiry = episode_state(client, eid)['retention']['expires_at']
    assert expiry
    request(client, f'/episodes/{eid}?confirm=true', auth, method='DELETE')
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(DirectorChatTurn)))
        stored = db.get(Job, job['id'])
        assert stored.payload == {} and stored.result is None
        assert db.scalars(select(Budget)).one().cost == 3000


def test_context_budget_counts_system_prompt_and_trims_only_old_messages(client, voiced_video_bytes, monkeypatch):
    from types import SimpleNamespace
    from app.services import director_chat, director_chat_model
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    repo = client.app.state.repo
    rows = [SimpleNamespace(question='以前的问题' * 100, reply='以前的回答' * 100) for _ in range(4)]
    with repo.factory() as db:
        episode = db.get(Episode, eid)
        _, base = director_chat._context(repo, db, episode, [], '现在的问题', None, 'materials')
        user, _ = director_chat_model.prepare_chat_input(base)
        limit = len((director_chat_model.load_chat_prompt() + user).encode()) + 2048 + 200
        monkeypatch.setattr(director_chat_model.DeepSeekDirectorChat, 'input_limit', limit)
        _, result = director_chat._context(repo, db, episode, rows, '现在的问题', None, 'materials')
        assert director_chat_model.input_fits(result)
        assert result['question'] == '现在的问题' and result['confirmed_evidence'] == base['confirmed_evidence']
        assert result['history_omitted_count'] == 8 and result['history'] == []
        assert len(rows) == 4 and rows[0].question


def test_overlarge_fixed_context_fails_before_job_or_budget_reservation(client, services, monkeypatch):
    from app.services.director_chat_model import DeepSeekDirectorChat
    auth = authenticate(client)
    eid = request(client, '/episodes', auth, body={'title': '上下文限制'}, expected=201)['id']
    monkeypatch.setattr(DeepSeekDirectorChat, 'input_limit', 2048)
    result = request(client, f'/episodes/{eid}/director/chat', auth, body={
        'text': '看看这期', 'client_id': 'too-large', 'step': 'materials', 'confirm_send': True}, expected=422)
    assert result['error']['code'] == 'DIRECTOR_CONTEXT_TOO_LONG'
    with client.app.state.repo.factory() as db:
        assert not list(db.scalars(select(Job).where(Job.episode_id == eid)))
        assert not list(db.scalars(select(Budget).where(Budget.episode_id == eid)))
