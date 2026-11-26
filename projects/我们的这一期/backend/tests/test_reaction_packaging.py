import wave
import array
import subprocess
from PIL import Image, ImageChops
from app.services.reaction_packaging import display_captions, face_sticker, paste_reaction, write_sounds, bound_effects
from app.services.automatic_treatment import face_at, plan_effects, valid_face
from test_automatic_treatment import face, observations
from test_media import _ffmpeg


def test_display_groups_fragments_without_changing_original_or_reviewed_text():
    rows=[dict(start=0,end=.6,text='嗓子打',evidence_id='s',cue_id='a'),
          dict(start=.6,end=.7,text='开',evidence_id='s',cue_id='b')]
    result=display_captions(rows)
    assert result[0]['text']=='嗓子打开' and result[0]['end']==.7
    assert result[0]['cue_ids']==['a','b'] and rows[0]['text']=='嗓子打'
    rows[1]['subtitle_changed']=True
    assert len(display_captions(rows))==2
    rows[1]['hidden']=True
    assert len(display_captions(rows))==1
    rows[1].pop('hidden');rows[1].pop('subtitle_changed');rows[1]['start']=1.5;rows[1]['end']=2
    assert len(display_captions(rows))==2


def test_reading_hold_stays_in_silence_without_moving_onset_or_source_edit():
    rows=[dict(start=0,end=.3,text='看啊',evidence_id='s',cue_id='a'),
          dict(start=1.5,end=1.8,text='到这里',evidence_id='t',cue_id='b')]
    result=display_captions(rows,boundaries=[2],speech_spans=rows,hold_short=True)
    assert result[0]['start']==0 and result[0]['source_end']==.3 and abs(result[0]['end']-.85)<1e-9
    assert result[1]['end']==2 and rows[0]['end']==.3
    rows[0]['subtitle_changed']=True
    assert display_captions(rows,boundaries=[2],hold_short=True)[0]['end']==.3


def test_reading_hold_does_not_cross_cut_hidden_voice_or_interview():
    row=dict(start=0,end=.3,text='原话',evidence_id='s',cue_id='a')
    assert display_captions([row],boundaries=[.5],hold_short=True)[0]['end']==.5
    assert display_captions([row],boundaries=[2],speech_spans=[row,dict(start=.4,end=.8)],hold_short=True)[0]['end']==.4
    assert display_captions([row],boundaries=[2],protected=[(0,1)],hold_short=True)[0]['end']==.3
    later=dict(row,start=.4,end=.7,text='后话')
    assert len(display_captions([row,later],boundaries=[.4,1]))==2
    assert len(display_captions([row,later],speech_spans=[dict(start=.31,end=.39)]))==2
    # Decimal edit boundaries must not split two cues inside the same shot.
    first=dict(row,start=28.099999999999994,end=28.22)
    second=dict(row,start=28.22,end=28.64)
    assert len(display_captions([first,second],boundaries=[28.1,30.2]))==1
    last=dict(row,start=29.38,end=30.2)
    clipped=display_captions([last],boundaries=[30.166667],hold_short=True)[0]
    assert clipped['source_end']==30.2 and clipped['end']==30.166667


def test_graphics_never_cross_shots_or_borrow_a_future_face():
    quote=dict(kind='quote',start=.3,end=1.6)
    face_event=dict(kind='eyes',start=.7,end=2,peak=1.2)
    result=bound_effects([quote,face_event],[1,3])
    assert result==[dict(quote,end=1)] and quote['end']==1.6
    assert bound_effects([dict(quote,start=.8)],[1,3])==[]


def test_wide_face_sticker_stays_inside_portrait_and_landscape():
    subject=Image.new('RGBA',(700,130),'#00ff00')
    for size in [(360,640),(640,360)]:
        for anchor in [[.1,.2,.2,.2],[.7,.2,.2,.2]]:
            for t in [.08,.15,.3,.65]:
                out=paste_reaction(Image.new('RGB',size,'black'),subject,t,1,anchor)
                red,green,blue=out.split()
                mask=ImageChops.multiply(green.point(lambda v:255 if v>200 else 0),red.point(lambda v:255 if v<20 else 0))
                mask=ImageChops.multiply(mask,blue.point(lambda v:255 if v<20 else 0))
                box=mask.getbbox()
                assert box and 0<box[0]<box[2]<size[0] and 0<box[1]<box[3]<size[1]


