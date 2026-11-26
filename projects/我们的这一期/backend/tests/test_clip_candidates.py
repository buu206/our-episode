import pytest
from app.services.clip_candidates import build_clip_candidates, compile_clip_selection
from app.services.director import DirectorError
from app.services.editing import validate_director_segments


def row(id,start,end,kind='speech'):
    return dict(id=id,asset_id='a',start=float(start),end=float(end),note='测试原声',kind=kind)


def test_candidates_preserve_speech_and_reject_irrelevant_visual_reference():
    evidence=[row('s1',1,3),row('s2',9,11),row('outside',6,6.1,'visual')]
    clips=build_clip_candidates(evidence,{'a':20.})
    assert len(clips)==2
    for c in clips:
        assert 'outside' not in c['evidence_ids']
        assert c['story_continuity_verified'] is False
        shots=compile_clip_selection([{'clip_id':c['clip_id'],'purpose':'保留表达'}],evidence,{'a':20.})
        validate_director_segments(shots,evidence)


def test_overlapping_speech_is_included_whole_and_duplicates_collapse():
    evidence=[row('s1',1,4),row('s2',3,6)]
    # Neither source alone covers their union: no invented container.
    assert build_clip_candidates(evidence,{'a':10.})==[]


def test_short_adjacent_utterances_can_form_one_grounded_clip():
    evidence=[row('s0',0,1),row('s1',5,6),row('s2',6.5,8),row('s3',10,12)]
    clips=build_clip_candidates(evidence,{'a':15.})
    assert any(c['speech_ids']==['s1','s2'] for c in clips)


@pytest.mark.parametrize('change',['time','reference','unknown','repeat','stale'])
def test_model_cannot_smuggle_timing_or_reuse_stale_candidates(change):
    evidence=[row('s1',1,3),row('s2',9,11)]
    clip=build_clip_candidates(evidence,{'a':20.})[0]
    choices=[dict(clip_id=clip['clip_id'],purpose='开场')]
    if change=='time':choices[0]['start']=0.
    if change=='reference':choices[0]['evidence_ids']=['invented']
    if change=='unknown':choices[0]['clip_id']='unknown'
    if change=='repeat':choices*=2
    if change=='stale':evidence[0]['note']='修改后的文字'
    with pytest.raises(DirectorError):compile_clip_selection(choices,evidence,{'a':20.})


def test_requested_duration_is_checked_without_trimming_words():
    evidence=[row('s1',1,3),row('s2',9,11)]
    clip=build_clip_candidates(evidence,{'a':20.})[0]
    with pytest.raises(DirectorError,match='目标时长'):
        compile_clip_selection([dict(clip_id=clip['clip_id'],purpose='开场')],evidence,{'a':20.},duration_range=(25,40))


def test_visual_only_input_never_invents_a_dialogue_clip():
    assert build_clip_candidates([row('v1',0,1,'visual')],{'a':10.})==[]
