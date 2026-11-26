from copy import deepcopy
import pytest
from app.services.dialogue_accents import complete_quote
from app.services.automatic_treatment import plan_effects


def cue(text, **changes):
    return dict(dict(text=text, context=text, start=5., end=6.5,
                     alignment='asr', evidence_id='speech'), **changes)


@pytest.mark.parametrize('text', ['差一点！', '轮到你了', '我来试试', '这也太难了', '居然成功了'])
def test_different_scenes_use_literal_complete_speech(text):
    line=cue(text); original=deepcopy(line)
    events=plan_effects([], [line])
    assert len(events)==1 and events[0]['text']==text.rstrip('！')
    assert events[0]['start']==5 and events[0]['evidence_id']=='speech'
    assert line==original


@pytest.mark.parametrize('line', [
    cue('成功了',context='如果成功了再喊我'),
    cue('成功了',context='差点成功了'),
    cue('赢了',context='他说没赢了我不信'),
    cue('轮到你了吗？'), cue('嗯嗯好的'), cue('这是很长的说明不要变花字'),
    cue('轮到你了',alignment='utterance'),cue('轮到你了',subtitle_changed=True),
    cue('轮到你了',hidden=True),cue('轮到你了',end=5.2),
    cue('轮到你了',end=9),cue('轮到你了',evidence_id=''),
])
def test_uncertain_fragment_revised_or_unreadable_speech_is_not_promoted(line):
    assert complete_quote(line) is None


def test_protected_speech_and_duplicate_accents_remain_quiet():
    lines=[cue('轮到你了'),cue('轮到你了',start=10,end=11.5),cue('差一点！',start=15,end=16.5)]
    events=plan_effects([],lines,protected=[(14,17)])
    assert [(e['text'],e['start']) for e in events]==[('轮到你了',5)]


def test_whole_clause_preserves_negative_instead_of_cutting_a_positive_fragment():
    assert complete_quote(cue('根本没成功'))=='根本没成功'
    assert complete_quote(cue('轮到你了',context='停一下。轮到你了！'))=='轮到你了'


@pytest.mark.parametrize('size', ['320x180', '180x320'])
def test_new_scene_phrase_reaches_encoded_video_without_changing_duration(tmp_path, size):
    import subprocess
    from app.services.automatic_treatment import render_automatic
    from app.services import media
    from test_media import _ffmpeg
    from test_visual_beats import frame
    source=tmp_path/'source.mp4'; output=tmp_path/'output.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i',f'color=blue:s={size}:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','7',
        '-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(source)],
        check=True,capture_output=True)
    segments=[dict(start=0.,end=7.,_dialogue=[cue('轮到你了')])]
    path,report=render_automatic(source,output,segments,tmp_path,observations=[])
    assert path==output
    assert report['automatic_effects'][0]['text']=='轮到你了'
    assert report['automatic_effects'][0]['rendered_frames']>20
    assert report['cartoon_sounds']==[]
    def audio(path):
        return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(path),
            '-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(source)==audio(output)
    original,rendered=frame(source,5.7),frame(output,5.7)
    w,h=rendered.size
    # Compare the upper field, away from the ordinary subtitle lane.
    assert rendered.crop((0,0,w,int(h*.5))).tobytes()!=original.crop((0,0,w,int(h*.5))).tobytes()
    info=media.probe(output)
    assert info['has_audio'] and abs(info['duration']-7)<.15
