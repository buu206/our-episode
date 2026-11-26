"""Independent lettering, action sounds and subtitle exclusion on real images/audio."""
import wave
import array
import pytest
from PIL import Image, ImageChops
from app.services.editing import VisualBeat
from app.services.sticker_beats import render_frames
from app.services.variety_v2 import visual_sound_cues, _sfx


def beat(**changes):
    return dict(kind='headline',source_time=1.,duration=2.,text='稳住了',
                target=dict(x=.4,y=.75,width=.4,height=.2),**changes)


def test_independent_action_cues_leave_text_silent_and_legacy_unchanged(tmp_path):
    rows=[dict(asset_id='a',start=0.,end=8.,caption='',visual_beats=[
        beat(sound='none'),{**beat(sound='wood',sound_delay=1.),'source_time':4.}])]
    assert visual_sound_cues(rows)==[(5.,'wood')]
    output=tmp_path/'sfx.wav';assert _sfx(output,8.,visual_sound_cues(rows))==1
    with wave.open(str(output)) as audio: samples=array.array('h',audio.readframes(audio.getnframes()));rate=audio.getframerate()
    assert max(abs(v) for v in samples[:4*rate])==0
    assert 2000<max(abs(v) for v in samples[5*rate:6*rate])<15000
    rows[0]['visual_beats']=[beat()]
    assert visual_sound_cues(rows)==[(1.08,'aside')]


@pytest.mark.parametrize('change',[dict(sound='none',sound_delay=1.),dict(sound='wood',sound_delay=2.),dict(palette='url'),dict(scale=.1)])
def test_invalid_effect_configuration_rejected(change):
    with pytest.raises(ValueError):VisualBeat.model_validate(beat(**change))


def test_palette_and_position_change_rendered_art_not_source(tmp_path):
    for label,fields in [('warm',dict(palette='amber')),('cool',dict(palette='mint'))]:
        render_frames(VisualBeat.model_validate(beat(**fields)),tmp_path/'unused',tmp_path/label,360,640)
    with Image.open(tmp_path/'warm/0020.png') as a,Image.open(tmp_path/'cool/0020.png') as b:
        assert ImageChops.difference(a.convert('RGB'),b.convert('RGB')).getbbox()
    data=beat(palette='coral');data['target']=dict(x=.1,y=.1,width=.4,height=.2)
    render_frames(VisualBeat.model_validate(data),tmp_path/'unused',tmp_path/'moved',360,640)
    with Image.open(tmp_path/'moved/0020.png') as a,Image.open(tmp_path/'warm/0020.png') as b:
        assert a.getchannel('A').getbbox()[3]<b.getchannel('A').getbbox()[1]


def test_animated_letters_never_enter_reserved_subtitle_area(tmp_path):
    render_frames(VisualBeat.model_validate(beat()),tmp_path/'unused',tmp_path/'seq',360,640,safe_bottom=430)
    for path in (tmp_path/'seq').glob('*.png'):
        with Image.open(path) as image:
            assert image.getchannel('A').crop((0,430,360,640)).getbbox() is None


def test_new_settings_survive_schema_but_legacy_wire_stays_unchanged():
    old=beat();assert VisualBeat.model_validate(old).model_dump()=={**old,'position':'bottom_right'}
    new=beat(palette='mint',scale=.7,sound='wood',sound_delay=.8)
    assert VisualBeat.model_validate(new).model_dump()=={**new,'position':'bottom_right'}


def test_render_projects_actual_two_line_subtitles_to_safe_area(tmp_path):
    import subprocess
    from test_media import _ffmpeg
    from test_visual_beats import frame
    from app.services.visual_beats import decorate_visual_beats
    from app.services.automatic_treatment import subtitle_layout
    src=tmp_path/'source.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i','color=blue:s=360x640:r=30',
        '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','3',
        '-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    text='这是一句比较长的对白，需要保留完整的两行阅读空间'
    row=dict(asset_id='a',path=src,start=0.,end=3.,caption='',visual_beats=[beat(sound='none')],
             _dialogue=[dict(start=.8,end=2.8,text=text)])
    decorate_visual_beats(src,out,[row],tmp_path)
    result=frame(out,1.6)
    _,lines,size,y=subtitle_layout(360,640,text);assert len(lines)>=2
    top=int(y-size*.62)
    # Original blue subtitle region remains unpainted, while an overlay is visible above it.
    assert result.crop((0,top,360,640)).getextrema()[0][1]<15
    assert result.crop((0,0,360,top)).getextrema()[0][1]>100
