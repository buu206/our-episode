from copy import deepcopy
import json
import pytest
from sqlalchemy import select
from app.models import Episode,Version,Job
from app.chat_models import DirectorChatTurn
from app.repository import DomainError
from app.services import story_events as events, director_chat as chat
from app.services.director_chat_model import parse_chat_reply
from app.services.director import DirectorError
from app.services.pipeline import Pipeline
from app.worker import Worker
from test_director_cut import cut_version, audio_version, rendered, client, setup, movie, confirmed, offline, KEYS
from test_director_chat import ChatServices


def context():
    return {'question':'保留“交代”，先提案','available_actions':['revise_cut'],
        'current_version':{'segments':[{'asset_id':'a','start':0.,'end':6.,'caption':''}]},
        'confirmed_evidence':[{'id':'first','asset_id':'a','kind':'speech','start':0.,'end':2.,'note':'交代'},
          {'id':'last','asset_id':'a','kind':'speech','start':4.,'end':6.,'note':'结果'}],
        'sequence_check':{'windows':[{'observations':[{'start':2.,'end':4.,'visible_change':'前倾后坐回','uncertainty':'抽帧'}]}]}}


def decisions():
    return [{'event':'e1','keep':True,'role':'setup','reason':'交代','risk':''},
            {'event':'e2','keep':False,'role':'aside','reason':'舍弃准备','risk':'动作衔接可能跳'},
            {'event':'e3','keep':True,'role':'result','reason':'保留结果','risk':''}]


def test_partition_and_compile_preserve_source_without_inferred_silence():
    c=context();before=deepcopy(c);rows=events.build(c)
    assert [r['kind'] for r in rows]==['speech','untranscribed_action','speech']
    assert rows[0]['must_keep'] and rows[1]['observations']==['w1.o1']
    selected,review=events.compile_decisions(c,decisions())
    assert [(s['start'],s['end']) for s in selected]==[(0.,2.),(4.,6.)]
    assert all(r['quality']=='not_accepted' for r in review) and c==before


@pytest.mark.parametrize('change',['missing','duplicate','reorder','unknown','protected','unobserved','risk'])
def test_bad_event_decisions_blocked(change):
    c=context();d=decisions()
    if change=='missing':d.pop()
    if change=='duplicate':d[1]['event']='e1'
    if change=='reorder':d.reverse()
    if change=='unknown':d[-1]['event']='e4'
    if change=='protected':d[0].update(keep=False,risk='会丢交代')
    if change=='unobserved':c['sequence_check']=None
    if change=='risk':d[1]['risk']=' '
    with pytest.raises(DomainError):events.compile_decisions(c,d)


def test_overlapping_speech_not_split_and_adjacent_decisions_merged():
    c=context();c['confirmed_evidence'].append({'id':'overlap','asset_id':'a','kind':'speech','start':1.,'end':3.,'note':'第二个人'})
    rows=events.build(c)
    assert [(r['start'],r['end']) for r in rows]==[(0.,3.),(3.,4.),(4.,6.)]
    d=decisions();d[1]['keep']=True
    selected,_=events.compile_decisions(c,d)
    assert len(selected)==1 and selected[0]['end']==6


def test_schema_rejects_two_cut_formats():
    action={'kind':'revise_cut','title':'试剪','explanation':'取舍','event_decisions':decisions()}
    assert parse_chat_reply(json.dumps({'reply':'建议','action':action}),available_actions=['revise_cut']).action.event_decisions
    action['selections']=[{'shot':1,'start':0.,'end':2.,'reason':'首段'}]
    with pytest.raises(DirectorError):parse_chat_reply(json.dumps({'reply':'建议','action':action}),available_actions=['revise_cut'])


def test_event_decisions_use_existing_card(cut_version):
    c,repo,grant,vid,path=cut_version;services=ChatServices()
    with repo.transaction() as db:
        job=chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,client_id='event-cut-test',text='保留“你好”，舍弃后面一句试试',version_id=vid,step='preview',confirm_send=True);jid=job.id
        turn=db.get(DirectorChatTurn,job.payload['turn_id']);rows=events.build(turn.context)
    d=[{'event':row['event'],'keep':row['start']<1. or row['kind']=='untranscribed_action','role':'setup' if row['start']<1. else 'aside','reason':'按完整话轮取舍','risk':'可能少了后文'} for row in rows]
    services.reply={'reply':'先给试剪','action':{'kind':'revise_cut','title':'按故事取舍','explanation':'舍弃后句','event_decisions':d}}
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        job=db.get(Job,jid);turn=db.get(DirectorChatTurn,job.payload['turn_id'])
        assert job.state=='succeeded',job.error
        assert turn.proposal['event_review'] and 'event_decisions' not in turn.proposal
        assert turn.proposal['selections'] and turn.proposal['status']=='pending'
        assert len(list(db.scalars(select(Version))))==1

@pytest.mark.parametrize('question,wanted', [('给一张镜头调整卡，先不执行',True),('试剪看看',True),('给我一个剪法',True),('不要试剪，先聊聊',False),('只解释为什么',False),('不要建议卡',False)])
def test_explicit_request_uses_focused_prompt_without_past_assistant_bias(question,wanted):
    from app.services.director_chat_model import prepare_chat_input,load_chat_prompt
    c=context();c['question']=question;c['history']=[{'role':'assistant','text':'旧的错误判断'}]
    wire,available=prepare_chat_input(c)
    assert events.requested(c)==wanted
    if wanted:
        assert '旧的错误判断' not in wire and available==['revise_cut']
        assert '只做故事取舍' in load_chat_prompt(c)
    else:assert '旧的错误判断' in wire


def test_dense_observation_boundaries_split_only_long_nonverbal_spans():
    c=context();c['current_version']['segments'][0]['end']=12.
    c['confirmed_evidence'][1].update(start=10.,end=12.)
    c['sequence_check']['windows'][0]={'sampling':{'continuous_video':False,'max_sample_gap_seconds':.25},'observations':[{'start':2.2,'end':5.1},{'start':5.1,'end':8.2},{'start':8.2,'end':9.8}]}
    rows=events.build(c)
    assert [(r['start'],r['end']) for r in rows]==[(0.,2.),(2.,5.1),(5.1,8.2),(8.2,10.),(10.,12.)]
    c['sequence_check']['windows'][0]['sampling']['max_sample_gap_seconds']=1.
    assert len(events.build(c))==3



def test_saved_catalog_survives_partition_recipe_change(monkeypatch):
    c=context();c['story_event_catalog']=events.derive(c)
    monkeypatch.setattr(events,'derive',lambda _: [])
    selected,_=events.compile_decisions(c,decisions())
    assert [(r['start'],r['end']) for r in selected]==[(0.,2.),(4.,6.)]
    copy=events.build(c);copy[0]['end']=1.
    assert c['story_event_catalog'][0]['end']==2.
