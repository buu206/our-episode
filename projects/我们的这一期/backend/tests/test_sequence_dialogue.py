"""Synthetic picture/text alignment; never a real audiovisual quality verdict."""
import json
from copy import deepcopy
import httpx
import pytest
from sqlalchemy import select
from app.models import Episode,Version,Job,ModelCall,Budget
from app.services import sequence_dialogue as sd, sequence_review as sr
from app.services.director import DirectorError
from test_sequence_review import rendered,client,setup,movie,confirmed,offline,KEYS,post,run
from test_new_providers import envelope


@pytest.fixture
def joint(rendered):
    c,repo,g,vid,path=rendered
    with repo.transaction() as db:
        e=db.get(Episode,g['episode_id']);v=db.get(Version,vid)
        v.packaging='variety_v2';e.analysis_state='confirmed';e.evidence_confirmed_revision=e.evidence_revision
        e.evidence=[{'id':'speech-one','asset_id':v.segments[0]['asset_id'],'kind':'speech','start':1.1,'end':2.9,'note':'合成对白，不要不要剪掉。'}]
    return rendered


def result(ids=None):
    return {'summary':'两张图之间有位置变化，同时提供了一句对白。','observations':[{
        'first_frame':'s1','last_frame':'s3','visible_change':'物体位置变化','uncertainty':'不能确认是谁说的或采样间动作',
        'speech_ids':['u1'] if ids is None else ids}]}


@pytest.fixture
def transport(monkeypatch):
    raw=result();raw['observations'][0].pop('speech_ids')
    state={'requests':[],'reply':raw,'after':None};original=httpx.AsyncClient
    def handler(request):
        state['requests'].append(request)
        body=json.loads(request.content);content=body['messages'][0]['content']
        packet=json.loads(content[1]['text'])['untrusted_confirmed_speech']
        assert packet['audio_analyzed'] is False and packet['scope']=='confirmed_speech_text'
        assert packet['items'][0]['text']=='合成对白，不要不要剪掉。'
        assert 'evidence_id' not in packet['items'][0]
        assert all('audio' not in c['type'] for c in content)
        if state['after']:state['after']()
        return httpx.Response(200,json=envelope(state['reply']))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return state


def submit(data):return post(data[0],f'/versions/{data[3]}/sequence-review',{'start':1,'end':3})


def test_joint_observation_binds_text_persists_links_and_does_not_edit(joint,transport):
    c,repo,g,vid,path=joint;original=path.read_bytes();job=submit(joint);run(repo)
    with repo.factory() as db:
        row=db.get(Job,job['id']);assert row.state=='succeeded',row.error
        assert row.payload['snapshot']['speech_digest']
        assert row.result['observations'][0]['speech_evidence_ids']==['speech-one']
        assert row.result['speech_context']['count']==1 and not row.result['audio_analyzed']
        projection=sr.context(repo,db,db.get(Version,vid))['windows'][0]
        assert projection['speech_context']['unlinked_evidence_ids']==[]
        assert projection['observations'][0]['speech_evidence_ids']==['speech-one']
        assert db.get(Version,vid).confirmed_at is None
        assert db.scalar(select(Budget)).state=='settled'
    repo.recover();assert submit(joint)['id']==job['id']
    assert len(transport['requests'])==1 and path.read_bytes()==original


@pytest.mark.parametrize('change',['text','time','confirmation','prompt'])
def test_changed_speech_scope_stops_before_send(joint,transport,change,monkeypatch,tmp_path):
    c,repo,g,vid,path=joint;job=submit(joint)
    with repo.transaction() as db:
        e=db.get(Episode,g['episode_id']);rows=deepcopy(e.evidence)
        if change=='text':rows[0]['note']='变更后的合成对白';e.evidence=rows
        elif change=='time':rows[0]['start']=1.5;e.evidence=rows
        elif change=='confirmation':e.analysis_state='review'
    if change=='prompt':
        p=tmp_path/'changed.txt';p.write_text('changed');monkeypatch.setattr(sd,'PROMPT',p)
    run(repo)
    assert not transport['requests']
    with repo.factory() as db:
        assert db.get(Job,job['id']).state=='failed'
        assert not list(db.scalars(select(ModelCall)))


@pytest.mark.parametrize('ids',[[],['u9'],['u1','u1'],['u1','u2']])
def test_provider_cannot_supply_unchecked_speech_links(joint,transport,ids):
    transport['reply']=result(ids);job=submit(joint);run(joint[1])
    with joint[1].factory() as db:
        row=db.get(Job,job['id']);assert row.state=='failed' and row.result is None
        assert db.scalar(select(Budget)).state=='settled'
    assert submit(joint)['id']==job['id'] and len(transport['requests'])==1


