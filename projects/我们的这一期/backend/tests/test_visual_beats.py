import hashlib
import json
import subprocess

import pytest
from PIL import Image

from app.schemas import VersionIn
from app.services import media
from app.services.editing import EditSegment, validate_director_segments
from app.services.visual_beats import decorate_visual_beats, plan
from test_media import _ffmpeg


def beat(kind='focus', **changes):
    return dict(kind=kind, source_time=.2, duration=1.6,
                target=dict(x=.1,y=.1,width=.3,height=.2),text='!',position='top_right',**changes)


def segment(beats=None):
    return dict(asset_id='a',start=0.,end=3.,caption='',visual_beats=beats or [beat()])


@pytest.mark.parametrize('change',[
    {'target':dict(x=.9,y=.1,width=.3,height=.2)}, {'duration':float('nan')},
    {'duration':3.}, {'source_time':2.5}, {'text':'x\n'}, {'kind':'arbitrary_filter'},
])
def test_reject_invalid_geometry_time_and_unbounded_commands(change):
    b=beat();b.update(change)
    with pytest.raises(ValueError): EditSegment.model_validate(segment([b]))


def test_cut_times_are_source_anchored_and_transitions_and_overlap_rejected():
    s=segment();s.update(start=.1,end=2.)
    assert plan([s],offset=2)[0][1] == pytest.approx(2.1)
    overlapping=segment([beat(),beat('burst')])
    with pytest.raises(ValueError): plan([overlapping])
    s['_preserve_expression']=True
    with pytest.raises(ValueError): plan([s])
    before=dict(asset_id='a',start=0.,end=1.,caption='')
    after=segment();after.update(transition='dissolve',transition_duration=.3)
    with pytest.raises(ValueError): plan([before,after])


def test_visual_beats_do_not_allow_model_to_invent_a_target_or_silently_change_packaging():
    s=segment();s.update(purpose='look',evidence_ids=['e'])
    with pytest.raises(ValueError): validate_director_segments([s],[dict(id='e',asset_id='a',start=0.,end=3.,kind='visual')])
    with pytest.raises(ValueError): VersionIn(title='x',segments=[segment()],packaging='original')


@pytest.fixture(scope='module')
def rendered(tmp_path_factory):
    folder=tmp_path_factory.mktemp('visual-beats')
    source=folder/'source.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-nostdin','-f','lavfi','-i','color=blue:s=180x320:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-vf',
        "drawbox=x=18:y=32:w=54:h=64:color=red:t=fill,drawbox=x=18:y=32:w=54:h=64:color=green:t=fill:enable='gte(t,1)'",
        '-t','7','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True,capture_output=True)
    rows=[]
    for kind,t in [('focus',.2),('burst',2.5),('comic_space',4.5)]:
        b=beat(kind);b['source_time']=t;b['duration']=1.6
        rows.append(b)
    s=segment(rows);s.update(end=7.,path=source)
    output=folder/'out.mp4'
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    result=decorate_visual_beats(source,output,[s],folder)
    return source,output,result,original_hash


def frame(path,t):
    import io
    data=subprocess.check_output([_ffmpeg(),'-v','error','-ss',str(t),'-i',str(path),'-frames:v','1','-f','image2pipe','-vcodec','png','-'])
    return Image.open(io.BytesIO(data)).convert('RGB')


def test_live_target_changes_with_source_and_effect_stays_in_its_time_window(rendered):
    source,output,result,original_hash=rendered
    # At this geometry the magnifier's center is (125,79), outside the original red area.
    red=frame(output,.6).getpixel((125,79));green=frame(output,1.4).getpixel((125,79))
    assert red[0] > 170 and red[2] < 70
    assert green[1] > 70 and green[0] < 60
    after=frame(output,2.1).getpixel((125,79))
    assert after[2] > 180 and after[0] < 60
    assert result['visual_beats_count']==3
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash


def test_audio_is_bit_preserved_and_cartoon_is_visibly_different(rendered):
    source,output,_,_=rendered
    def audio(p):
        return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(p),'-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(source)==audio(output)
    info=media.probe(output)
    assert (info['width'],info['height'])==(180,320)
    assert info['duration']==pytest.approx(7,abs=.04)
    space=frame(output,5)
    assert space.getpixel((4,160))[2]<100
    restored=frame(output,6.5)
    assert restored.getpixel((4,160))[2]>180


def test_existing_output_is_not_overwritten(rendered,tmp_path):
    source,output,_,_=rendered
    s=segment();s['path']=source
    with pytest.raises(media.MediaError) as exc: decorate_visual_beats(source,output,[s],tmp_path)
    assert exc.value.code=='MEDIA_OUTPUT_EXISTS'
