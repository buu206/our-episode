from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.services.dialogue_captions import attach_dialogue, apply_subtitle_edits, review_flags, display_text
from app.services.editing import EditSegment
from app.repository import DomainError
from app.models import Version
from app.worker import source_segments
from test_pipeline import client, settings, services, no_network, voiced_video_bytes, create_with_asset, analyze_and_review, request, episode_state, advance


def test_segmentation_preserves_particles_negation_and_english():
    words=[{'start':0.,'end':.3,'text':'不要，'},{'start':.31,'end':.6,'text':'好吗？'},
           {'start':1.,'end':1.2,'text':'hello'},{'start':1.21,'end':1.4,'text':'world'}]
    job=SimpleNamespace(payload={'steps':{'asr:a':words}})
    db=SimpleNamespace(scalars=lambda *_:SimpleNamespace(all=lambda:[job]))
    episode=SimpleNamespace(id='e',owner_id='o',evidence_revision=1,evidence_confirmed_revision=1,
        evidence=[dict(id='ev',asset_id='a',kind='speech',start=0.,end=1.4,note='不要，好吗？hello world')])
    segments=[dict(asset_id='a',start=0.,end=2.)]
    attach_dialogue(db,episode,segments)
    assert [r['text'] for r in segments[0]['_dialogue']]==['不要','好吗？','hello world']
    assert display_text('不 要 啊！')=='不要啊！'
    assert '有重复词，请听原声' in review_flags(dict(text='嗓子嗓子',start=0.,end=2.,alignment='asr'))


def test_edit_contract_and_hidden_speech_still_protected():
    cue='c'*32
    segment=dict(asset_id='a',start=0.,end=2.,caption='',subtitle_edits=[dict(cue_id=cue,text='不喝了！',hidden=True)])
    parsed=EditSegment.model_validate(segment).model_dump()
    parsed['_dialogue']=[dict(cue_id=cue,text='喝了',start=.1,end=1.)]
    apply_subtitle_edits([parsed])
    assert parsed['_dialogue']==[] and parsed['_speech_intervals']==[(.1,1.)]
    with pytest.raises(DomainError):apply_subtitle_edits([{**parsed,'_dialogue':[]}])
    with pytest.raises(DomainError):apply_subtitle_edits([{**parsed,'_preserve_expression':True}])
    for text in ('','x\x00','a\nb\nc'):
        bad=deepcopy(segment);bad['subtitle_edits'][0].update(text=text,hidden=False)
        with pytest.raises(ValueError):EditSegment.model_validate(bad)
    bad=deepcopy(segment);bad['subtitle_edits']*=2
    with pytest.raises(ValueError):EditSegment.model_validate(bad)


def test_api_edit_is_versioned_persistent_rendered_and_owner_scoped(client,services,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    analyze_and_review(client,auth,eid)
    before=deepcopy(episode_state(client,eid)['evidence'])
    base=request(client,f'/episodes/{eid}/versions',auth,body=dict(title='字幕测试',segments=[dict(asset_id=asset['id'],start=0.,end=2.,caption='')],packaging='variety_v2'),expected=201)
    track=request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')
    assert track['rows']
    cue=track['rows'][0];counts=deepcopy(services.counts)
    segments=deepcopy(base['segments']);segments[0]['subtitle_edits']=[dict(cue_id=cue['cue_id'],text='不喝了！',hidden=False,reviewed=True)]
    new=request(client,f'/episodes/{eid}/versions',auth,body=dict(base_version_id=base['id'],title='字幕校对',segments=segments,packaging='variety_v2'),expected=201)
    checked=request(client,f"/versions/{new['id']}/subtitles",auth,method='GET')
    assert checked['rows'][0]['text']=='不喝了！' and checked['rows'][0]['reviewed']
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows'][0]['text']==cue['text']
    assert episode_state(client,eid)['evidence']==before
    with client.app.state.repo.factory() as db:
        saved=db.get(Version,new['id'])
        assert source_segments(client.app.state.repo,db,saved)[0]['_dialogue'][0]['text']=='不喝了！'
    job=request(client,f"/versions/{new['id']}/render",auth)
    advance(client,job['id'])
    assert next(v for v in episode_state(client,eid)['versions'] if v['id']==new['id'])['state']=='ready'
    review=request(client,f"/versions/{new['id']}/review",auth)
    report=advance(client,review['id'])['result']['source_review']
    assert report['evidence_confirmed'] and report['captions_checked'] >= 1
    assert report['scope']=='source_metadata'
    assert services.counts==counts
    segments[0]['subtitle_edits'][0]['cue_id']='0'*32
    assert request(client,f'/episodes/{eid}/versions',auth,body=dict(base_version_id=base['id'],title='错误引用',segments=segments,packaging='variety_v2'),expected=409)['error']['code']=='SUBTITLES_CHANGED'
    with client.app.state.repo.transaction() as db:db.get(Version,new['id']).owner_id='another-owner'
    request(client,f"/versions/{new['id']}/subtitles",auth,method='GET',expected=404)