def test_speech_changed_after_paid_response_is_not_published(joint,transport):
    c,repo,g,vid,path=joint
    def mutate():
        with repo.transaction() as db:
            e=db.get(Episode,g['episode_id']);rows=deepcopy(e.evidence);rows[0]['note']='变了';e.evidence=rows
    transport['after']=mutate;job=submit(joint);run(repo)
    with repo.factory() as db:
        assert db.get(Job,job['id']).state=='failed' and db.get(Job,job['id']).result is None
        assert len(list(db.scalars(select(ModelCall))))==1


def test_partial_window_keeps_exact_text_and_reports_coverage(joint):
    c,repo,g,vid,path=joint
    with repo.factory() as db:
        p=sd.packet(db.get(Episode,g['episode_id']),db.get(Version,vid),{'start':2,'end':3})
    assert p['items'][0]['text']=='合成对白，不要不要剪掉。'
    assert not p['items'][0]['whole_utterance_in_window'] and p['items'][0]['whole_utterance_in_output']
    assert p['items'][0]['start']==1.1


def test_legacy_picture_only_report_stays_readable(joint):
    c,repo,g,vid,path=joint
    from test_sequence_review import store_observation
    old=store_observation(joint,1,3,1)
    with repo.factory() as db:
        sr.validate_job(repo,db,db.get(Job,old))
        assert 'speech_context' not in sr.context(repo,db,db.get(Version,vid))['windows'][0]


def test_unlinked_speech_is_reported_instead_of_claiming_no_speech(joint,transport):
    transport['reply']={'summary':'图片未看清变化。','observations':[]}
    job=submit(joint);run(joint[1])
    with joint[1].factory() as db:
        report=db.get(Job,job['id']).result
        assert report['speech_context']['unlinked_evidence_ids']==['speech-one']


def test_timing_contract_rejects_speech_from_other_moment():
    frames=[{'frame_id':'s1','time':1.},{'frame_id':'s3','time':3.}]
    packet={'items':[{'speech_id':'u1','start':4.,'end':5.}]}
    with pytest.raises(DirectorError):sd.parse(json.dumps(result()),frames,packet)
    assert sd.parse(json.dumps(result([])),frames,packet)['observations'][0]['speech_ids']==[]


def test_joint_input_limit_stops_before_provider_request(joint,transport,monkeypatch):
    monkeypatch.setattr(sr.Adapter,'input_limit',100)
    job=submit(joint);run(joint[1])
    with joint[1].factory() as db:
        row=db.get(Job,job['id']);assert row.state=='failed' and row.error['code']=='INPUT_LIMIT'
        assert db.scalar(select(Budget)).cost==0
    assert not transport['requests']


@pytest.mark.parametrize('ids',[[],['u9'],['u1','u1'],['u1','u2']])
def test_publication_rechecks_computed_reference_coverage(ids):
    frames=[{'frame_id':'s1','time':1.3},{'frame_id':'s3','time':2.7}]
    p={'items':[{'speech_id':'u1','start':1.1,'end':2.9}]}
    with pytest.raises(DirectorError):sd.parse(json.dumps(result(ids)),frames,p)


def test_model_frame_pairs_get_all_temporal_links_without_rewriting_dialogue():
    frames=[{'frame_id':'s1','time':1.3},{'frame_id':'s3','time':2.7}]
    p={'items':[{'speech_id':'u1','start':1.1,'end':2.9,'text':'不要不要'},
                {'speech_id':'u2','start':2.,'end':3.,'text':'还有呢'},
                {'speech_id':'u3','start':3.,'end':4.,'text':'不在范围'}]}
    raw=result();raw['observations'][0].pop('speech_ids')
    linked=sd.parse_model(json.dumps(raw),frames,p)
    assert linked['observations'][0]['speech_ids']==['u1','u2']
    assert p['items'][0]['text']=='不要不要'


@pytest.mark.parametrize('raw,reason',[('not json','json'),('{"secret":"private response"}','schema'),
    (json.dumps({'summary':'说明','observations':[{'first_frame':'missing','last_frame':'s2','visible_change':'变化','uncertainty':'未知'}]}),'frame_positions')])
def test_failure_diagnostics_exclude_provider_response(raw,reason):
    with pytest.raises(DirectorError) as err:
        sd.parse_model(raw,[{'frame_id':'s1','time':1.},{'frame_id':'s2','time':2.}],{'items':[]})
    assert err.value.usage=={'validation_reason':reason}
    assert 'private response' not in str(err.value)
