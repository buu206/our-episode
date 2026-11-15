import subprocess

from PIL import Image, ImageDraw
import pytest

from app.services.editing import VisualBeat
from app.services.sticker_beats import cutout, sticker, render_frames
from app.services.visual_beats import decorate_visual_beats
from test_media import _ffmpeg


def effect(**updates):
    return dict(kind='cutout',source_time=.1,duration=1.,text='看这里',position='bottom_right',
                target=dict(x=.1,y=.1,width=.5,height=.5),
                contour=[dict(x=.3,y=.1),dict(x=.5,y=.5),dict(x=.1,y=.5)],**updates)


def test_silhouette_preserves_object_and_removes_background_and_has_outline():
    b=VisualBeat.model_validate(effect())
    frame=Image.new('RGB',(200,300),'blue')
    ImageDraw.Draw(frame).polygon([(60,30),(100,150),(20,150)],fill='red')
    p=cutout(frame,b)
    assert p.getpixel((0,0))[3]==0
    assert p.getpixel((40,85))[0]>240
    outlined=sticker(p,200)
    assert outlined.getchannel('A').getextrema()==(0,255)
    pixels=list(outlined.getdata())
    assert any(r>220 and 45<g<150 and blue>100 and a>240 for r,g,blue,a in pixels)


@pytest.mark.parametrize('change',[
    {'contour':None}, {'contour':[dict(x=.1,y=.1)]*3},
    {'contour':[dict(x=.9,y=.1),dict(x=.5,y=.3),dict(x=.1,y=.5)]},
    {'contour':[dict(x=.1,y=.1),dict(x=.5,y=.5),dict(x=.1,y=.5),dict(x=.5,y=.1)]},
    {'contour':[dict(x=float('nan'),y=.1),dict(x=.5,y=.5),dict(x=.1,y=.5)]},
])
def test_invalid_contours_are_rejected(change):
    data=effect();data.update(change)
    with pytest.raises(ValueError):VisualBeat.model_validate(data)


def test_cutout_render_keeps_audio_and_dimensions_and_does_not_linger(tmp_path):
    src=tmp_path/'in.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i','color=blue:s=180x320:r=30',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                    '-vf','drawbox=x=18:y=32:w=72:h=128:color=red:t=fill','-t','2',
                    '-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    b=effect()
    row=dict(path=src,asset_id='a',start=0.,end=2.,caption='',visual_beats=[b])
    result=decorate_visual_beats(src,out,[row],tmp_path)
    assert result['reviewed_freeze_cutouts']==1
    from test_visual_beats import frame
    changed=frame(out,.5);original=frame(src,.5)
    from PIL import ImageChops
    assert ImageChops.difference(changed,original).getbbox()
    after=frame(out,1.7)
    assert after.getpixel((140,220))[2]>180
    def audio(path):
        return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(path),'-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(src)==audio(out)
    assert changed.size==(180,320)


def test_headline_is_animated_and_transparent(tmp_path):
    data=effect();data.update(kind='headline');data.pop('contour')
    b=VisualBeat.model_validate(data)
    render_frames(b,tmp_path/'unused',tmp_path/'seq',180,320)
    with Image.open(tmp_path/'seq/0000.png') as first,Image.open(tmp_path/'seq/0012.png') as middle:
        assert middle.getpixel((0,0))[3]==0
        assert first.tobytes()!=middle.tobytes()
        assert middle.getchannel('A').getbbox()
