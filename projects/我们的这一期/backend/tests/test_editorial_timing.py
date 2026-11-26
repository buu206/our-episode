from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from app.services import editorial_timing
from app.services.director_chat_model import prepare_chat_input


def shot(start,end,text='',at=None,**extra):
    return {'asset_id':'a'*32,'start':float(start),'end':float(end),'caption':'',
        'editorial':{'role':'action','treatment':'accent' if text else 'none',
            'reason':'fixture','evidence_ids':['e1'],**({'source_time':float(at),
            'duration':1.5,'text':text,'layout':'adaptive','sound':'none'} if text else {})},**extra}


def version(segments,**extra):
    return SimpleNamespace(state='confirmed',packaging='variety_v2',narration_path=None,segments=segments,**extra)


def test_source_cuts_map_to_output_and_overlap_without_asserting_action():
    v=version([shot(4,11),shot(11,15,'这一块稳了',11.5),shot(17,22),shot(22,26.159467,'又稳住了',22.5)])
    sequence={'windows':[{'observations':[
        {'start':14.133333,'end':16.366667,'visible_change':'移向塔顶'},
        {'start':16.633333,'end':18.866667,'visible_change':'举至塔顶上方'},
        {'start':19.133333,'end':19.866667,'visible_change':'放在塔顶'}]}]}
    original=deepcopy(v.segments)
    result=editorial_timing.context(v,sequence)
    assert [(r['output_start'],r['output_end']) for r in result['cues']]==[(7.5,9.0),(16.5,18.0)]
    assert result['cues'][1]['observation_overlaps']==[{'observation':'w1.o2','output_start':16.633,'output_end':18.0}]
    assert result['scope']=='planned_editorial_timing' and result['not_checked']
    assert v.segments==original


def test_crossfade_uses_renderer_overlap_clock():
    v=version([shot(10,14),shot(20,24,'看看',20.5,transition='dissolve',transition_duration=.2)])
    cue=editorial_timing.context(v)['cues'][0]
    assert cue['output_start']==4.3 and cue['output_end']==5.8


@pytest.mark.parametrize('change',[{'narration_path':'voice.wav'},{'packaging':'standard'},{'state':'draft'},{'state':'restricted'}])
def test_unknown_intro_or_unrendered_versions_do_not_guess_timing(change):
    v=version([shot(10,14,'看看',11)])
    for key,value in change.items():setattr(v,key,value)
    assert editorial_timing.context(v) is None


def test_no_active_plan_is_not_claim_of_no_effects():
    assert editorial_timing.context(None) is None
    assert editorial_timing.context(version([shot(1,5)])) is None


def test_touching_boundaries_are_not_overlaps():
    result=editorial_timing.context(version([shot(10,14,'看看',11)]),{'windows':[{'observations':[
        {'start':0,'end':1},{'start':2.5,'end':3}]}]})
    assert result['cues'][0]['observation_overlaps']==[]


def test_model_wire_keeps_same_output_clock_and_cautions():
    mapping=editorial_timing.context(version([shot(20,24,'看看',20.5)]))
    wire,_=prepare_chat_input({'available_actions':[],'editorial_timing':mapping})
    data=json.loads(wire)['untrusted_context']['editorial_timing']
    assert data['clock']=='output_seconds' and data['cues'][0]['output_start']==.5
    assert data['not_checked']==mapping['not_checked']


# Integration: refreshed timing must not recycle a previous assistant's claim.
from test_director_chat import client, services, settings, no_network, episode_only


def test_new_mapping_keeps_user_history_but_omits_old_timing_claim(client,monkeypatch):
    from app.models import Episode
    from app.services import director_chat
    _,eid=episode_only(client)
    repo=client.app.state.repo
    mapping=editorial_timing.context(version([shot(20,24,'看看',20.5)]))
    monkeypatch.setattr(editorial_timing,'context',lambda *args:mapping)
    rows=[SimpleNamespace(question='先保留这版',reply='字出现在抬手前',context={})]
    with repo.factory() as db:
        _,result=director_chat._context(repo,db,db.get(Episode,eid),rows,'现在呢',None,'materials')
    assert result['history']==[{'role':'user','text':'先保留这版'}]
    assert result['history_omitted_count']==1 and rows[0].reply=='字出现在抬手前'
    assert result['editorial_timing']==mapping
