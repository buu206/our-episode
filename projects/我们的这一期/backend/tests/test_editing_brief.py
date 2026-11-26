"""The planning inventory must not manufacture facts or widen edit authority."""
from copy import deepcopy
import json
import pytest
from app.services.director import prepare_input, DirectorError, parse_result, check_grounding


def observation(id, asset, start, end, kind='visual'):
    return {'id':id,'asset_id':asset,'start':float(start),'end':float(end),
            'kind':kind,'note':'仅用于离线测试的已给定观察。'}


def test_only_observed_sources_enter_inventory_without_claiming_a_shared_event():
    rows=[observation('b1','b',3,6),observation('a1','a',0,2),observation('a2','a',5,9)]
    original=deepcopy(rows)
    payload=json.loads(prepare_input(rows,{'a':12.,'b':10.,'not_observed':20.}))
    brief=payload['editing_brief']
    assert brief['source_count']==2
    assert [r['asset_id'] for r in brief['inventory']]==['a','b']
    assert brief['same_event_verified'] is False
    assert brief['synchronous_cameras_verified'] is False
    assert payload['verified_evidence']==original==rows
    ranges=[r for a in brief['inventory'] for r in a['observations']]
    assert [(r['evidence_id'],r['start'],r['end']) for r in ranges]==[('a1',0.,2.),('a2',5.,9.),('b1',3.,6.)]


def test_continuity_containers_do_not_turn_unknown_gaps_into_observations():
    rows=[observation('s1','a',0,2,'speech'),observation('s2','a',9,11,'speech')]
    payload=json.loads(prepare_input(rows,{'a':12.}))
    item=payload['editing_brief']['inventory'][0]
    assert item['continuity_container_ids']==[payload['source_windows'][0]['id']]
    assert [r['evidence_id'] for r in item['observations']]==['s1','s2']
    assert not any(r['start']==0 and r['end']==11 for r in item['observations'])


def test_legacy_payload_remains_byte_compatible():
    rows=[{'asset_id':'a','start':0.,'end':2.,'note':'旧样例'}]
    assert prepare_input(rows,{'a':5.},'背景')==json.dumps({'verified_evidence':rows,'background_context':'背景'},ensure_ascii=False)


@pytest.mark.parametrize('mutation',['outside','continuity','duplicate'])
def test_inventory_cannot_bypass_input_validation(mutation):
    rows=[observation('a1','a',0,2)]
    if mutation=='outside':rows[0]['end']=20.
    if mutation=='continuity':rows[0]['kind']='continuity'
    if mutation=='duplicate':rows.append(deepcopy(rows[0]))
    with pytest.raises(DirectorError):prepare_input(rows,{'a':5.})


def test_inventory_does_not_allow_a_director_to_bridge_unobserved_visual_gap():
    rows=[observation('a1','a',0,2),observation('a2','a',5,9)]
    prepare_input(rows,{'a':10.})
    data={'directions':[{'id':'unsafe','title':'测试','summary':'测试',
        'evidence':[{'asset_id':'a','start':0.,'end':9.,'note':'试图扩大范围'}],
        'edit_plan':{'segments':[{'asset_id':'a','start':0.,'end':9.,'caption':'',
            'purpose':'测试','evidence_ids':['a1','a2']}],'narration_text':''}}], 'uncertainties':[]}
    result=parse_result(json.dumps(data),{'a':10.},require_edit_plan=True)
    with pytest.raises(DirectorError):check_grounding(result,rows)


def test_packaging_splits_are_not_counted_as_editing_improvement():
    from app.services.director import Direction, EditPlan
    from app.services.editing_brief import inspect_directions
    segments=[{'asset_id':'a','start':float(i),'end':float(i+1),'caption':''} for i in range(3)]
    d=Direction(id='test',title='测试',summary='测试',evidence=[{'asset_id':'a','start':0.,'end':3.,'note':'测试'}],
                edit_plan=EditPlan(segments=segments,narration_text=''))
    r=inspect_directions([d])[0]
    assert (r['actual_cuts'],r['adjacent_packaging_splits'])==(0,2)
    assert 'continuous_take_not_editing_gain' in r['flags']
    assert 'no_planned_graphics' in r['flags'] and not r['quality_accepted']


def test_real_cuts_and_opening_only_effects_remain_separate_review_questions():
    from app.services.director import Direction, EditPlan
    from app.services.editing_brief import inspect_directions
    d=Direction(id='test',title='测试',summary='测试',evidence=[{'asset_id':'a','start':0.,'end':3.,'note':'测试'}],
        edit_plan=EditPlan(segments=[{'asset_id':'a','start':0.,'end':3.,'caption':'','editorial':{
            'role':'setup','treatment':'accent','reason':'已观察','evidence_ids':['a1'],
            'source_time':0.,'duration':1.,'text':'开场'}},
            {'asset_id':'b','start':2.,'end':4.,'caption':''},
            {'asset_id':'a','start':5.,'end':7.,'caption':''}],narration_text=''))
    r=inspect_directions([d])[0]
    assert r['actual_cuts']==2 and r['source_count']==2
    assert r['flags']==['graphics_only_at_setup']
    assert r['status']=='needs_editorial_review' and r['quality_accepted'] is False
