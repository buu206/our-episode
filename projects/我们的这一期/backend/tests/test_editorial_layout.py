import subprocess
from PIL import Image,ImageDraw
import pytest
from app.services.editorial_plan import EditorialBeat,compile_editorial
from app.services.editorial_layout import choose_slot,intersects,sound_cues,prepare_layout


def accent(**kw):
    return dict(role='action',treatment='accent',reason='可见的操作节点',evidence_ids=['v'],
        source_time=.4,duration=1.4,text='稳住',layout='adaptive',sound='none',**kw)


def test_faces_and_subtitle_band_excluded_and_stable():
    image=Image.new('RGB',(320,180),'white')
    faces=[(.1,.03,.5,.34),(.55,.3,.96,.65)]
    slot=choose_slot([image]*3,faces,'稳住',1280,720,.7)
    assert slot and slot==choose_slot([image]*3,faces,'稳住',1280,720,.7)
    assert slot['box'][3]<=.7 and not any(intersects(slot['box'],b) for b in faces)
    assert choose_slot([image]*3,[(0,0,1,1)],'稳住',1280,720,.7) is None


def test_low_detail_background_is_preferred_to_busy_area():
    image=Image.new('RGB',(320,180),'white');d=ImageDraw.Draw(image)
    for x in range(160):
        for y in range(180):d.point((x,y),fill='black' if (x+y)%2 else 'white')
    slot=choose_slot([image]*3,[],'稳住',1280,720,.7)
    assert slot and slot['cx']>.5


@pytest.mark.parametrize('changes',[{'sound':'legacy'},{'sound':'execute'}, {'layout':'adaptive','treatment':'chapter'},
                                    {'sound':'wood','sound_delay':1.3},{'text':'这一段太长超过八个汉字'}])
def test_new_fields_remain_bounded(changes):
    data=accent();data.update(changes)
    with pytest.raises(ValueError):EditorialBeat.model_validate(data)


def test_silent_skipped_or_overcrowded_cues_have_no_sound():
    rows=[]
    for index in range(4):
        raw=accent();raw.update(sound='wood' if index else 'none',source_time=.2)
        rows.append(dict(start=0.,end=3.,editorial=raw))
    plan=compile_editorial(rows,0);layouts={i:{'status':'placed'} for i in range(4)}
    assert sound_cues(plan,layouts)==[(3.2,'wood'),(9.2,'wood')]
    layouts[1]['status']='skipped';assert sound_cues(plan,layouts)==[(6.2,'wood')]


def test_detector_missing_omits_effect_without_inventing_position(tmp_path,monkeypatch):
    from app.services import automatic_treatment,media
    from types import SimpleNamespace
    monkeypatch.setattr(automatic_treatment,'detect_faces',lambda *args:([],'detector_failed'))
    monkeypatch.setattr(media,'_inspect',lambda *args:SimpleNamespace(width=1280,height=720))
    layouts=prepare_layout(tmp_path/'unused',[dict(start=0.,end=3.,editorial=accent())],tmp_path)
    assert layouts[0]['status']=='skipped' and layouts[0]['reason']=='face_check_unavailable'


def test_actual_render_uses_selected_layout_and_keeps_audio(tmp_path,monkeypatch):
    from app.services import automatic_treatment
    from app.services.editorial_render import render_editorial
    from test_media import _ffmpeg
    from test_visual_beats import frame
    src=tmp_path/'src.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i','color=blue:s=320x180:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','libx264',
        '-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    rows=[{'time':i*.2,'faces':[{'box':[.1,.03,.4,.3]}]} for i in range(15)]
    monkeypatch.setattr(automatic_treatment,'detect_faces',lambda *args:(rows,'detected'))
    path,result=render_editorial(src,out,[dict(start=0.,end=3.,editorial=accent())],tmp_path)
    layout=result['editorial_summary'][0]['layout'];assert layout['status']=='placed'
    image=frame(out,.9)
    assert image.crop((0,140,320,180)).getextrema()[0][1]<15
    assert image.getextrema()[0][1]>100
    def audio(p):return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(p),'-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(src)==audio(path)


def test_packaging_only_preserves_locked_time_and_does_not_clip_effects():
    from app.services.editorial_plan import retain_cut
    a=accent();a.update(source_time=1.,duration=1.)
    proposed=[dict(asset_id='a',start=0.,end=4.,caption='',editorial=a),
              dict(asset_id='a',start=4.,end=8.,caption='',editorial={**a,'source_time':5.})]
    locked=[dict(asset_id='a',start=1.5,end=3.),dict(asset_id='a',start=5.,end=7.)]
    result=retain_cut(proposed,locked)
    assert [(s['start'],s['end']) for s in result]==[(1.5,3.),(5.,7.)]
    assert result[0]['editorial']['treatment']=='none'
    assert result[1]['editorial']['treatment']=='accent'
    assert proposed[0]['editorial']['treatment']=='accent'
    with pytest.raises(ValueError):retain_cut(proposed,[dict(asset_id='b',start=0.,end=3.)])
    with pytest.raises(ValueError):retain_cut(proposed,[dict(asset_id='a',start=0.,end=9.)])


def test_one_smaller_size_can_use_shallow_headroom_but_never_cross_face():
    image=Image.new('RGB',(320,180),'white')
    face=(0,.22,1,1)
    slot=choose_slot([image]*3,[face],'又稳住了',1280,720,.73)
    assert slot and slot['scale']==.55
    assert slot['box'][1]>=.02 and not intersects(slot['box'],face)
    assert choose_slot([image]*3,[(0,.17,1,1)],'又稳住了',1280,720,.73) is None
