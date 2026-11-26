"""Deterministic mapping tests: no provider calls or inferred audiovisual facts."""
from types import SimpleNamespace
import pytest
from app.services.story_evidence import context


def fixture():
    episode=SimpleNamespace(evidence_revision=3,evidence_confirmed_revision=3,analysis_state='confirmed',evidence=[
        {'id':'first','kind':'speech','asset_id':'a','start':10.,'end':12.,'note':'不要删这句'},
        {'id':'last','kind':'speech','asset_id':'b','start':5.,'end':8.,'note':'保留重复重复'},
        {'id':'other','kind':'speech','asset_id':'x','start':0.,'end':1.,'note':'其他素材'}])
    version=SimpleNamespace(state='ready',packaging='variety_v2',narration_path=None,segments=[
        {'asset_id':'a','start':9.,'end':13.}, {'asset_id':'b','start':4.,'end':9.}])
    return episode,version


def test_source_times_map_to_output_without_rewriting_speech():
    ep,v=fixture();r=context(ep,v)
    assert r['speech']==[
        {'evidence_id':'first','shot':1,'output_start':1.,'output_end':3.,'whole_utterance':True},
        {'evidence_id':'last','shot':2,'output_start':5.,'output_end':8.,'whole_utterance':True}]
    assert r['omitted']==0 and r['not_checked']
    assert ep.evidence[1]['note']=='保留重复重复'


def test_overlap_uses_same_timing_as_renderer_and_partial_utterance_is_flagged():
    ep,v=fixture();v.segments[1].update(start=6.,transition='dissolve',transition_duration=.5)
    r=context(ep,v)
    assert r['speech'][1]=={'evidence_id':'last','shot':2,'output_start':3.5,'output_end':5.5,'whole_utterance':False}


@pytest.mark.parametrize('field,value',[('packaging','standard'),('narration_path','voice.wav'),('state','draft')])
def test_unknown_output_offset_is_not_guessed(field,value):
    ep,v=fixture();setattr(v,field,value);assert context(ep,v) is None


def test_unconfirmed_or_obsolete_speech_is_not_exposed():
    ep,v=fixture();ep.evidence_confirmed_revision=2;assert context(ep,v) is None
    ep.evidence_confirmed_revision=3;ep.analysis_state='review';assert context(ep,v) is None


def test_cap_reports_omitted_instead_of_implying_no_other_speech():
    ep,v=fixture();ep.evidence=[dict(ep.evidence[0],id=str(i)) for i in range(70)]
    r=context(ep,v);assert len(r['speech'])==64 and r['omitted']==6
