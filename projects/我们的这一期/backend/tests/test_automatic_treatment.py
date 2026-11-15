from types import SimpleNamespace
from copy import deepcopy
import subprocess
import pytest
from PIL import Image
from app.services.automatic_treatment import valid_face, face_at, plan_effects, bulge, render_automatic
from app.services.dialogue_captions import attach_dialogue, caption_timeline
from test_media import _ffmpeg
from test_visual_beats import frame


def face():
    return dict(confidence=.99,box=[.3,.3,.25,.25],eyes=[[[.35,.36],[.36,.355],[.37,.36],[.36,.365]],
        [[.45,.36],[.46,.355],[.47,.36],[.46,.365]]],mouth=[[.37,.43],[.40,.40],[.43,.43],[.40,.46],[.39,.45]])


def observations():return [dict(time=i/5,faces=[face()]) for i in range(41)]


def test_landmarks_reject_nan_out_of_bounds_and_confidence():
    assert valid_face(face())
    for key,value in [('confidence',.3),('box',[.9,.3,.25,.25]),('mouth',[[float('nan'),.4]])]:
        f=face();f[key]=value;assert not valid_face(f)


def test_unpublished_validation_cannot_change_confirmed_caption_timing():
    ep = SimpleNamespace(id='e', owner_id='o', evidence_revision=1, evidence_confirmed_revision=1,
        evidence=[{'id':'speech','kind':'speech','asset_id':'a','start':0,'end':2,'note':'你好'}])
    original = SimpleNamespace(payload={'steps':{'asr:a':[{'start':0,'end':1,'text':'你好'}]}})
    audit = SimpleNamespace(payload={'validation_only':True,'steps':{'asr:a':[{'start':.5,'end':2,'text':'你好'}]}})
    db = SimpleNamespace(scalars=lambda _:SimpleNamespace(all=lambda:[audit, original]))
    segments = [{'asset_id':'a','start':0,'end':2}]
    attach_dialogue(db,ep,segments)
    assert segments[0]['_dialogue'][0]['start'] == 0
    assert segments[0]['_dialogue'][0]['end'] == 1


def test_tracking_drops_occlusion_face_switch_and_gap():
    rows=observations();assert face_at(rows,1.1,face()['box'])
    rows[5]['faces']=[];assert face_at(rows,1.1,face()['box']) is None
    assert face_at([observations()[0],observations()[20]],1.1,face()['box']) is None
    rows=observations();rows[5]['faces'][0]['box']=[.7,.7,.2,.2]
    assert face_at(rows,1.1,face()['box']) is None


def test_freeze_never_overlaps_speech_or_protected_interview():
    rows=observations();captions=[dict(start=0,end=8,text='连续说话',evidence_id='s')]
    assert plan_effects(rows,captions)==[]
    captions=[dict(start=6,end=7,text='呛到了',evidence_id='s')]
    result=plan_effects(rows,captions)
    assert any(e['kind']=='eyes' and e['evidence_id']=='s' for e in result)
    assert plan_effects(rows,captions,[(0,8)])==[]
    assert plan_effects([],captions)==[]


def test_warp_has_no_seams_outside_measured_region():
    image=Image.new('RGB',(200,200),'white')
    result=bulge(image,face()['mouth'],.7)
    assert result.getpixel((0,0))==(255,255,255)
    assert result.getpixel((60,86))==(255,255,255)


def test_quote_uses_literal_speech_and_does_not_require_or_invent_a_face():
    captions=[dict(start=5,end=6.4,text='不用不用吐不了',evidence_id='s')]
    events=plan_effects([],captions)
    assert len(events)==1 and events[0]['text']=='吐不了'
    assert events[0]['evidence_id']=='s' and 'anchor' not in events[0]
    assert plan_effects([],captions,[(4,7)])==[]
    assert not any(e['kind']=='mouth' for e in plan_effects(observations(),[]))
    for context in ['不是吐不了，是喝不了','别以为吐不了','如果吐不了怎么办？']:
        assert not plan_effects([],[dict(captions[0],context=context)])


def test_edited_caption_does_not_reuse_old_word_alignment():
    jobs=[SimpleNamespace(payload={'steps':{'asr:a':[
        dict(start=0.,end=1.,text='原文'),dict(start=1.1,end=2.,text='后文')]}})]
    db=SimpleNamespace(scalars=lambda _:SimpleNamespace(all=lambda:jobs))
    ep=SimpleNamespace(id='e',owner_id='o',evidence_revision=1,evidence_confirmed_revision=1,
        evidence=[dict(id='s',kind='speech',asset_id='a',start=0.,end=2.,note='用户修订')])
    segments=[dict(asset_id='a',start=0.,end=3.)]
    attach_dialogue(db,ep,segments)
    line=segments[0]['_dialogue'][0]
    assert line['text']=='用户修订' and line['alignment']=='utterance' and line['end']==2
    ep.evidence[0]['note']='原文 后文';attach_dialogue(db,ep,segments)
    assert segments[0]['_dialogue'][0]['alignment']=='asr'
    shifted=[dict(segments[0],start=0.,end=3.),dict(segments[0],start=0.,end=3.)]
    assert caption_timeline(shifted,1)[1]['start']==4
    bad=deepcopy(segments);bad[0]['_dialogue'][0]['end']=10
    with pytest.raises(ValueError):caption_timeline(bad)


def test_subtitles_render_with_original_audio_when_detector_has_no_faces(tmp_path):
    src=tmp_path/'src.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i','color=blue:s=180x320:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:v','libx264',
        '-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    segments=[dict(start=0.,end=2.,_dialogue=[dict(start=.3,end=1.6,text='先看这里',evidence_id='s')])]
    path,result=render_automatic(src,out,segments,tmp_path,observations=[])
    assert path==out and result['subtitle_count']==1 and result['automatic_effects']==[]
    a=frame(out,.8);b=frame(out,.1)
    assert a.crop((20,260,160,300)).tobytes()!=b.crop((20,260,160,300)).tobytes()
    def audio(p):return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(p),'-map','0:a','-c:a','copy','-f','adts','-'])
    assert audio(src)==audio(out)
