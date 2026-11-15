"""Offline editorial contracts and real local media; no model quality claims."""
from copy import deepcopy
import subprocess
import pytest
from app.services.editorial_plan import EditorialBeat, compile_editorial
from app.services.editing import EditSegment, validate_director_segments
from app.services.editorial_render import render_editorial, chapter_layout
from test_media import _ffmpeg
from test_visual_beats import frame


def beat(**kw):
    return dict(role='setup',treatment='chapter',reason='先交代现场再继续过程',
                evidence_ids=['v'],source_time=0.,duration=2.,text='现场说明',detail='保持完整原画面',**kw)


@pytest.mark.parametrize('changes',[
    {'role':'action'}, {'duration':1.}, {'source_time':float('nan')},
    {'treatment':'none'}, {'evidence_ids':['v','v']}, {'detail':'规则'*20,'duration':2.},
    {'text':'\n'}, {'treatment':'execute'},
])
def test_invalid_plan_cannot_be_rendered(changes):
    data=beat();data.update(changes)
    with pytest.raises(ValueError):EditorialBeat.model_validate(data)


def test_sources_and_boundaries_and_legacy_compatibility():
    row=dict(asset_id='a',start=0.,end=3.,caption='',purpose='交代',evidence_ids=['v'],editorial=beat())
    evidence=[dict(id='v',asset_id='a',kind='visual',start=0.,end=3.,note='测试画面')]
    validate_director_segments([row],evidence)
    bad=deepcopy(row);bad['editorial']['evidence_ids']=['other']
    with pytest.raises(ValueError):validate_director_segments([bad],evidence)
    bad=deepcopy(row);bad['end']=1.
    with pytest.raises(ValueError):EditSegment.model_validate(bad)
    legacy=deepcopy(row);legacy.pop('editorial')
    assert 'editorial' not in EditSegment.model_validate(legacy).model_dump()
    bad=deepcopy(row);bad['caption']='又一层字幕'
    with pytest.raises(ValueError):EditSegment.model_validate(bad)


def test_no_effect_is_explicit_and_transition_overlap_rejected():
    blank=dict(role='action',treatment='none',reason='完整动作无需加字',evidence_ids=['v'])
    rows=[dict(start=0.,end=4.,caption='',editorial=blank),
          dict(start=0.,end=4.,caption='',transition='dissolve',transition_duration=.3,editorial=beat())]
    with pytest.raises(ValueError):compile_editorial(rows)
    rows[1]['editorial']['source_time']=.4
    assert [x['beat'].treatment for x in compile_editorial(rows)]==['none','chapter']
    rows[1]['_preserve_expression']=True
    with pytest.raises(ValueError):compile_editorial(rows)