def test_visible_contour_sticker_has_transparent_corners_not_a_rectangle():
    f=face();f.update(contour=[[.3,.31],[.3,.4],[.32,.48],[.37,.54],[.43,.55],
                             [.5,.5],[.55,.4],[.55,.31]],contour_kind='jaw')
    src=Image.new('RGB',(360,640),'orange')
    subject=face_sticker(src,f)
    assert subject is not None and subject.height>120
    assert subject.getpixel((0,0))[3]==0
    assert subject.getpixel((subject.width//2,subject.height//2))[3]==255
    f['contour']=list(reversed(f['contour']))
    assert face_sticker(src,f).tobytes()==subject.tobytes()
    out=paste_reaction(src,subject,.3,1.,f['box'])
    assert out.tobytes()!=src.tobytes()
    assert out.crop((0,500,360,640)).tobytes()==src.crop((0,500,360,640)).tobytes()
    f['contour'][0]=[float('nan'),0]
    assert face_sticker(src,f) is None
    assert face_sticker(src,face()) is None


def test_contour_tracks_only_the_matching_face():
    rows=observations()
    for r in rows:r['faces'][0].update(contour=[[.3,.3]]*8,contour_kind='jaw')
    assert len(face_at(rows,1.1,face()['box'])['contour'])==8
    rows[5]['faces']=[]
    assert face_at(rows,1.1,face()['box']) is None


def test_confident_but_inverted_native_landmarks_are_not_effect_anchors():
    f=face();f['mouth']=[[x,y-.2] for x,y in f['mouth']]
    assert not valid_face(f)
    f=face();f['mouth']=[[.3]]
    assert not valid_face(f)


def test_quote_uses_stronger_later_phrase_at_its_actual_time():
    lines=[dict(start=15.1,end=15.75,text='不用不用'),dict(start=15.75,end=16.5,text='吐不了吐不了')]
    event=plan_effects([],lines)[0]
    assert event['text']=='吐不了' and event['start']==15.75
    assert plan_effects([],[dict(start=5,end=6,text='嘴包住',context='嘴包住。嗓子打开。你知道吗？')])[0]['text']=='嘴包住'
    assert plan_effects([],[dict(start=5,end=6,text='嘴包住？')])==[]


def test_cartoon_sound_is_ducked_during_speech_and_bounded(tmp_path):
    p=tmp_path/'sfx.wav'
    events=[dict(kind='eyes',audible_start=.3),dict(kind='eyes',audible_start=1.3)]
    cues=write_sounds(p,2,events,[dict(start=0,end=1)])
    assert cues[0]['ducked_for_speech'] and not cues[1]['ducked_for_speech']
    with wave.open(str(p),'rb') as audio:
        assert audio.getnframes()==96000
        data=array.array('h',audio.readframes(96000))
    assert max(map(abs,data[:48000]))<max(map(abs,data[48000:]))<6000
    assert not any(data[:14400])


def test_face_render_and_sound_mix_keep_duration_and_audible_original(tmp_path, monkeypatch):
    from app.services import automatic_treatment as treatment, media
    src=tmp_path/'src.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i','color=orange:s=180x320:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:v','libx264',
        '-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    rows=observations()
    for r in rows:r['faces'][0].update(contour=[[.3,.31],[.3,.4],[.32,.48],[.37,.54],
        [.43,.55],[.5,.5],[.55,.4],[.55,.31]],contour_kind='jaw')
    monkeypatch.setattr(treatment,'plan_effects',lambda *a:[dict(kind='eyes',start=.4,end=1.5,
        peak=.6,anchor=face()['box'],freeze=0,basis='test')])
    segments=[dict(start=0,end=2,_dialogue=[dict(start=.2,end=1.8,text='原声保留',evidence_id='s')])]
    _,meta=treatment.render_automatic(src,out,segments,tmp_path,observations=rows)
    assert meta['face_stickers']==1 and len(meta['cartoon_sounds'])==1
    assert meta['cartoon_sounds'][0]['ducked_for_speech']
    assert abs(media.probe(src)['duration']-media.probe(out)['duration'])<.08
    def pcm(path):return array.array('h',subprocess.check_output([_ffmpeg(),'-v','error','-i',str(path),
        '-map','0:a','-ac','1','-ar','48000','-f','s16le','-']))
    a,b=pcm(src),pcm(out)
    # Compare the original waveform outside the effect; re-encoding is allowed,
    # missing original audio or a shifted/normalised-to-half mix is not.
    left,right=a[1000:15000],b[1000:15000]
    gain=sum(x*y for x,y in zip(left,right))/sum(x*x for x in left)
    assert .9<gain<1.1
