import asyncio
import json
import httpx
import pytest
from app.services.director import DirectorError
from app.services.director_text_comparison import ProTextComparison
from app.services.director_chat_access import chat_adapter
from app.services.deepseek import response_usage, MODEL
from app.models import Episode
from test_director_chat_access import sample as base_sample, offline, CONFIG
from test_new_providers import envelope, NOW


@pytest.fixture
def sample(base_sample):
    from app.models import Budget
    repo, grant, context, previous = base_sample
    with repo.transaction() as db:
        ledger = db.get(Budget, previous)
        ledger.state, ledger.cost = "settled", 0
        reservation = repo.reserve(db, "local-owner", grant["episode_id"], ProTextComparison.required_reservation_yuan)
    return repo, grant, context, reservation


def adapter(sample):
    repo, _, context, reservation = sample
    return ProTextComparison(CONFIG, repo, context, lambda r: r == reservation)


@pytest.fixture
def transport(monkeypatch):
    state = {'requests': [], 'model': 'DeepSeek-V4-Pro-0813'}
    original = httpx.AsyncClient
    def handle(request):
        state['requests'].append(json.loads(request.content))
        data = envelope({'reply': '先保留动作，不把没说话直接认成空白。', 'action': None})
        data['model'] = state['model']
        return httpx.Response(200, json=data)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return state


def test_pro_is_explicit_text_only_and_uses_pro_tariff(sample, transport):
    result = asyncio.run(adapter(sample).chat(sample[2], reservation=sample[3]))
    request = transport['requests'][0]
    assert request['model'] == 'deepseek-v4-pro'
    assert request['thinking'] == {'type': 'disabled'}
    assert all(isinstance(m['content'], str) for m in request['messages'])
    usage = result['usage']
    assert usage['model'] == 'deepseek-v4-pro' and usage['cost_status'] == 'known'
    expected = (usage['cache_hit_tokens'] * .3 + usage['cache_miss_tokens'] * 9 + usage['output_tokens'] * 27) / 1e6
    if usage['price_band'] == 'offpeak': expected /= 2
    assert usage['cost_yuan'] == pytest.approx(expected)
    assert chat_adapter(CONFIG, sample[0], sample[2], lambda r: True).transport_model == MODEL


def test_wrong_returned_model_does_not_use_flash_price(sample, transport):
    transport['model'] = MODEL
    with pytest.raises(DirectorError) as error:
        asyncio.run(adapter(sample).chat(sample[2], reservation=sample[3]))
    assert error.value.code == 'MODEL_NOT_ALLOWED'
    assert error.value.usage['cost_status'] == 'unknown'
    assert len(transport['requests']) == 1


@pytest.mark.parametrize('change', ['evidence', 'source_bytes', 'context', 'oversize'])
def test_comparison_rechecks_scope_and_budget_before_send(sample, transport, change):
    repo, grant, context, reservation = sample
    if change == 'evidence':
        with repo.transaction() as db:
            db.get(Episode, grant['episode_id']).evidence = []
    elif change == 'source_bytes':
        (repo.data_dir / grant['assets'][0]['path']).write_bytes(b'x' * 64)
    elif change == 'context': context['story_evidence'] = {'unsupported': True}
    elif change == 'oversize': context['question'] = '太长' * 32000
    with pytest.raises(DirectorError):
        asyncio.run(adapter(sample).chat(context, reservation=reservation))
    assert not transport['requests']


def test_pro_reservation_covers_peak_cap():
    assert ProTextComparison.required_reservation_yuan >= (32000 * 9 + 2048 * 27) / 1e6
    data = envelope({'reply': 'test', 'action': None})
    data['model'] = 'deepseek-v4-pro'
    data['usage'] = {'prompt_tokens': 32000, 'completion_tokens': 2048,
                     'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 32000}
    usage = response_usage(data, NOW, NOW, 32000, 2048, model=ProTextComparison.transport_model,
                           models=ProTextComparison.response_models, rates=ProTextComparison.token_rates)
    assert usage['cost_status'] == 'known' and usage['cost_yuan'] <= ProTextComparison.required_reservation_yuan


@pytest.fixture
def comparison_source(sample):
    from app.services.pipeline import ModelServices
    from app.services import director_chat
    from app.models import Budget
    repo, grant, _, reservation = sample
    services = ModelServices(CONFIG)
    services.repo = repo
    with repo.transaction() as db:
        ledger = db.get(Budget, reservation)
        ledger.state, ledger.cost = 'settled', 0
        ep = db.get(Episode, grant['episode_id'])
        job = director_chat.enqueue(repo, db, ep, services, client_id='offline-source',
            text='先保留原话，怎么改？', confirm_send=True)
        job.state = 'succeeded'
        jid = job.id
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('compare_text_test', Path(__file__).resolve().parents[2] / 'scripts/compare_director_text.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return repo, jid, module.compare


def test_operator_preflight_execution_and_repetition_keep_chat_unchanged(comparison_source, transport):
    from sqlalchemy import select
    from app.models import Job, ModelCall, Budget
    from app.chat_models import DirectorChatTurn
    repo, jid, compare = comparison_source
    with repo.factory() as db:
        original = [(t.id, t.reply, t.proposal) for t in db.scalars(select(DirectorChatTurn))]
        count = len(list(db.scalars(select(Job))))
    assert compare(repo, CONFIG, jid)['new_calls'] == 0
    with repo.factory() as db: assert len(list(db.scalars(select(Job)))) == count
    result = compare(repo, CONFIG, jid, True)
    assert result['state'] == 'succeeded' and not result['published']
    assert compare(repo, CONFIG, jid, True)['new_calls'] == 0
    with repo.factory() as db:
        assert [(t.id, t.reply, t.proposal) for t in db.scalars(select(DirectorChatTurn))] == original
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].usage['model'] == 'deepseek-v4-pro'
        assert db.get(Budget, calls[0].reservation_id).state == 'settled'
        assert db.get(Job, result['job']).payload['steps']['comparison']['reply']
    assert len(transport['requests']) == 1


def test_operator_does_not_repeat_unknown_cost(comparison_source, transport):
    repo, jid, compare = comparison_source
    transport['model'] = 'unrecognized-provider-model'
    result = compare(repo, CONFIG, jid, True)
    assert result['state'] == 'failed'
    assert compare(repo, CONFIG, jid, True)['new_calls'] == 0
    assert len(transport['requests']) == 1