@pytest.mark.parametrize('size',[(180,320),(320,180)])
def test_full_frame_keeps_whole_source_audio_and_leaves_process_clean(tmp_path,size):
    w,h=size;src=tmp_path/'src.mp4';out=tmp_path/'out.mp4'
    subprocess.run([_ffmpeg(),'-v','error','-f','lavfi','-i',f'color=blue:s={w}x{h}:r=30',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6','-c:v','libx264',
        '-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(src)],check=True,capture_output=True)
    accent=beat();accent.update(role='reaction',treatment='accent',source_time=.3,duration=1.,text='等等',detail='')
    rows=[dict(start=0.,end=2.,editorial=beat()),
          dict(start=0.,end=2.,editorial=dict(role='action',treatment='none',reason='过程留白',evidence_ids=['v'])),
          dict(start=0.,end=2.,editorial=accent)]
    path,result=render_editorial(src,out,rows,tmp_path)
    assert path==out and len(result['editorial_summary'])==3
    x,y,vw,vh=chapter_layout(w,h)
    assert abs(vw/vh-w/h)<.02
    card=frame(out,.8)
    assert card.getpixel((x+vw//2,y+vh//2))[2]>200
    # The new intro overlays a title, but never replaces/shrinks the source.
    assert card.getpixel((10,h//2))[2]>240
    assert card.getpixel((round(w*.18),round(h*.045+30*min(w,h)/720)))[0]>200
    clear=frame(out,3.)
    assert clear.getpixel((w//2,h//2))[2]>240 and clear.getpixel((10,10))[0]<10
    def audio(path):
        return subprocess.check_output([_ffmpeg(),'-v','error','-i',str(path),'-map','0:a:0','-c:a','copy','-f','adts','-'])
    assert audio(src)==audio(out)


def test_injected_model_plan_survives_selection_and_render(client,services,voiced_video_bytes):
    from test_pipeline import create_with_asset,analyze_and_review,request,advance,episode_state
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    aid=asset['id']
    analyze_and_review(client,auth,eid)
    ep=episode_state(client,eid)
    items=deepcopy(ep['evidence']);items[0].update(start=0.,end=2.)
    ep=request(client,f'/episodes/{eid}/evidence',auth,method='PUT',body={
        'revision':ep['evidence_revision'],'items':items,'confirmed':True})
    refs=[e['id'] for e in ep['evidence'] if e['asset_id']==aid and e['start']<=0 and e['end']>=2]
    assert refs
    async def proposal(evidence,assets,background,reservation,permit):
        services._permission('directions',reservation,permit)
        source=next(e for e in evidence if e['id']==refs[0])
        return {'result':{'directions':[{'id':'editorial-test','title':'合成测试','summary':'非真实模型',
            'evidence':[{k:source[k] for k in ('asset_id','start','end','note')}],
            'edit_plan':{'segments':[dict(asset_id=aid,start=0.,end=2.,caption='',purpose='测试说明',
                evidence_ids=refs,editorial={**beat(),'evidence_ids':[refs[0]]})],'narration_text':''}}],
            'uncertainties':[]},'usage':services._usage('directions')}
    services.directions=proposal
    job=request(client,f'/episodes/{eid}/directions',auth)
    advance(client,job['id'])
    ep=episode_state(client,eid)
    version=request(client,f'/episodes/{eid}/direction',auth,expected=201,body={
        'direction_id':'editorial-test','revision':ep['evidence_revision'],'packaging':'variety_v2'})
    job=request(client,f"/versions/{version['id']}/render",auth)
    done=advance(client,job['id'])
    assert done['state']=='succeeded',done
    assert done['result']['editorial_summary'][0]['treatment']=='chapter'
    ep=episode_state(client,eid)
    stored=next(v for v in ep['versions'] if v['id']==version['id'])
    assert stored['state']=='ready' and stored['segments'][0]['editorial']['reason']=='先交代现场再继续过程'


# These fixtures explicitly disable all network calls and use synthetic video.
from test_pipeline import client, services, settings, voiced_video_bytes, no_network


def test_model_diagnostic_does_not_echo_untrusted_field_or_value():
    import json
    from app.services.director import parse_result,DirectorError
    data={'directions':[], 'uncertainties':['待补充'], 'private-secret-field':'private-material'}
    with pytest.raises(DirectorError) as error:
        parse_result(json.dumps(data), {'a':3.})
    assert 'extra_forbidden' in error.value.message
    assert 'private' not in error.value.message


def test_effect_limits_and_reading_time_have_deterministic_errors():
    too_fast=beat();too_fast.update(detail='二十个汉字需要更长的时间才能阅读完毕',duration=2.)
    with pytest.raises(ValueError,match='停留过短'):EditorialBeat.model_validate(too_fast)
    rows=[dict(start=0.,end=3.,editorial=beat()) for _ in range(3)]
    with pytest.raises(ValueError,match='最多两处'):compile_editorial(rows)
    accent=beat();accent.update(treatment='accent',role='action',duration=2.5,detail='')
    rows=[dict(start=0.,end=3.,editorial=accent) for _ in range(4)]
    with pytest.raises(ValueError,match='包装占比过高'):compile_editorial(rows)
