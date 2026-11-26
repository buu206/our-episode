import asyncio
import json
import pytest
from app.services.clip_candidates import build_clip_candidates
from app.services.clip_selection_director import parse_selection, prepare_selection_input, ClipSelectionDirector
from app.services.director import DirectorError


def data():
    evidence=[dict(id=f's{i}',kind='speech',asset_id='a',start=float(i*8),end=float(i*8+3),note='离线测试话轮') for i in range(3)]
    return evidence,{'a':30.}


def proposal(clips):
    return dict(title='测试',summary='先交代再回应',clips=[dict(clip_id=c['clip_id'],purpose='保留原话') for c in clips],uncertainties=['仍需核对故事衔接'])


def test_selection_compiles_source_timing_without_model_supplied_ranges():
    evidence,assets=data()
    clips=build_clip_candidates(evidence,assets)
    result=parse_selection(json.dumps(proposal(clips)),evidence,assets,duration_range=(5,20))
    segments=result.directions[0].edit_plan.segments
    assert [(s.start,s.end) for s in segments]==[(c['start'],c['end']) for c in clips]
    assert all(s.caption=='' and not s.visual_beats for s in segments)


def test_request_describes_unreviewed_inputs_and_original_context():
    evidence,assets=data()
    payload=json.loads(prepare_selection_input(evidence,assets))
    assert 'unreviewed' in payload['input_status']
    assert payload['target_duration_seconds']==[25,40]
    assert all(c['observations'] for c in payload['catalog'])


@pytest.mark.parametrize('change',['start','reference','unknown','reverse','overlong','empty'])
def test_invalid_contract_or_sequence_cannot_be_published(change):
    evidence,assets=data();clips=build_clip_candidates(evidence,assets);body=proposal(clips)
    if change=='start':body['clips'][0]['start']=0
    if change=='reference':body['clips'][0]['evidence_ids']=['fake']
    if change=='unknown':body['clips'][0]['clip_id']='fake'
    if change=='reverse':body['clips'].reverse()
    if change=='empty':body.update(clips=[],uncertainties=[])
    bounds=(1,2) if change=='overlong' else (5,20)
    with pytest.raises(DirectorError):parse_selection(json.dumps(body),evidence,assets,duration_range=bounds)


def test_insufficient_evidence_is_a_valid_empty_proposal_with_reason():
    evidence,assets=data()
    body=dict(title='暂不推荐',summary='缺少可用结尾',clips=[],uncertainties=['需要核对回应'])
    assert parse_selection(json.dumps(body),evidence,assets).directions==[]


def test_structural_failure_retains_billed_usage_without_retry():
    evidence,assets=data()
    class Offline(ClipSelectionDirector):
        calls=0
        def _guard(self):pass
        async def request(self,*args,**kwargs):
            self.calls+=1
            return '{"invalid":true}',{'cost_yuan':.01,'cost_status':'known'}
    client=Offline({})
    with pytest.raises(DirectorError) as caught:asyncio.run(client.propose(evidence,assets))
    assert caught.value.usage['cost_yuan']==.01
    assert client.calls==1
